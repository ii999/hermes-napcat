"""Bounded media I/O with DNS-time SSRF checks and explicit shared-path mappings."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import os
import re
import socket
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from .config import MediaSettings

_OWN_FILE = re.compile(r"napcat_[0-9a-f]{32}\.[a-z0-9]+(?:\.part)?\Z")
_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg", "audio/amr": ".amr", "audio/silk": ".silk",
    "video/mp4": ".mp4", "application/pdf": ".pdf", "text/plain": ".txt",
}


class MediaError(RuntimeError):
    pass


def origin(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port in (None, 80 if parsed.scheme == "http" else 443):
        return f"{parsed.scheme.lower()}://{host}"
    return f"{parsed.scheme.lower()}://{host}:{port}"


def public_ip(text: str) -> bool:
    address = ipaddress.ip_address(text)
    mapped = getattr(address, "ipv4_mapped", None)
    address = mapped or address
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_unspecified)


class SafeResolver(AbstractResolver):
    """Validate the addresses aiohttp will actually connect to; no check-then-resolve gap."""
    def __init__(self, private_hosts: set[str]):
        self._resolver = DefaultResolver()
        self._private_hosts = private_hosts

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        results = await self._resolver.resolve(host, port, family)
        if host.lower().rstrip(".") not in self._private_hosts:
            if not results or any(not public_ip(item["host"]) for item in results):
                raise MediaError("media hostname resolves to a non-public address")
        return results

    async def close(self) -> None:
        await self._resolver.close()


@dataclass(frozen=True)
class Downloaded:
    path: Path
    mime: str
    size: int


class MediaStore:
    def __init__(self, config: MediaSettings, root: Path):
        self.config = config
        root = root.expanduser().absolute()
        if root.is_symlink():
            raise MediaError("media cache must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        self._policies = {name: config.download_policy(name) for name in ("inbound", "outbound")}
        self._private_origins = {
            name: {origin(value) for value in policy.trusted_private_origins}
            for name, policy in self._policies.items()
        }
        self._private_hosts = {
            name: {urlsplit(value).hostname for value in origins}
            for name, origins in self._private_origins.items()
        }
        self.shared_root: Path | None = None
        if config.shared_cache_dir is not None:
            shared = config.shared_cache_dir.expanduser().absolute()
            if shared.is_symlink():
                raise MediaError("shared media cache must not be a symlink")
            shared.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.shared_root = shared.resolve()
            if not any(self.shared_root.is_relative_to(m.hermes.expanduser().resolve())
                       for m in config.shared_paths):
                raise MediaError("shared media cache escapes its configured mapping")
        # Covers synchronous local-send entry points as well as asynchronous cache I/O.
        self._storage_lock = threading.RLock()
        self._reserved_bytes = 0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._outbound_session: aiohttp.ClientSession | None = None

    def validate_url(self, url: str, *, direction: str = "inbound") -> str:
        policy = self._policies[direction]
        if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() for c in url):
            raise MediaError("invalid media URL")
        try:
            parsed = urlsplit(url)
            current_origin = origin(url)
        except ValueError as exc:
            raise MediaError("invalid media URL") from exc
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or
                parsed.password or parsed.fragment):
            raise MediaError("media URL must use http(s), without userinfo or fragment")
        host = parsed.hostname.lower().rstrip(".")
        trusted = current_origin in self._private_origins[direction]
        if host in self._private_hosts[direction] and not trusted:
            raise MediaError("private media service origin does not match its configured origin")
        if not trusted and policy.mode == "allowlist" and host not in policy.allowed_hosts:
            raise MediaError("media host is not allowlisted")
        with contextlib.suppress(ValueError):
            if not trusted and not public_ip(host):
                raise MediaError("non-public media address is forbidden")
        return url

    def _owned_files(self):
        for root in dict.fromkeys((self.root, self.shared_root)):
            if root is None:
                continue
            for path in root.iterdir():
                if not _OWN_FILE.fullmatch(path.name):
                    continue
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    yield path, metadata

    def _prune_and_usage(self) -> int:
        with self._storage_lock:
            usage = 0
            cutoff = time.time() - self.config.cache_ttl_seconds
            for path, metadata in self._owned_files():
                if metadata.st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                else:
                    usage += metadata.st_size
            return usage

    async def download(self, url: str, *, kind: str, direction: str = "inbound") -> Downloaded:
        if not self.config.enabled:
            raise MediaError("media download is disabled")
        if direction not in self._policies:
            raise MediaError("unknown media download direction")
        # Hold a reservation for the entire transfer. No concurrent cache quota overcommit.
        async with self._lock:
            with self._storage_lock:
                usage = self._prune_and_usage()
                if usage + self.config.max_bytes > self.config.cache_max_bytes:
                    raise MediaError("media cache quota reached; existing recent files are retained")
                self._reserved_bytes = self.config.max_bytes
            try:
                attribute = "_session" if direction == "inbound" else "_outbound_session"
                if getattr(self, attribute) is None:
                    connector = aiohttp.TCPConnector(
                        resolver=SafeResolver(self._private_hosts[direction]),
                        use_dns_cache=False, limit=4)
                    session = aiohttp.ClientSession(
                        connector=connector, trust_env=False, auto_decompress=False,
                        timeout=aiohttp.ClientTimeout(total=self.config.timeout),
                    )
                    setattr(self, attribute, session)
                async with asyncio.timeout(self.config.timeout):
                    return await self._download(url, kind, direction)
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                raise MediaError(f"media transfer failed ({type(exc).__name__})") from exc
            finally:
                with self._storage_lock:
                    self._reserved_bytes = 0

    async def _download(self, url: str, kind: str, direction: str = "inbound") -> Downloaded:
        session = self._session if direction == "inbound" else self._outbound_session
        assert session is not None
        for _ in range(4):
            self.validate_url(url, direction=direction)
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise MediaError("redirect did not include a target")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise MediaError(f"media server returned HTTP {response.status}")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise MediaError("encoded media responses are refused")
                if response.content_length and response.content_length > self.config.max_bytes:
                    raise MediaError("media exceeds configured byte limit")
                mime = response.headers.get("Content-Type", "application/octet-stream").split(";")[0].lower()
                expected = {"image": "image/", "record": "audio/", "video": "video/"}.get(kind)
                if expected and not mime.startswith(expected) and mime != "application/octet-stream":
                    raise MediaError("media MIME type does not match its segment")
                suffix = _EXTENSIONS.get(mime, ".bin")
                name = f"napcat_{uuid.uuid4().hex}{suffix}"
                temporary = self.root / (name + ".part")
                destination = self.root / name
                size = 0
                head = b""
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > self.config.max_bytes:
                                raise MediaError("media exceeds configured byte limit")
                            if len(head) < 16:
                                head = (head + chunk)[:16]
                            output.write(chunk)
                    if not size:
                        raise MediaError("media response was empty")
                    if kind == "image":
                        detected = self._image_mime(head)
                        if not detected:
                            raise MediaError("unsupported or invalid image bytes")
                        mime = detected
                        destination = self.root / f"napcat_{uuid.uuid4().hex}{_EXTENSIONS[mime]}"
                    os.replace(temporary, destination)
                    return Downloaded(destination, mime, size)
                finally:
                    temporary.unlink(missing_ok=True)
        raise MediaError("too many media redirects")

    @staticmethod
    def _image_mime(head: bytes) -> str | None:
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return "image/webp"
        return None

    def local_path(self, path: str) -> Path:
        supplied = Path(path).expanduser()
        if not supplied.is_absolute():
            raise MediaError("outbound media path must be absolute")
        candidate = supplied.resolve(strict=True)
        roots = [*self.config.outbound_roots, *(m.hermes for m in self.config.shared_paths)]
        if not any(candidate.is_relative_to(root.expanduser().resolve()) for root in roots):
            raise MediaError("outbound file is outside configured media roots")
        if not candidate.is_file():
            raise MediaError("outbound media is not a regular file")
        return candidate

    def shared_path(self, path: str) -> str:
        candidate = self.local_path(path)
        # Most specific mapping wins when an operator has nested mounts.
        mappings = sorted(self.config.shared_paths, key=lambda m: len(str(m.hermes)), reverse=True)
        for mapping in mappings:
            root = mapping.hermes.expanduser().resolve()
            if candidate.is_relative_to(root):
                return mapping.napcat.rstrip("/\\") + "/" + candidate.relative_to(root).as_posix()
        raise MediaError("this operation requires a configured shared_paths mapping")

    @staticmethod
    def _file_uri(remote: str) -> str:
        remote = remote.replace("\\", "/")
        return "file://" + ("" if remote.startswith("/") else "/") + quote(remote, safe="/:")

    def _mapped_reference(self, candidate: Path) -> str | None:
        try:
            return self._file_uri(self.shared_path(str(candidate)))
        except MediaError:
            return None

    def _reference(self, candidate: Path, size: int) -> str:
        with self._storage_lock:
            mapped = self._mapped_reference(candidate)
            if mapped is not None:
                return mapped
            if self.shared_root is not None:
                # Keep staging ownership narrow; never expose the entire inbound cache.
                if self.shared_root.is_symlink() or self.shared_root.resolve() != self.shared_root:
                    raise MediaError("shared media cache changed or became a symlink")
                usage = self._prune_and_usage()
                if usage + self._reserved_bytes + size > self.config.cache_max_bytes:
                    raise MediaError("shared media cache quota reached")
                suffix = candidate.suffix.lower()
                if suffix not in _EXTENSIONS.values():
                    suffix = ".bin"
                name = f"napcat_{uuid.uuid4().hex}{suffix}"
                temporary = self.shared_root / (name + ".part")
                destination = self.shared_root / name
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    copied = 0
                    with os.fdopen(descriptor, "wb") as output, candidate.open("rb") as source:
                        while chunk := source.read(min(64 * 1024, size + 1 - copied)):
                            copied += len(chunk)
                            if copied > size:
                                raise MediaError("media changed while staging")
                            output.write(chunk)
                    if copied != size:
                        raise MediaError("media changed while staging")
                    os.replace(temporary, destination)
                    mapped = self._mapped_reference(destination)
                    if mapped is None:
                        raise MediaError("shared media cache no longer has a valid mapping")
                    return mapped
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
                finally:
                    temporary.unlink(missing_ok=True)
            with candidate.open("rb") as stream:
                value = stream.read(self.config.inline_max_bytes + 1)
            if len(value) > self.config.inline_max_bytes:
                raise MediaError(
                    "media exceeds inline upload limit; configure shared_cache_dir/shared_paths "
                    "or raise inline_max_bytes together with ws_max_bytes")
            if len(value) != size:
                raise MediaError("media changed before upload")
            return "base64://" + base64.b64encode(value).decode("ascii")

    def outbound_reference(self, path: str, *, kind: str | None = None,
                           max_bytes: int | None = None) -> str:
        candidate = self.local_path(path)
        size = candidate.stat().st_size
        limits = [limit for limit in (max_bytes, self.config.max_bytes if kind == "image" else None)
                  if limit is not None]
        if limits and size > min(limits):
            raise MediaError("local media exceeds configured byte limit")
        if kind == "image":
            with candidate.open("rb") as stream:
                if self._image_mime(stream.read(16)) is None:
                    raise MediaError("unsupported or invalid image bytes")
        return self._reference(candidate, size)

    def validate_cached(self, downloaded: Downloaded) -> Path:
        """Validate one explicit cache object; a cache path alone grants no access."""
        candidate = downloaded.path.resolve(strict=True)
        if (downloaded.path.is_symlink() or candidate.parent != self.root
                or not _OWN_FILE.fullmatch(candidate.name) or not candidate.is_file()):
            raise MediaError("downloaded media is not owned by this cache")
        if candidate.stat().st_size != downloaded.size:
            raise MediaError("downloaded media changed before upload")
        return candidate

    def cached_reference(self, downloaded: Downloaded) -> str:
        return self._reference(self.validate_cached(downloaded), downloaded.size)

    async def close(self) -> None:
        for attribute in ("_session", "_outbound_session"):
            session = getattr(self, attribute)
            if session is not None:
                await session.close()
                setattr(self, attribute, None)
