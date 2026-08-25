import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, patch

from egress import (
    ConnectTarget,
    PinnedHttpsProxy,
    ProxyProtocolError,
    connect_authority_url,
    validate_connect_authority,
)
from policy import BrowserPolicyError, ValidatedUrl


class ConnectAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_only_explicit_https_443_authorities(self):
        self.assertEqual(connect_authority_url("example.com:443"), "https://example.com:443/")
        self.assertEqual(connect_authority_url("[2606:4700:4700::1111]:443"), "https://[2606:4700:4700::1111]:443/")
        for authority in (
            "", "example.com", "example.com:80", "user@example.com:443",
            "example.com:443/path", "example.com:443?query", "example.com:443 bad",
            "example.com:99999",
        ):
            with self.assertRaises((ProxyProtocolError, BrowserPolicyError), msg=authority):
                connect_authority_url(authority)

    async def test_validation_result_is_preserved_as_pinned_target(self):
        validator = AsyncMock(
            return_value=ValidatedUrl(
                "https://example.com/", "example.com", ("93.184.216.34", "2606:4700::1111")
            )
        )
        target = await validate_connect_authority("example.com:443", validator)
        validator.assert_awaited_once_with("https://example.com:443/")
        self.assertEqual(target.hostname, "example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.resolved_ips, ("93.184.216.34", "2606:4700::1111"))


class PinnedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_connection_uses_validated_ip_not_hostname(self):
        proxy = PinnedHttpsProxy()
        fake_reader = object()
        fake_writer = object()
        with patch("egress.asyncio.open_connection", new=AsyncMock(return_value=(fake_reader, fake_writer))) as connect:
            result = await proxy._open_pinned_target(
                ConnectTarget("attacker-controlled.example", 443, ("93.184.216.34",))
            )
        self.assertEqual(result, (fake_reader, fake_writer))
        connect.assert_awaited_once_with("93.184.216.34", 443, family=socket.AF_INET)

    async def test_proxy_revalidates_pinned_ips_before_any_connection(self):
        proxy = PinnedHttpsProxy()
        with patch("egress.asyncio.open_connection", new=AsyncMock()) as connect:
            with self.assertRaises(BrowserPolicyError):
                await proxy._open_pinned_target(
                    ConnectTarget("example.com", 443, ("127.0.0.1",))
                )
        connect.assert_not_awaited()

        with self.assertRaises(BrowserPolicyError):
            proxy.authorize(
                "session-1",
                ValidatedUrl("https://example.com/", "example.com", ("169.254.169.254",)),
            )
        with self.assertRaises(BrowserPolicyError):
            proxy.authorize(
                "session-1",
                ValidatedUrl("https://attacker.example/", "example.com", ("93.184.216.34",)),
            )

    async def test_proxy_rejects_plain_http_forward_requests(self):
        proxy = PinnedHttpsProxy()
        await proxy.start()
        try:
            parts = proxy.url.rsplit(":", 1)
            reader, writer = await asyncio.open_connection("127.0.0.1", int(parts[1]))
            writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            self.assertTrue(response.startswith(b"HTTP/1.1 403"), response)
            self.assertEqual(proxy.blocked_connections, 1)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    async def test_proxy_rejects_connect_without_route_grant(self):
        proxy = PinnedHttpsProxy()
        await proxy.start()
        try:
            parts = proxy.url.rsplit(":", 1)
            reader, writer = await asyncio.open_connection("127.0.0.1", int(parts[1]))
            writer.write(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            self.assertTrue(response.startswith(b"HTTP/1.1 403"), response)
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    async def test_connect_consumes_exact_route_grant_without_second_dns_lookup(self):
        proxy = PinnedHttpsProxy()
        proxy.authorize(
            "session-1",
            ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",)),
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        reader.feed_eof()
        with patch("egress.validate_public_url", new=AsyncMock()) as dns_validator:
            target = await proxy._read_connect_target(reader)
        dns_validator.assert_not_awaited()
        self.assertEqual(target.resolved_ips, ("93.184.216.34",))
        self.assertEqual(target.session_id, "session-1")
        with self.assertRaises(ProxyProtocolError):
            await proxy._read_connect_target(self._connect_reader("example.com:443"))

    async def test_revoke_removes_unused_grants(self):
        proxy = PinnedHttpsProxy()
        proxy.authorize(
            "session-to-close",
            ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",)),
        )
        await proxy.revoke("session-to-close")
        with self.assertRaises(ProxyProtocolError):
            await proxy._read_connect_target(self._connect_reader("example.com:443"))

    async def test_revoke_invalidates_grant_consumed_while_tcp_connect_is_pending(self):
        proxy = PinnedHttpsProxy()
        proxy.authorize(
            "session-to-freeze",
            ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",)),
        )
        target = await proxy._read_connect_target(self._connect_reader("example.com:443"))
        proxy._assert_current_grant(target)
        await proxy.revoke("session-to-freeze")
        with self.assertRaises(ProxyProtocolError):
            proxy._assert_current_grant(target)

        proxy.authorize(
            "session-to-freeze",
            ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",)),
        )
        replacement = await proxy._read_connect_target(self._connect_reader("example.com:443"))
        self.assertNotEqual(target.grant_epoch, replacement.grant_epoch)
        proxy._assert_current_grant(replacement)

    @staticmethod
    def _connect_reader(authority: str) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
        )
        reader.feed_eof()
        return reader


if __name__ == "__main__":
    unittest.main()
