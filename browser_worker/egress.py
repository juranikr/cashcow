from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from policy import (
    BrowserPolicyError,
    MAX_DNS_ADDRESSES,
    ValidatedUrl,
    assert_public_ip,
    normalize_public_https_url,
    validate_public_url,
)


MAX_PROXY_HEADER_BYTES = 16 * 1024
MAX_TUNNEL_BYTES_PER_DIRECTION = 32 * 1024 * 1024
MAX_TUNNEL_SECONDS = 80.0
MAX_CONCURRENT_TUNNELS = 24
CONNECT_TIMEOUT_SECONDS = 5.0
GRANT_TTL_SECONDS = 10.0
MAX_PENDING_GRANTS = 160


class ProxyProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectTarget:
    hostname: str
    port: int
    resolved_ips: tuple[str, ...]
    session_id: str | None = None
    grant_epoch: int | None = None


@dataclass(frozen=True)
class EgressGrant:
    session_id: str
    resolved_ips: tuple[str, ...]
    expires_at: float
    epoch: int


def connect_authority_url(authority: str) -> str:
    """Turn a strict CONNECT authority into a URL accepted by the SSRF policy."""
    if (
        not authority
        or len(authority) > 512
        or any(character in authority for character in "/\\?#")
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in authority)
    ):
        raise ProxyProtocolError("invalid CONNECT authority")
    try:
        parts = urlsplit(f"https://{authority}/")
        port = parts.port
    except ValueError as error:
        raise ProxyProtocolError("invalid CONNECT port") from error
    if not parts.hostname or parts.username or parts.password or port != 443:
        raise ProxyProtocolError("only host:443 CONNECT targets are allowed")
    return f"https://{authority}/"


async def validate_connect_authority(
    authority: str,
    validator: Callable[[str], Awaitable[ValidatedUrl]] = validate_public_url,
) -> ConnectTarget:
    validated = await validator(connect_authority_url(authority))
    return ConnectTarget(validated.hostname, 443, validated.resolved_ips)


