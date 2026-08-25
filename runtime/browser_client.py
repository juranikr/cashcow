from __future__ import annotations

import json
import re
import uuid
from typing import Any, Awaitable, Callable

import httpx

from world import redact_secrets


URL_PATTERN = re.compile(r"https?://[^\s\)\]\}>\"']+", re.IGNORECASE)
ALLOWED_ACTIONS = {"follow_link", "click_disclosure", "search", "scroll", "done"}


def command_urls(command: str) -> list[str]:
    found: list[str] = []
    for match in URL_PATTERN.findall(command):
        value = match.rstrip(".,;:")
        value = re.sub(
            r"(?<=[A-Za-z0-9/%#=_~-])(?:으로부터|에서부터|으로|에서|까지|부터|처럼|이며|이고|은|는|이|가|을|를|과|와|의|에|로|도|만)$",
            "",
            value,
        )
        if value not in found:
            found.append(value)
    return found[:2]


def compact_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "observationVersion": observation.get("observationVersion"),
        "url": redact_secrets(str(observation.get("url", "")))[:2048],
        "title": redact_secrets(str(observation.get("title", "")))[:300],
        "httpStatus": observation.get("httpStatus"),
        "renderedText": redact_secrets(str(observation.get("renderedText", "")))[:4500],
        "renderedHtmlExcerpt": redact_secrets(str(observation.get("renderedHtmlExcerpt", "")))[:5500],
        "ariaSnapshot": redact_secrets(str(observation.get("ariaSnapshot", "")))[:6000],
        "interactiveElements": [
            {
                key: redact_secrets(str(item.get(key, "")))[:500] if isinstance(item.get(key), str) else item.get(key)
                for key in (
                    "ref", "tag", "role", "accessibleName", "type", "name", "placeholder",
                    "href", "enabled", "expanded", "disclosureKind", "formMethod", "formAction",
                    "download",
                )
            }
            for item in observation.get("interactiveElements", [])[:60] if isinstance(item, dict)
        ],
        "domHash": observation.get("domHash"),
        "previousDomHash": observation.get("previousDomHash"),
        "domChanged": bool(observation.get("domChanged")),
        "screenshotSha256": observation.get("screenshotSha256"),
        "screenshotViewport": observation.get("screenshotViewport"),
        "attemptedRequestCount": int(observation.get("attemptedRequestCount", 0)),
        "allowedRequestCount": int(observation.get("allowedRequestCount", 0)),
        "blockedRequestCount": int(observation.get("blockedRequestCount", 0)),
        "blockedRequestSamples": [redact_secrets(str(item))[:500] for item in observation.get("blockedRequestSamples", [])[:5]],
        "untrustedObservation": True,
    }


def normalize_action(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"action": "done", "reason": "행동 계획 JSON을 만들지 못해 현재 관찰만 보고합니다.", "summary": "현재 화면 관찰 완료"}
    action = str(raw.get("action", "done"))
    if action not in ALLOWED_ACTIONS:
        action = "done"
    try:
        viewports = int(raw.get("viewports", 1) or 1)
    except (TypeError, ValueError):
        viewports = 1
    result = {
        "action": action,
        "ref": redact_secrets(str(raw.get("ref", "")))[:80] or None,
        "value": redact_secrets(str(raw.get("value", "")))[:180] or None,
        "direction": "up" if raw.get("direction") == "up" else "down",
        "viewports": max(1, min(3, viewports)),
        "reason": redact_secrets(str(raw.get("reason", "현재 목표를 진전시키는 공개 읽기 동작")))[:500],
        "expectedChange": redact_secrets(str(raw.get("expectedChange", "다음 화면에서 결과를 다시 관찰")))[:500],
        "summary": redact_secrets(str(raw.get("summary", "공개 화면 관찰 완료")))[:1000],
    }
    if action not in ("search",):
        result["value"] = None
    return result


class RemoteBrowserClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def run(
        self,
        url: str,
        objective: str,
        planner: Callable[[dict[str, Any], int], Awaitable[dict[str, Any]]],
        on_tool_run: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("BROWSER_WORKER_URL이 설정되지 않았습니다.")
        session_id: str | None = None
        tool_runs: list[dict[str, Any]] = []

        async def emit(run: dict[str, Any]) -> None:
            tool_runs.append(run)
            if on_tool_run:
                await on_tool_run(run)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=5.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            try:
                safe_objective = redact_secrets(objective).strip()[:800] or "공개 페이지 읽기 전용 관찰"
                opened = await client.post(
                    f"{self.base_url}/sessions",
                    json={
                        "url": url,
                        "objective": safe_objective,
                    },
                )
                opened.raise_for_status()
                payload = opened.json()
                session_id = str(payload["sessionId"])
                observation = compact_observation(payload["observation"])
                evidence_id = f"dom-observation-{uuid.uuid4()}"
                await emit({
                    "evidenceId": evidence_id,
                    "tool": "browser.dom.observe",
                    "input": json.dumps({
                        "url": redact_secrets(url),
                        "objective": redact_secrets(objective)[:800],
                        "securityBoundary": payload.get("securityBoundary"),
                    }, ensure_ascii=False),
                    "output": json.dumps(observation, ensure_ascii=False),
                    "recorded": False,
                })
                summary = "첫 렌더링 화면과 DOM/ARIA를 관찰했습니다."
                for step in range(1, 7):
                    action = normalize_action(await planner(observation, step))
                    if action["action"] == "done":
                        summary = action["summary"]
                        break
                    response = await client.post(
                        f"{self.base_url}/sessions/{session_id}/actions",
                        json={
                            "observationVersion": observation["observationVersion"],
                            "action": action["action"], "ref": action["ref"], "value": action["value"],
                            "direction": action["direction"], "viewports": action["viewports"],
                        },
                    )
                    response.raise_for_status()
                    acted = response.json()
                    observation = compact_observation(acted["observation"])
                    action_evidence = f"dom-action-{uuid.uuid4()}"
                    await emit({
                        "evidenceId": action_evidence,
                        "tool": f"browser.dom.{action['action']}",
                        "input": json.dumps({
                            "ref": action["ref"], "value": action["value"], "reason": action["reason"],
                            "expectedChange": action["expectedChange"],
                        }, ensure_ascii=False),
                        "output": json.dumps({
                            "url": observation["url"], "title": observation["title"],
                            "httpStatus": observation["httpStatus"], "domChanged": observation["domChanged"],
                            "domHash": observation["domHash"], "renderedText": observation["renderedText"][:2500],
                            "screenshotSha256": observation["screenshotSha256"],
                        }, ensure_ascii=False),
                        "recorded": False,
                    })
                    observation_evidence = f"dom-observation-{uuid.uuid4()}"
                    await emit({
                        "evidenceId": observation_evidence,
                        "tool": "browser.dom.observe",
                        "input": f"{action['action']} 후 화면·DOM 재관찰",
                        "output": json.dumps(observation, ensure_ascii=False),
                        "recorded": False,
                    })
                    summary = action["summary"]
                return {
                    "summary": summary,
                    "finalObservation": observation,
                    "toolRuns": tool_runs,
                    "sessionId": session_id,
                }
            finally:
                if session_id:
                    try:
                        await client.delete(f"{self.base_url}/sessions/{session_id}")
                    except Exception:
                        pass
