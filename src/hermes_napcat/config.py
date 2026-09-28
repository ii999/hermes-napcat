"""Validated configuration; no process-global environment mutations."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def numeric_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("QQ identifiers must be positive decimal strings or integers")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0 or len(text) > 20:
        raise ValueError("QQ identifiers must be positive decimal strings or integers")
    return str(int(text))


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        mapped = getattr(address, "ipv4_mapped", None)
        return address.is_loopback or bool(mapped and mapped.is_loopback)
    except ValueError:
        return False


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class SharedPath(StrictModel):
    hermes: Path
    napcat: str

    @model_validator(mode="after")
    def absolute_roots(self):
        if not self.hermes.is_absolute() or self.hermes == Path(self.hermes.anchor):
            raise ValueError("shared hermes path must be an absolute, dedicated directory")
        # NapCat's remote runtime may use Linux or Windows paths.
        if not (self.napcat.startswith("/") or
                (len(self.napcat) >= 3 and self.napcat[1:3] in (":/", ":\\"))):
            raise ValueError("shared napcat path must be absolute")
        remote_root = self.napcat.rstrip("/\\")
        if not remote_root or (len(remote_root) == 2 and remote_root[1] == ":"):
            raise ValueError("do not share a filesystem root")
        return self


class MediaSettings(StrictModel):
    enabled: bool = True
    allowed_hosts: tuple[str, ...] = (
        "multimedia.nt.qq.com", "gchat.qpic.cn", "c2cpicdw.qpic.cn", "grouptalk.c2c.qq.com",
    )
    trusted_private_origins: tuple[str, ...] = ()
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_attachments: int = Field(default=4, ge=1, le=16)
    timeout: float = Field(default=30, gt=0, le=300)
    cache_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    cache_ttl_seconds: int = Field(default=86400, ge=3600)
    outbound_roots: tuple[Path, ...] = ()
    shared_paths: tuple[SharedPath, ...] = ()
    inline_max_bytes: int = Field(default=512 * 1024, ge=1024, le=32 * 1024 * 1024)

    @field_validator("allowed_hosts")
    @classmethod
    def exact_hosts(cls, hosts):
        result = []
        for host in hosts:
            host = host.lower().rstrip(".")
            if not host or any(c in host for c in ("*", "/", "@", ":", " ")):
                raise ValueError("media.allowed_hosts accepts exact DNS host names")
            result.append(host)
        return tuple(result)

    @field_validator("trusted_private_origins")
    @classmethod
    def valid_origins(cls, origins):
        result = []
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                    parsed.username or parsed.password or parsed.query or parsed.fragment or
                    parsed.path not in ("", "/")):
                raise ValueError("private origins must be exact http(s) origins, without paths")
            # Accessing .port validates its syntax/range.
            _ = parsed.port
            result.append(origin.rstrip("/").lower())
        return tuple(result)

    @model_validator(mode="after")
    def valid_storage(self):
        if self.cache_max_bytes < self.max_bytes:
            raise ValueError("cache_max_bytes must be at least max_bytes")
        for path in self.outbound_roots:
            if not path.is_absolute() or path == Path(path.anchor):
                raise ValueError("outbound_roots must contain dedicated absolute directories")
        return self


class QQToolsSettings(StrictModel):
    """Limits and opt-ins for model-callable QQ actions."""

    enabled: bool = False
    allow_cross_chat: bool = False
    max_segments: int = Field(default=64, ge=1, le=256)
    max_media_items: int = Field(default=8, ge=1, le=32)
    max_forward_nodes: int = Field(default=50, ge=1, le=100)
    max_forward_chars: int = Field(default=50_000, ge=100, le=200_000)
    max_local_media_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024 * 1024,
    )


class GroupContextSettings(StrictModel):
    """Opt-in, bounded public group context, separate from Hermes sessions."""

    enabled: bool = False
    observe_untriggered: bool = True
    observe_all_members: bool = False
    live_buffer_messages: int = Field(default=200, ge=10, le=2000)
    max_groups: int = Field(default=64, ge=1, le=256)
    max_message_chars: int = Field(default=4000, ge=200, le=16000)
    max_context_chars: int = Field(default=12000, ge=1000, le=64000)
    history_limit: int = Field(default=50, ge=1, le=100)
    history_window_seconds: int = Field(default=1800, ge=30, le=86400)
    history_backfill: bool = True
    backfill_timeout_seconds: float = Field(default=3, gt=0, le=30)
    backfill_cooldown_seconds: float = Field(default=30, ge=1, le=3600)
    stop_at_last_bot_message: bool = False
    max_pending_messages: int = Field(default=32, ge=1, le=256)
    observation_messages_per_minute: int = Field(default=120, ge=1, le=1200)


class ClassifierSettings(StrictModel):
    """An operator-configured, tool-free chat-completions classifier."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = ""
    api_key_env: str = "NAPCAT_CLASSIFIER_API_KEY"
    timeout_seconds: float = Field(default=8, gt=0, le=30)
    allow_insecure_http: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self):
        url = urlsplit(self.base_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username or
                url.password or url.query or url.fragment):
            raise ValueError("classifier.base_url must be an http(s) endpoint without credentials")
        _ = url.port
        if url.scheme == "http" and not loopback(url.hostname) and not self.allow_insecure_http:
            raise ValueError("remote classifier HTTP requires allow_insecure_http=true")
        if self.enabled and not self.model.strip():
            raise ValueError("classifier.model is required when enabled")
        if (not self.api_key_env.startswith("NAPCAT_CLASSIFIER_") or
                not self.api_key_env.replace("_", "").isalnum() or
                not self.api_key_env.isascii()):
            raise ValueError("use a dedicated NAPCAT_CLASSIFIER_* secret")
        return self


