import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from main import (
        BROWSER_HARDENING_SCRIPT,
        MAX_NETWORK_REQUESTS,
        Worker,
        health,
        present_forbidden_worker_environment,
        search_navigation_url,
    )
    from policy import BrowserPolicyError, ValidatedUrl
    IMPORT_ERROR = None
except ModuleNotFoundError as error:
    if error.name not in {"fastapi", "pydantic", "playwright"}:
        raise
    IMPORT_ERROR = error


class FakeRequest:
    def __init__(
        self,
        page,
        *,
        url="https://example.com/",
        method="GET",
        headers=None,
        resource_type="document",
        navigation=False,
        redirected_from=None,
        post_data=None,
    ):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.post_data = post_data
        self.frame = getattr(page, "main_frame", SimpleNamespace(page=page))
        self._headers = headers or {}
        self._navigation = navigation
        self.redirected_from = redirected_from

    def is_navigation_request(self):
        return self._navigation

    async def all_headers(self):
        return self._headers


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.aborted = None
        self.continued_headers = None

    async def abort(self, reason):
        self.aborted = reason

    async def continue_(self, *, headers=None):
        self.continued_headers = headers


def fake_session(*, network_enabled=True):
    page = SimpleNamespace()
    page.main_frame = SimpleNamespace(page=page)
    return SimpleNamespace(
        id="test-session",
        page=page,
        network_enabled=network_enabled,
        main_navigation_seen=False,
        attempted_requests=0,
        network_requests=0,
        blocked_request_count=0,
        blocked_requests=deque(maxlen=20),
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"browser dependencies unavailable: {IMPORT_ERROR}")
class WorkerRoutePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_verifiable_fail_closed_contract(self):
        fake_worker = SimpleNamespace(
            browser=SimpleNamespace(is_connected=lambda: True),
            proxy=SimpleNamespace(running=True, allowed_connections=3, blocked_connections=2),
            sessions={},
        )
        with (
            patch("main.worker", fake_worker),
            patch.dict("main.os.environ", {"BROWSER_WORKER_BUILD_SHA": "abc123"}, clear=True),
        ):
            payload = await health()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["buildSha"], "abc123")
        self.assertTrue(payload["credentialIsolation"])
        self.assertFalse(payload["forbiddenCredentialEnvironmentPresent"])
        self.assertTrue(payload["chromiumSandboxRequired"])
        self.assertTrue(payload["publicHttpsOnly"])
        self.assertTrue(payload["safeMethodsOnly"])

    async def test_start_refuses_credential_bearing_environment_before_proxy_or_browser(self):
        worker = Worker()
        with patch.dict("main.os.environ", {"CUSTOM_PRIVATE_TOKEN": "must-not-reach-browser"}, clear=True):
            self.assertEqual(present_forbidden_worker_environment(), ("CUSTOM_PRIVATE_TOKEN",))
            with self.assertRaisesRegex(RuntimeError, "CUSTOM_PRIVATE_TOKEN"):
                await worker.start()
        self.assertFalse(worker.proxy.running)
        self.assertIsNone(worker.playwright)
        self.assertIsNone(worker.browser)

    async def test_blocks_every_stateful_method_before_dns_or_network(self):
        worker = Worker()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"):
            session = fake_session()
            route = FakeRoute(FakeRequest(session.page, method=method, post_data="payload"))
            with patch("main.validate_public_url", new=AsyncMock()) as validate:
                await worker._route(session, route)
            self.assertEqual(route.aborted, "blockedbyclient", method)
            self.assertIsNone(route.continued_headers)
            validate.assert_not_awaited()

    async def test_blocks_cookie_authorization_and_app_tokens(self):
        worker = Worker()
        for header in (
            "cookie", "Authorization", "proxy-authorization", "x-api-key",
            "x-auth-token", "X-Custom-Token",
        ):
            session = fake_session()
            route = FakeRoute(FakeRequest(session.page, headers={header: "secret"}))
            with patch("main.validate_public_url", new=AsyncMock()) as validate:
                await worker._route(session, route)
            self.assertEqual(route.aborted, "blockedbyclient", header)
            validate.assert_not_awaited()

    async def test_blocks_protocol_upgrade_before_dns_or_network(self):
        worker = Worker()
        for headers in (
            {"upgrade": "websocket"},
            {"connection": "keep-alive, Upgrade"},
            {"sec-websocket-key": "opaque"},
        ):
            session = fake_session()
            route = FakeRoute(FakeRequest(session.page, headers=headers))
            with patch("main.validate_public_url", new=AsyncMock()) as validate:
                await worker._route(session, route)
            self.assertEqual(route.aborted, "blockedbyclient")
            validate.assert_not_awaited()

    async def test_request_attempt_budget_is_reserved_before_dns(self):
        worker = Worker()
        session = fake_session()
        session.attempted_requests = MAX_NETWORK_REQUESTS
        route = FakeRoute(FakeRequest(session.page))
        with patch("main.validate_public_url", new=AsyncMock()) as validate:
            await worker._route(session, route)
        self.assertEqual(route.aborted, "blockedbyclient")
        validate.assert_not_awaited()

    async def test_proxy_grant_failure_aborts_instead_of_falling_through(self):
        worker = Worker()
        session = fake_session()
        route = FakeRoute(FakeRequest(session.page))
        validated = ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",))
        with (
            patch("main.validate_public_url", new=AsyncMock(return_value=validated)),
            patch.object(worker.proxy, "authorize", side_effect=BrowserPolicyError("grant budget")),
        ):
            await worker._route(session, route)
        self.assertEqual(route.aborted, "blockedbyclient")
        self.assertIsNone(route.continued_headers)

    async def test_dns_completion_cannot_reopen_a_frozen_session(self):
        worker = Worker()
        session = fake_session()
        route = FakeRoute(FakeRequest(session.page))
        validated = ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",))

        async def freeze_during_dns(_url: str):
            session.network_enabled = False
            return validated

        with (
            patch("main.validate_public_url", new=AsyncMock(side_effect=freeze_during_dns)),
            patch.object(worker.proxy, "authorize") as authorize,
        ):
            await worker._route(session, route)
        self.assertEqual(route.aborted, "blockedbyclient")
        self.assertIsNone(route.continued_headers)
        authorize.assert_not_called()

    async def test_final_url_validation_runs_only_after_offline_and_revoke(self):
        worker = Worker()
        context = SimpleNamespace(set_offline=AsyncMock())
        page = SimpleNamespace(
            url="https://example.com/final",
            goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        )
        session = SimpleNamespace(
            id="session-1",
            context=context,
            page=page,
            network_enabled=False,
            last_status=None,
        )
        validated = ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",))
        validation_calls = 0

        async def validate_after_freeze(_url: str):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                self.assertFalse(session.network_enabled)
                context.set_offline.assert_awaited_with(True)
                worker.proxy.revoke.assert_awaited_with(session.id)
            return validated

        with (
            patch.object(worker, "_replace_isolated_context", new=AsyncMock()),
            patch.object(worker, "_validate_public_url", new=AsyncMock(side_effect=validate_after_freeze)),
            patch.object(worker.proxy, "revoke", new=AsyncMock()),
        ):
            await worker._navigate(session, "https://example.com/")
        self.assertEqual(validation_calls, 2)

    async def test_allows_only_validated_credential_free_get(self):
        worker = Worker()
        session = fake_session()
        route = FakeRoute(FakeRequest(session.page, headers={"accept": "text/html"}))
        validated = ValidatedUrl("https://example.com/", "example.com", ("93.184.216.34",))
        with patch("main.validate_public_url", new=AsyncMock(return_value=validated)) as validate:
            await worker._route(session, route)
        validate.assert_awaited_once_with("https://example.com/")
        self.assertIsNone(route.aborted)
        self.assertEqual(route.continued_headers, {"accept": "text/html"})
        self.assertEqual(session.network_requests, 1)

    async def test_blocks_network_when_frozen_and_non_https_navigation(self):
        worker = Worker()
        frozen = fake_session(network_enabled=False)
        frozen_route = FakeRoute(FakeRequest(frozen.page))
        await worker._route(frozen, frozen_route)
        self.assertEqual(frozen_route.aborted, "blockedbyclient")

        data_session = fake_session()
        data_route = FakeRoute(
            FakeRequest(data_session.page, url="data:text/html,hello", navigation=True)
        )
        await worker._route(data_session, data_route)
        self.assertEqual(data_route.aborted, "blockedbyclient")

    async def test_allows_server_redirect_but_blocks_scripted_second_main_navigation(self):
        worker = Worker()
        validated = ValidatedUrl("https://example.com/next", "example.com", ("93.184.216.34",))

        redirected_session = fake_session()
        redirected_session.main_navigation_seen = True
        redirected_route = FakeRoute(
            FakeRequest(
                redirected_session.page,
                url="https://example.com/next",
                navigation=True,
                redirected_from=object(),
            )
        )
        with patch("main.validate_public_url", new=AsyncMock(return_value=validated)):
            await worker._route(redirected_session, redirected_route)
        self.assertIsNone(redirected_route.aborted)

        scripted_session = fake_session()
        scripted_session.main_navigation_seen = True
        scripted_route = FakeRoute(
            FakeRequest(scripted_session.page, url="https://example.com/next", navigation=True)
        )
        with patch("main.validate_public_url", new=AsyncMock()) as validate:
            await worker._route(scripted_session, scripted_route)
        self.assertEqual(scripted_route.aborted, "blockedbyclient")
        validate.assert_not_awaited()

    async def test_blocks_streaming_and_unexpected_popup_resources(self):
        worker = Worker()
        streaming = fake_session()
        event_route = FakeRoute(FakeRequest(streaming.page, resource_type="eventsource"))
        await worker._route(streaming, event_route)
        self.assertEqual(event_route.aborted, "blockedbyclient")

        for navigation in (True, False):
            popup_session = fake_session()
            popup_route = FakeRoute(FakeRequest(object(), navigation=navigation))
            await worker._route(popup_session, popup_route)
            self.assertEqual(popup_route.aborted, "blockedbyclient")

    async def test_blocks_obvious_state_change_get_before_dns(self):
        worker = Worker()
        for url in (
            "https://example.com/account/delete",
            "https://example.com/public?access_token=secret",
        ):
            session = fake_session()
            route = FakeRoute(FakeRequest(session.page, url=url))
            with patch("main.validate_public_url", new=AsyncMock()) as validate:
                await worker._route(session, route)
            self.assertEqual(route.aborted, "blockedbyclient")
            validate.assert_not_awaited()


