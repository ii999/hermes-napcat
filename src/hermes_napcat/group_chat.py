"""Group observation, bounded catch-up, and participation scheduling.

The controller admits turns through a callback; Hermes still owns model execution.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from .config import Settings
from .context import GroupContext, GroupMessage
from .engagement import ParticipationClassifier, WindowBudget, candidate_from_tail
from .policy import Policy, RecentIDs
from .protocol import Incoming, Target, message_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupTurn:
    incoming: Incoming
    text: str
    own_reply: bool = False
    context: str | None = None
    quote: GroupMessage | None = None
    proactive: bool = False


@dataclass
class ProactiveTicket:
    address: str
    revision: int
    expires: float
    first_send_started: bool = False


class GroupChatController:
    def __init__(self, settings: Settings, policy: Policy,
                 call: Callable[..., Awaitable[Any]],
                 dispatch: Callable[[GroupTurn], Awaitable[None]], *,
                 transport_state: Callable[[], tuple[int, int]] = lambda: (0, 0),
                 classifier_key: str = ""):
        self.settings, self.policy, self.call, self.dispatch = settings, policy, call, dispatch
        self.config, self.proactive = settings.group_context, settings.proactive_assist
        self.context = GroupContext(settings, policy)
        self.transport_state = transport_state
        self.classifier = ParticipationClassifier(self.proactive.classifier, classifier_key)
        self.seen = RecentIDs(settings.dedup_capacity, settings.dedup_ttl)
        self._negative_quotes = RecentIDs(settings.dedup_capacity, 30)
        self._observe_budget = WindowBudget(self.config.observation_messages_per_minute, 60,
                                           settings.dedup_capacity)
        self._read_budget = WindowBudget(30, 60, self.config.max_groups)
        self._decisions = WindowBudget(self.proactive.max_decisions_per_hour, 3600, self.config.max_groups)
        self._responses = WindowBudget(self.proactive.max_responses_per_hour, 3600, self.config.max_groups)
        self._cooldowns: OrderedDict[str, float] = OrderedDict()
        self._fetch_state: OrderedDict[str, tuple[tuple[int, int], float]] = OrderedDict()
        self._fetches: dict[str, asyncio.Task] = {}
        self._queues: dict[str, deque[Incoming]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._controls: set[asyncio.Task] = set()
        self._proactive_runs: set[asyncio.Task] = set()
        self._pending = 0
        self._closed = False
        self._model_slots = asyncio.Semaphore(settings.event_workers)
        self._classifier_slot = asyncio.Semaphore(1)
        self._ticket: ContextVar[ProactiveTicket | None] = ContextVar("napcat_proactive", default=None)
        self.stats = {key: 0 for key in (
            "observed", "observation_dropped", "queue_dropped", "history_errors",
            "decisions", "classifier_errors", "dry_run", "proactive_dispatched",
            "stale_suppressed", "dispatch_errors", "recalls",
        )}

    @property
    def closed(self) -> bool:
        return self._closed

    def _bounded_set(self, mapping: OrderedDict, key: str, value: Any) -> None:
        mapping.pop(key, None)
        mapping[key] = value
        while len(mapping) > self.config.max_groups:
            mapping.popitem(last=False)

    def _cancel_timer(self, address: str) -> None:
        task = self._timers.pop(address, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def receive(self, raw: dict[str, Any]) -> None:
        if self._closed or not self.config.enabled:
            return
        if raw.get("post_type") == "notice":
            if (raw.get("notice_type") != "group_recall"
                    or str(raw.get("self_id")) != self.settings.self_id):
                return
            try:
                target = Target.parse(f"group:{raw.get('group_id')}")
                identifier = message_id(raw.get("message_id"))
            except ValueError:
                return
            if target.id in self.settings.allowed_groups:
                self.context.recall(target.address, identifier)
                self._cancel_timer(target.address)
                self.stats["recalls"] += 1
            return
        try:
            incoming = Incoming.parse(raw)
        except (ValueError, TypeError):
            log.warning("Invalid group message rejected")
            return
        if (incoming is None or incoming.target.kind != "group"
                or incoming.self_id != self.settings.self_id
                or incoming.target.id not in self.settings.allowed_groups):
            return
        address = incoming.target.address
        key = (address, incoming.message_id)
        if self.seen.contains(key) or self.context.is_recalled(*key):
            return
        self.seen.add(key)
        self._cancel_timer(address)  # Even observation-only members can take the conversational floor.
        self.context.mark_activity(address)
        authorized = self.policy.can_receive(incoming)
        direct = self.policy.trigger(incoming)
        reply_candidate = incoming.reply_to is not None and self.settings.group_reply_to_bot
        observe = self.context.can_observe(incoming) and (
            self.config.observe_untriggered or direct is not None or reply_candidate
            or incoming.user_id == self.settings.self_id)
        if observe:
            admitted = (authorized and direct is not None) or self._observe_budget.take(
                f"{address}:{incoming.user_id}")
            if admitted:
                try:
                    self.stats["observed"] += int(self.context.put(incoming))
                except ValueError:
                    log.warning("Invalid group attribution rejected")
                    return
            else:
                self.context.room(address).gap = True
                self.stats["observation_dropped"] += 1
        if not authorized:
            return
        if direct is not None and direct.lstrip().startswith("/") and incoming.user_id in self.settings.admins:
            # Do not queue /stop behind a slow model turn. Preserve the genuine principal.
            if self.policy.rate_allowed(incoming) and len(self._controls) < self.config.max_pending_messages:
                task = asyncio.create_task(self._dispatch_control(GroupTurn(incoming, direct)))
                self._controls.add(task)
                task.add_done_callback(self._controls.discard)
            return
        if direct is not None or reply_candidate:
            self._enqueue(incoming)
        elif self.proactive.enabled and observe:
            # Timers retain only bounded text and identity, never remote URLs or a raw WS payload.
            record = self.context.lookup(address, incoming.message_id)
            if record is None:
                return
            bounded = replace(incoming, text=record.text, segments=[],
                              raw={"time": record.timestamp})
            self._timers[address] = asyncio.create_task(self._after_quiet(bounded))

    async def _dispatch_control(self, turn: GroupTurn) -> None:
        try:
            await self.dispatch(turn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["dispatch_errors"] += 1
            log.warning("Group control dispatch failed (%s)", type(exc).__name__)

    def _enqueue(self, incoming: Incoming) -> None:
        address = incoming.target.address
        if (self._pending >= self.config.max_pending_messages
                or (address not in self._workers and len(self._workers) >= self.config.max_groups)):
            self.stats["queue_dropped"] += 1
            self.context.room(address).gap = True
            log.warning("Group turn queue full; addressed message was not dispatched")
            return
        self._queues.setdefault(address, deque()).append(incoming)
        self._pending += 1
        if address not in self._workers:
            self._workers[address] = asyncio.create_task(self._drain(address))

    async def _drain(self, address: str) -> None:
        try:
            queue = self._queues[address]
            while queue and not self._closed:
                incoming = queue.popleft()
                self._pending -= 1
                try:
                    await self._addressed(incoming)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.stats["dispatch_errors"] += 1
                    log.warning("Group turn failed (%s); no automatic replay", type(exc).__name__)
        finally:
            remaining = self._queues.pop(address, deque())
            self._pending -= len(remaining)
            self._workers.pop(address, None)

    async def _quote(self, incoming: Incoming) -> GroupMessage | None:
        identifier = incoming.reply_to
        address = incoming.target.address
        if identifier is None or self.context.is_recalled(address, identifier):
            return None
        record = self.context.lookup(address, identifier)
        if record is not None:
            return record
        key = (address, identifier)
        if self._negative_quotes.contains(key) or not self._read_budget.take(address):
            return None
        try:
            async with asyncio.timeout(self.config.backfill_timeout_seconds):
                raw = await self.call("get_msg", {"message_id": int(identifier)})
            parsed = self.context.parse_history(incoming.target, raw)
            if parsed is None or parsed.message_id != identifier:
                raise ValueError("unverified quote")
            record = GroupMessage.from_incoming(parsed, self.config.max_message_chars, history=True)
            self.context.put(parsed, history=True)
            return record
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._negative_quotes.add(key)
            log.info("Group quote unavailable (%s)", type(exc).__name__)
            return None

    async def _fetch_page(self, target: Target, anchor: str | None) -> bool:
        if not self._read_budget.take(target.address):
            return False
        params: dict[str, Any] = {
            "group_id": target.id, "count": self.config.history_limit,
            "reverse_order": False, "disable_get_url": True, "parse_mult_msg": False,
        }
        if anchor is not None:
            # NapCat resolves this field through its short-message-ID mapping, not arithmetic.
            params["message_seq"] = anchor
        try:
            async with asyncio.timeout(self.config.backfill_timeout_seconds):
                data = await self.call("get_group_msg_history", params)
            if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                raise ValueError("invalid history response")
            rows = data["messages"]
            if len(rows) > self.config.history_limit + 1:
                raise ValueError("history response exceeds requested bound")
            rejected = False
            for raw in rows:
                incoming = self.context.parse_history(target, raw)
                if incoming is None:
                    rejected = True
                    continue
                self.context.put(incoming, history=True)
            room = self.context.room(target.address)
            room.history_status = "filtered_window" if rejected else "fetched_window"
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.context.room(target.address).history_status = "unavailable"
            self.stats["history_errors"] += 1
            log.warning("Group history unavailable (%s); using retained context", type(exc).__name__)
            return False

    async def ensure_history(self, target: Target, anchor: str | None = None) -> None:
        if not self.config.history_backfill:
            return
        address = target.address
        existing = self._fetches.get(address)
        if existing is not None:
            await asyncio.shield(existing)
            return
        state = self.transport_state()
        previous = self._fetch_state.get(address)
        room = self.context.room(address)
        if previous is not None:
            old_state, attempted_at = previous
            if old_state != state:
                room.gap = True
            if time.monotonic() - attempted_at < self.config.backfill_cooldown_seconds:
                return
            if (old_state == state and room.history_status in ("fetched_window", "filtered_window")
                    and self.config.observe_untriggered and not room.gap):
                return
        task = self._fetches.get(address)
        if task is None:
            self._bounded_set(self._fetch_state, address, (state, time.monotonic()))
            task = asyncio.create_task(self._fetch_page(target, anchor))
            self._fetches[address] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._fetches.get(address) is task:
                self._fetches.pop(address, None)

    async def _addressed(self, incoming: Incoming) -> None:
        address = incoming.target.address
        if self.context.is_recalled(address, incoming.message_id):
            return
        text = self.policy.trigger(incoming)
        quote = None
        own_reply = incoming.reply_to is not None and self.policy.own.contains((address, incoming.reply_to))
        if text is None:
            quote = await self._quote(incoming)
            own_reply = own_reply or bool(quote and quote.own)
            text = self.policy.trigger(incoming, own_reply)
        if text is None or not self.policy.rate_allowed(incoming):
            return
        self._bounded_set(self._cooldowns, address, time.monotonic())
        self._cancel_timer(address)
        async with self._model_slots:
            await self.ensure_history(incoming.target, incoming.message_id)
            quote = quote or await self._quote(incoming)
            own_reply = own_reply or bool(quote and quote.own)
            if self.context.is_recalled(address, incoming.message_id):
                return
            context, _, _ = self.context.render(address, current=incoming, quote=quote)
            await self.dispatch(GroupTurn(incoming, text, own_reply, context, quote))

    async def _after_quiet(self, incoming: Incoming) -> None:
        address = incoming.target.address
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.proactive.quiet_window_ms / 1000)
            if self._closed or address in self._workers:
                return
            last = self._cooldowns.get(address, float("-inf"))
            if time.monotonic() - last < self.proactive.cooldown_seconds:
                return
            revision = self.context.room(address).revision
            rows = self.context.records(address, limit=self.config.history_limit)
            candidate = candidate_from_tail(rows, self.proactive, self.settings.self_id)
            if (candidate is None or candidate.message_id != incoming.message_id
                    or not self.policy.can_receive(incoming)
                    or not self._decisions.take(address)):
                return
            await self.ensure_history(incoming.target, incoming.message_id)
            context, _, _ = self.context.render(address)
            async with self._classifier_slot:
                if revision != self.context.room(address).revision:
                    return
                decision = await self.classifier.decide(candidate, context)
            self.stats["decisions"] += 1
            if (self._closed or revision != self.context.room(address).revision
                    or self.context.is_recalled(address, incoming.message_id)):
                self.stats["stale_suppressed"] += 1
                return
            if not decision.respond or decision.confidence < self.proactive.confidence_threshold:
                return
            if not self._responses.take(address):
                return
            self._bounded_set(self._cooldowns, address, time.monotonic())
            if self.proactive.dry_run:
                self.stats["dry_run"] += 1
                log.info("Proactive dry-run: would respond (reason=%s)", decision.reason)
                return
            if not self.policy.rate_allowed(incoming):
                return
            async with self._model_slots:
                if revision != self.context.room(address).revision:
                    return
                ticket = ProactiveTicket(address, revision,
                                         time.monotonic() + self.proactive.max_reply_age_seconds)
                token = self._ticket.set(ticket)
                try:
                    # Earlier fragments remain in channel_context; the latest fragment anchors the reply.
                    context, _, _ = self.context.render(address, current=incoming)
                    self.stats["proactive_dispatched"] += 1
                    if self._timers.get(address) is current_task:
                        self._timers.pop(address, None)
                    self._proactive_runs.add(current_task)
                    await self.dispatch(GroupTurn(incoming, candidate.text, context=context, proactive=True))
                finally:
                    self._ticket.reset(token)
                    self._proactive_runs.discard(current_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["classifier_errors"] += 1
            log.warning("Proactive evaluation/dispatch failed (%s); staying silent", type(exc).__name__)
        finally:
            if self._timers.get(address) is current_task:
                self._timers.pop(address, None)

    def before_send(self, target: Target) -> None:
        ticket = self._ticket.get()
        if ticket is None or ticket.address != target.address or ticket.first_send_started:
            return
        if (self._closed or time.monotonic() > ticket.expires
                or self.context.room(target.address).revision != ticket.revision):
            self.stats["stale_suppressed"] += 1
            raise PermissionError("proactive response is stale; do not retry")
        # Once any part starts, preserve existing partial/uncertain delivery semantics.
        ticket.first_send_started = True

    def sent(self, target: Target, identifier: str, action: str, params: dict[str, Any]) -> None:
        if not self.config.enabled or target.kind != "group":
            return
        parts = params.get("message")
        if not isinstance(parts, list):
            parts = [{"type": "text", "data": {"text": "[机器人发送了合并转发消息]"}}]
        raw = {"post_type": "message", "message_type": "group", "group_id": target.id,
               "self_id": self.settings.self_id, "user_id": self.settings.self_id,
               "message_id": identifier, "time": time.time(), "message": parts,
               "sender": {"nickname": "assistant", "user_id": self.settings.self_id}}
        try:
            incoming = Incoming.parse(raw)
            if incoming is not None:
                self.context.put(incoming)
                self.seen.add((target.address, identifier))
                self._cancel_timer(target.address)
        except (ValueError, TypeError):
            # A completed send must not become a delivery failure because context recording failed.
            log.warning("Sent message could not be recorded in group context")

    async def recent_messages(self, target: Target, *, limit: int = 50,
                              before: str | None = None) -> dict[str, Any]:
        if (not self.config.enabled or target.kind != "group"
                or not self.policy.can_send(target)):
            raise PermissionError("group context is disabled or target is not authorized")
        if type(limit) is not int or not 1 <= limit <= self.config.history_limit:
            raise ValueError("limit exceeds group_context.history_limit")
        if before is not None:
            before = message_id(before)
            if self.context.lookup(target.address, before) is None:
                raise ValueError("before_message_id must be in the current retained group context")
            # Anchors come from verified local records; never pass arbitrary model IDs to pagination.
            if self.config.history_backfill:
                await self._fetch_page(target, before)
        else:
            await self.ensure_history(target)
        _, rows, truncated = self.context.render(target.address, before=before, limit=limit)
        room = self.context.room(target.address)
        return {"success": True, "target": target.address, "messages": rows,
                "truncated": truncated, "history_status": room.history_status,
                "gap_detected": room.gap, "window_seconds": self.config.history_window_seconds}

    async def wait_idle(self) -> None:
        """Join scheduled admission/evaluation tasks; useful for shutdown checks and protocol tests."""
        tasks = [*self._workers.values(), *self._timers.values(), *self._controls, *self._proactive_runs]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._workers.values(), *self._timers.values(), *self._fetches.values(),
                 *self._controls, *self._proactive_runs]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._timers.clear()
        self._fetches.clear()
        self._controls.clear()
        self._proactive_runs.clear()
        self._queues.clear()
        self._pending = 0
        await self.classifier.close()
        self.context.clear()