class ProactiveSettings(StrictModel):
    enabled: bool = False
    dry_run: bool = True
    quiet_window_ms: int = Field(default=2200, ge=100, le=30000)
    burst_window_seconds: int = Field(default=30, ge=1, le=120)
    confidence_threshold: float = Field(default=0.90, ge=0.5, le=1, allow_inf_nan=False)
    cooldown_seconds: float = Field(default=120, ge=1, le=3600)
    max_responses_per_hour: int = Field(default=4, ge=1, le=60)
    max_decisions_per_hour: int = Field(default=30, ge=1, le=600)
    max_reply_age_seconds: float = Field(default=90, ge=5, le=300)
    ignored_users: tuple[str, ...] = ()  # Explicit sibling-bot IDs; QQ has no reliable is_bot flag.
    classifier: ClassifierSettings = Field(default_factory=ClassifierSettings)

    @field_validator("ignored_users", mode="before")
    @classmethod
    def normalize_ignored(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError("use a YAML list for proactive ignored_users")
        return tuple(dict.fromkeys(numeric_id(item) for item in value))


class Settings(StrictModel):
    self_id: str
    token: SecretStr
    mode: Literal["forward", "reverse"] = "forward"
    ws_url: str = "ws://127.0.0.1:3001"
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=3002, ge=1, le=65535)
    ws_path: str = "/onebot/v11"
    allow_insecure_ws: bool = False
    allowed_users: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    allow_all_users: bool = False
    admins: tuple[str, ...] = ()
    group_mention: bool = True
    group_reply_to_bot: bool = True
    group_prefixes: tuple[str, ...] = ("/ai",)
    request_timeout: float = Field(default=15, gt=0, le=120)
    connect_timeout: float = Field(default=15, gt=0, le=120)
    reconnect_min: float = Field(default=1, gt=0, le=60)
    reconnect_max: float = Field(default=30, gt=0, le=300)
    ws_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    event_queue_size: int = Field(default=128, ge=1, le=4096)
    event_workers: int = Field(default=4, ge=1, le=16)
    dedup_ttl: float = Field(default=600, ge=1, le=86400)
    dedup_capacity: int = Field(default=8192, ge=16, le=100000)
    messages_per_minute: int = Field(default=12, ge=1, le=300)
    message_chars: int = Field(default=2000, ge=100, le=4000)
    max_outbound_chars: int = Field(default=20000, ge=100, le=100000)
    send_interval: float = Field(default=0.4, ge=0, le=10)
    # Replacement toolset for group sessions. Empty means no model tools.
    group_toolsets: tuple[str, ...] = ()
    media: MediaSettings = Field(default_factory=MediaSettings)
    qq_tools: QQToolsSettings = Field(default_factory=QQToolsSettings)
    group_context: GroupContextSettings = Field(default_factory=GroupContextSettings)
    proactive_assist: ProactiveSettings = Field(default_factory=ProactiveSettings)

    @field_validator("self_id", mode="before")
    @classmethod
    def normalize_self(cls, value):
        return numeric_id(value)

    @field_validator("allowed_users", "allowed_groups", "admins", mode="before")
    @classmethod
    def normalize_ids(cls, value):
        if not isinstance(value, (tuple, list)):
            raise ValueError("use a YAML list of QQ IDs")
        return tuple(dict.fromkeys(numeric_id(v) for v in value))

    @field_validator("token")
    @classmethod
    def require_token(cls, value):
        token = value.get_secret_value()
        if len(token) < 16 or any(c.isspace() for c in token):
            raise ValueError("NAPCAT_TOKEN must contain at least 16 non-whitespace characters")
        return value

    @field_validator("group_prefixes")
    @classmethod
    def prefixes(cls, values):
        if any(not p or p != p.strip() for p in values):
            raise ValueError("group prefixes must be nonempty and have no surrounding whitespace")
        return values

    @model_validator(mode="after")
    def validate_transport(self):
        if self.group_context.enabled and len(self.allowed_groups) > self.group_context.max_groups:
            raise ValueError("allowed_groups exceeds group_context.max_groups")
        if self.proactive_assist.enabled:
            if not self.group_context.enabled or not self.group_context.observe_untriggered:
                raise ValueError("proactive assist requires enabled live group context")
            if not self.proactive_assist.dry_run and self.group_toolsets:
                raise ValueError("live proactive assist requires group_toolsets=[]")
        parsed = urlsplit(self.ws_url)
        if (parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("ws_url must be a ws(s) URL without credentials, query or fragment")
        _ = parsed.port
        if self.mode == "forward" and parsed.scheme == "ws" and not loopback(parsed.hostname):
            if not self.allow_insecure_ws:
                raise ValueError("remote plaintext WebSocket requires allow_insecure_ws=true")
        if self.mode == "reverse" and not loopback(self.listen_host) and not self.allow_insecure_ws:
            raise ValueError("non-loopback listener requires explicit allow_insecure_ws=true")
        if not self.ws_path.startswith("/") or "?" in self.ws_path or "#" in self.ws_path:
            raise ValueError("ws_path must be an absolute URL path")
        if self.reconnect_max < self.reconnect_min:
            raise ValueError("reconnect_max must be >= reconnect_min")
        if self.media.inline_max_bytes * 4 // 3 + 16384 > self.ws_max_bytes:
            raise ValueError("ws_max_bytes must fit base64-encoded inline_max_bytes plus overhead")
        if not self.allow_all_users and not set(self.admins).issubset(self.allowed_users):
            raise ValueError("admins must also appear in allowed_users")
        return self