@unittest.skipIf(IMPORT_ERROR is not None, f"browser dependencies unavailable: {IMPORT_ERROR}")
class SafeActionConstructionTests(unittest.TestCase):
    def test_search_builds_same_origin_get_without_hidden_query_fields(self):
        target = search_navigation_url(
            {"name": "q", "formAction": "https://example.com/search?csrf=hidden"},
            "public facts",
            "https://example.com/article",
        )
        self.assertEqual(target, "https://example.com/search?q=public+facts")

    def test_search_rejects_cross_origin_or_missing_field(self):
        with self.assertRaises(BrowserPolicyError):
            search_navigation_url(
                {"name": "q", "formAction": "https://attacker.example/search"},
                "facts",
                "https://example.com/",
            )
        with self.assertRaises(BrowserPolicyError):
            search_navigation_url(
                {"name": "", "formAction": "https://example.com/search"},
                "facts",
                "https://example.com/",
            )

    def test_init_script_disables_bypass_apis_and_form_submission(self):
        for term in (
            "WebSocket", "WebTransport", "EventSource", "RTCPeerConnection",
            "sendBeacon", "HTMLFormElement.prototype.submit", "SharedWorker",
            "stopImmediatePropagation",
        ):
            self.assertIn(term, BROWSER_HARDENING_SCRIPT)


if __name__ == "__main__":
    unittest.main()
