import asyncio
import socket
import unittest
from unittest.mock import patch

from policy import (
    BrowserPolicyError,
    allowed_resource_scheme,
    has_credential_headers,
    has_credential_query,
    is_risky_label,
    is_risky_url,
    is_safe_http_method,
    is_sensitive_field,
    validate_public_url,
    without_credential_headers,
)


class BrowserPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_non_https_userinfo_and_nonstandard_ports(self):
        for url in (
            "http://example.com", "file:///etc/passwd", "https://user:pass@example.com",
            "https://example.com:8443", "https://localhost", "https://metadata.google.internal",
            "https://example.com:not-a-port", "https://example.com:99999",
            "https://example.com\\@127.0.0.1/", "https://%31%32%37.0.0.1/",
            "https://bad_host.example/", "https://-bad.example/", "https://bad-.example/",
            "https://example.com/line\nbreak", "https://[invalid/",
            "https://[2606:4700:4700::1111%25lo]/",
        ):
            with self.assertRaises(BrowserPolicyError, msg=url):
                await validate_public_url(url)

    async def test_blocks_private_mixed_dns_and_metadata(self):
        private_records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]
        mixed_records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.170.2", 443)),
        ]
        for records in (private_records, mixed_records):
            with patch("socket.getaddrinfo", return_value=records):
                with self.assertRaises(BrowserPolicyError):
                    await validate_public_url("https://example.com")

    async def test_blocks_all_non_global_ip_classes_and_mapped_ipv4(self):
        for url in (
            "https://127.0.0.1", "https://10.0.0.1", "https://169.254.169.254",
            "https://224.0.0.1", "https://0.0.0.0", "https://[::1]",
            "https://[fe80::1]", "https://[::ffff:127.0.0.1]",
            "https://[64:ff9b::7f00:1]",
        ):
            with self.assertRaises(BrowserPolicyError, msg=url):
                await validate_public_url(url)

    async def test_rejects_entire_dns_answer_if_one_address_is_private(self):
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        with patch("socket.getaddrinfo", return_value=records):
            with self.assertRaises(BrowserPolicyError):
                await validate_public_url("https://rebind.example")

    async def test_accepts_public_https_and_normalizes_fragment(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=records):
            result = await validate_public_url("https://Example.com/path?q=1#private")
        self.assertEqual(result.url, "https://example.com/path?q=1")
        self.assertEqual(result.resolved_ips, ("93.184.216.34",))

    def test_blocks_high_impact_labels_and_sensitive_fields(self):
        self.assertTrue(is_risky_label("구매 후 결제"))
        self.assertTrue(is_risky_label("Delete account"))
        self.assertFalse(is_risky_label("제품 상세 보기"))
        self.assertTrue(is_sensitive_field({"name": "password", "type": "text"}))
        self.assertFalse(is_sensitive_field({"name": "q", "type": "search", "placeholder": "검색"}))

    def test_blocks_obvious_state_change_and_download_urls(self):
        for url in (
            "https://example.com/account/delete",
            "https://example.com/cart/add-to-cart?id=1",
            "https://example.com/file/download?id=1",
            "https://example.com/?action=unsubscribe",
            "https://example.com/?can_delete=1",
            "https://example.com/%EC%A3%BC%EB%AC%B8/confirm",
            "https://example.com/%2564%2565%256c%2565%2574%2565",
        ):
            self.assertTrue(is_risky_url(url), url)
        for url in (
            "https://example.com/articles/123",
            "https://example.com/search?q=public+facts",
            "https://example.com/products",
        ):
            self.assertFalse(is_risky_url(url), url)

    def test_detects_credentials_in_query_without_flagging_search_text(self):
        for url in (
            "https://example.com/?token=secret",
            "https://example.com/?api_key=secret",
            "https://example.com/?x-amz-signature=secret",
            "https://example.com/?refresh_token=secret",
            "https://example.com/?%2574%256f%256b%2565%256e=secret",
        ):
            self.assertTrue(has_credential_query(url), url)
        self.assertFalse(has_credential_query("https://example.com/search?q=buy+security+keys"))

    def test_only_safe_http_methods_and_resource_schemes_are_allowed(self):
        self.assertTrue(is_safe_http_method("get"))
        self.assertTrue(is_safe_http_method("HEAD"))
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"):
            self.assertFalse(is_safe_http_method(method))
        self.assertTrue(allowed_resource_scheme("https://example.com/a"))
        self.assertTrue(allowed_resource_scheme("data:text/plain,ok"))
        self.assertFalse(allowed_resource_scheme("http://example.com"))
        self.assertFalse(allowed_resource_scheme("file:///etc/passwd"))

    def test_removes_ambient_credentials_case_insensitively(self):
        cleaned = without_credential_headers(
            {
                "Cookie": "session=secret",
                "AUTHORIZATION": "Bearer secret",
                "Proxy-Authorization": "Basic secret",
                "X-Api-Key": "secret",
                "X-Custom-Token": "secret",
                "Accept": "text/html",
            }
        )
        self.assertEqual(cleaned, {"Accept": "text/html"})
        self.assertTrue(has_credential_headers({"X-Goog-Api-Key": "secret"}))
        self.assertTrue(has_credential_headers({"X-Custom-Token": "secret"}))
        self.assertFalse(has_credential_headers({"Accept": "text/html"}))


if __name__ == "__main__":
    unittest.main()