class PinnedHttpsProxy:
    """Minimal CONNECT proxy that dials only the IPs returned by SSRF validation.

    Chromium sends TLS (including the original SNI) through the tunnel, while this
    proxy performs the actual TCP connection to a public IP granted by the
    Playwright request route. Ungranted browser background traffic is rejected,
    and no second DNS lookup creates a rebinding window.
    """

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._tunnels = asyncio.Semaphore(MAX_CONCURRENT_TUNNELS)
        self._grants: dict[str, deque[EgressGrant]] = {}
        self._active_writers: dict[str, set[asyncio.StreamWriter]] = {}
        self._session_epochs: dict[str, int] = {}
        self._next_epoch = 0
        self.allowed_connections = 0
        self.blocked_connections = 0

    @property
    def url(self) -> str:
        if not self._server or not self._server.sockets:
            raise RuntimeError("HTTPS egress proxy is not running")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    @property
    def running(self) -> bool:
        return bool(self._server and self._server.is_serving())

    async def start(self) -> None:
        if self._server:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
            limit=MAX_PROXY_HEADER_BYTES,
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server:
            server.close()
            await server.wait_closed()
        for session_id in list(self._active_writers):
            await self.revoke(session_id)
        self._grants.clear()
        self._session_epochs.clear()

    def authorize(self, session_id: str, validated: ValidatedUrl) -> None:
        """Grant one short-lived CONNECT using the exact IPs already validated."""
        try:
            _, normalized_hostname = normalize_public_https_url(validated.url)
        except BrowserPolicyError as error:
            raise BrowserPolicyError("invalid HTTPS egress grant") from error
        if (
            not session_id
            or not validated.resolved_ips
            or len(validated.resolved_ips) > MAX_DNS_ADDRESSES
            or normalized_hostname != validated.hostname
        ):
            raise BrowserPolicyError("invalid HTTPS egress grant")
        pinned_ips = tuple(assert_public_ip(raw_ip) for raw_ip in validated.resolved_ips)
        epoch = self._session_epochs.get(session_id)
        if epoch is None:
            self._next_epoch += 1
            epoch = self._next_epoch
            self._session_epochs[session_id] = epoch
        now = time.monotonic()
        pending = 0
        for hostname in list(self._grants):
            queue = self._grants[hostname]
            while queue and queue[0].expires_at <= now:
                queue.popleft()
            if not queue:
                self._grants.pop(hostname, None)
            else:
                pending += len(queue)
        if pending >= MAX_PENDING_GRANTS:
            raise BrowserPolicyError("HTTPS egress grant budget exceeded")
        self._grants.setdefault(validated.hostname, deque()).append(
            EgressGrant(session_id, pinned_ips, now + GRANT_TTL_SECONDS, epoch)
        )

    def _consume_grant(self, hostname: str) -> EgressGrant:
        now = time.monotonic()
        queue = self._grants.get(hostname)
        while queue and queue[0].expires_at <= now:
            queue.popleft()
        if not queue:
            self._grants.pop(hostname, None)
            raise ProxyProtocolError("CONNECT target was not authorized by a routed read request")
        grant = queue.popleft()
        if not queue:
            self._grants.pop(hostname, None)
        return grant

    async def revoke(self, session_id: str) -> None:
        # Removing the epoch invalidates grants that have already been consumed
        # but are still waiting for their upstream TCP connection to complete.
        self._session_epochs.pop(session_id, None)
        for hostname in list(self._grants):
            remaining = deque(
                grant for grant in self._grants[hostname] if grant.session_id != session_id
            )
            if remaining:
                self._grants[hostname] = remaining
            else:
                self._grants.pop(hostname, None)
        writers = list(self._active_writers.pop(session_id, set()))
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers),
                return_exceptions=True,
            )

    async def _read_connect_target(self, reader: asyncio.StreamReader) -> ConnectTarget:
        try:
            raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as error:
            raise ProxyProtocolError("invalid proxy request") from error
        if len(raw_headers) > MAX_PROXY_HEADER_BYTES:
            raise ProxyProtocolError("proxy headers are too large")
        try:
            request_line = raw_headers.split(b"\r\n", 1)[0].decode("ascii")
            method, authority, version = request_line.split(" ")
        except (UnicodeDecodeError, ValueError) as error:
            raise ProxyProtocolError("invalid proxy request line") from error
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProxyProtocolError("only HTTPS CONNECT is allowed")
        _, hostname = normalize_public_https_url(connect_authority_url(authority))
        grant = self._consume_grant(hostname)
        return ConnectTarget(hostname, 443, grant.resolved_ips, grant.session_id, grant.epoch)

    def _assert_current_grant(self, target: ConnectTarget) -> None:
        if (
            target.session_id is None
            or target.grant_epoch is None
            or self._session_epochs.get(target.session_id) != target.grant_epoch
        ):
            raise ProxyProtocolError("CONNECT grant was revoked while opening the tunnel")

    async def _open_pinned_target(
        self, target: ConnectTarget
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: Exception | None = None
        for raw_ip in target.resolved_ips:
            try:
                address = ipaddress.ip_address(assert_public_ip(raw_ip))
                return await asyncio.wait_for(
                    asyncio.open_connection(
                        str(address),
                        target.port,
                        family=socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                    ),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except (OSError, asyncio.TimeoutError) as error:
                last_error = error
        raise ConnectionError("no validated public address was reachable") from last_error

    @staticmethod
    async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        transferred = 0
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                return
            transferred += len(chunk)
            if transferred > MAX_TUNNEL_BYTES_PER_DIRECTION:
                raise ConnectionError("HTTPS tunnel byte limit exceeded")
            writer.write(chunk)
            await writer.drain()

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        client_to_upstream = asyncio.create_task(self._relay(client_reader, upstream_writer))
        upstream_to_client = asyncio.create_task(self._relay(upstream_reader, client_writer))
        tasks = {client_to_upstream, upstream_to_client}
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=MAX_TUNNEL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        target: ConnectTarget | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            async with self._tunnels:
                target = await self._read_connect_target(client_reader)
                upstream_reader, upstream_writer = await self._open_pinned_target(target)
                self._assert_current_grant(target)
                active = self._active_writers.setdefault(target.session_id or "", set())
                active.update((client_writer, upstream_writer))
                self.allowed_connections += 1
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await self._tunnel(client_reader, client_writer, upstream_reader, upstream_writer)
        except (BrowserPolicyError, ProxyProtocolError):
            self.blocked_connections += 1
            client_writer.write(
                b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            with contextlib.suppress(Exception):
                await client_writer.drain()
        except Exception:
            self.blocked_connections += 1
            client_writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            with contextlib.suppress(Exception):
                await client_writer.drain()
        finally:
            if target and target.session_id is not None:
                active = self._active_writers.get(target.session_id)
                if active:
                    active.discard(client_writer)
                    if upstream_writer:
                        active.discard(upstream_writer)
                    if not active:
                        self._active_writers.pop(target.session_id, None)
            if upstream_writer:
                upstream_writer.close()
                with contextlib.suppress(Exception):
                    await upstream_writer.wait_closed()
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
