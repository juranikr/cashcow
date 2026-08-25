from __future__ import annotations

import unittest
from unittest.mock import patch

from browser_client import RemoteBrowserClient, compact_observation


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url: str, *, json: dict):
        self.posts.append((url, json))
        return FakeResponse(
            {
                "sessionId": "00000000-0000-4000-8000-000000000001",
                "securityBoundary": "isolated-public-https-read-only-worker",
                "observation": {"observationVersion": 1, "url": "https://example.com/"},
            }
        )

    async def delete(self, url: str):
        self.deleted.append(url)
        return FakeResponse({"closed": True})


class RemoteBrowserClientContractTests(unittest.IsolatedAsyncioTestCase):
    def test_compact_observation_preserves_server_authoritative_action_metadata(self):
        compact = compact_observation(
            {
                "interactiveElements": [
                    {
                        "ref": "cc-1",
                        "tag": "summary",
                        "disclosureKind": "details",
                        "formAction": "https://example.com/search",
                        "download": True,
                    }
                ]
            }
        )
        element = compact["interactiveElements"][0]
        self.assertEqual(element["disclosureKind"], "details")
        self.assertEqual(element["formAction"], "https://example.com/search")
        self.assertTrue(element["download"])

    async def test_open_session_sends_redacted_bounded_objective(self):
        fake = FakeHttpClient()

        async def planner(_observation: dict, _step: int) -> dict:
            return {"action": "done", "summary": "done"}

        with patch("browser_client.httpx.AsyncClient", return_value=fake) as client_factory:
            await RemoteBrowserClient("http://browser-worker").run(
                "https://example.com/",
                "x" * 900,
                planner,
            )

        _, client_options = client_factory.call_args
        self.assertFalse(client_options["trust_env"])
        self.assertFalse(client_options["follow_redirects"])

        self.assertEqual(
            fake.posts[0],
            (
                "http://browser-worker/sessions",
                {"url": "https://example.com/", "objective": "x" * 800},
            ),
        )
        self.assertEqual(
            fake.deleted,
            ["http://browser-worker/sessions/00000000-0000-4000-8000-000000000001"],
        )


if __name__ == "__main__":
    unittest.main()
