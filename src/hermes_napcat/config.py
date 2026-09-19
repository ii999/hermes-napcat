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
