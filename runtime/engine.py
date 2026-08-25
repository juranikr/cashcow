from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from browser_client import RemoteBrowserClient, command_urls
from browser_planner import BrowseTrace, ranked_public_read_actions
from cognition import (
    begin_commitment,
    complete_reflection,
    consolidate_procedure,
    make_goal_contract,
    relevant_memories,
    staff_brief,
    update_checkpoint,
    update_relationship,
)
from storage import BOARD_HISTORY_LIMIT, PALETTE, board_meta, utc_now
from world import (
    COMPUTER_STATIONS,
    HEARING_RADIUS,
    MEETING_SEATS,
    TOOL_SPOTS,
    distance,
    direction_from_delta,
    is_blocked,
    navigate_toward,
    redact_secrets,
    target_tool_from_command,
)


TOOL_LABELS = {"computer": "컴퓨터", "whiteboard": "화이트보드", "meeting-room": "회의실"}
AGENT_IDS = ("minji", "doyun", "harin", "jun")
MAX_ATTEMPTS = 3
ROLE_RESPONSIBILITIES = {
    "minji": "완료 계약·우선순위·의존성 조정",
    "doyun": "외부 출처·가설·불확실성 독립 검토",
    "harin": "사용자 관점·표현·대안 검토",
    "jun": "구현·재현 가능한 검증·운영 위험 점검",
}
KNOWLEDGE_MARKERS = (
    "공용 정보", "공유 정보", "공용 지식", "공유 지식",
    "공용정보", "공유정보", "공용지식", "공유지식",
    "공용 데이터베이스", "공유 데이터베이스", "공용데이터베이스", "공유데이터베이스",
)
KNOWLEDGE_WRITE_MARKERS = ("등록", "저장", "기록", "추가", "작성", "업데이트", "갱신")
RUNTIME_MARKERS = (
    "운영계", "운영 환경", "ECS", "Fargate", "DynamoDB", "다이나모", "서버 런타임",
    "영속 실행", "영속성", "브라우저 종료", "브라우저를 종료", "브라우저를 닫", "브라우저가 종료",
    "브라우저 창을 닫", "창을 닫", "탭을 닫", "페이지를 닫", "접속을 끊", "연결을 끊",
    "클라이언트 종료", "다른 브라우저", "다른 PC",
)
PERMISSION_DENIAL_MARKERS = (
    "권한이 없", "권한 없음", "권한 부족", "접근할 수 없", "읽을 수 없", "쓸 수 없",
    "등록할 수 없", "열람이 불가능", "접근 불가", "읽기 불가", "등록 불가", "저장 불가",
    "권한이 부여되지 않", "접근할 방법이 없", "permission denied", "not authorized", "unauthorized",
    "do not have access", "don't have access", "cannot access", "access denied", "insufficient privileges",
)
RUNTIME_EVIDENCE_MARKERS = (
    "runtime.status", "cashcow-runtime", "worldengine", "dynamodb", "aws ecs", "fargate",
    "서버 프로세스", "서버 런타임", "브라우저 연결 불필요", "browserconnectionrequired",
)


def _event(plan_id: str, event_type: str, title: str, result: str = "success", **values: Any) -> dict:
    return {
        "id": str(uuid.uuid4()), "planId": plan_id, "at": utc_now(), "type": event_type,
        "title": title, "result": result, **values,
    }


def _message(speaker_id: str, speaker: str, body: str, kind: str, heard_by: list[str], plan_id: str | None = None, origin_position: dict | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()), "speakerId": speaker_id, "speaker": speaker,
        "body": redact_secrets(body)[:3000], "at": utc_now(), "kind": kind,
        "heardBy": heard_by, "planId": plan_id, "originPosition": deepcopy(origin_position),
    }


def _ensure_player(world: dict, actor_id: str) -> dict:
    players = world.setdefault("players", {})
    if actor_id not in players:
        now = utc_now()
        players[actor_id] = {"position": {"x": 49.0, "y": 54.0}, "facing": "south", "updatedAt": now}
    return players[actor_id]


def _plan(command: str, tool: str) -> list[dict]:
    concise = command.replace("@", "").strip()[:60] or "대표 요청 수행"
    label = TOOL_LABELS[tool]
    return [
        {"id": str(uuid.uuid4()), "phase": "contract", "title": f"작업 계약: {concise}", "detail": "목표·성공 기준·증거 의무·다음 행동을 서버에 영속 확정", "status": "done"},
        {"id": str(uuid.uuid4()), "phase": "memory", "title": "관련 기억과 실패 전략 회수", "detail": "검증된 에피소드·교훈 중 현재 목표와 가까운 최대 4개만 작업 기억에 활성화", "status": "done"},
        {"id": str(uuid.uuid4()), "phase": "move", "title": f"{label}(으)로 이동", "detail": "공유 월드 좌표에서 순간이동 없이 이동", "status": "active", "tool": tool},
        {"id": str(uuid.uuid4()), "phase": "act", "title": f"{label}에서 관찰→행동→재관찰", "detail": "실제 도구 입출력과 예상·실제 변화를 체크포인트로 기록", "status": "pending", "tool": tool},
        {"id": str(uuid.uuid4()), "phase": "verify", "title": "성공 기준과 증거 의무 검증", "detail": "필수 기준이 하나라도 비면 완료하지 않고 다른 전략으로 재계획", "status": "pending"},
        {"id": str(uuid.uuid4()), "phase": "reflect", "title": "리포트·에피소드·교훈 통합", "detail": "예상과 실제 결과의 차이로 자기평가를 보정", "status": "pending"},
    ]


def _set_plan_phase(job: dict, phase: str, status: str, detail: str | None = None) -> None:
    step = next((item for item in job.get("plan", []) if item.get("phase") == phase), None)
    if not step:
        legacy = {"move": 1, "act": 2, "verify": 3, "reflect": 3}
        index = legacy.get(phase)
        if index is not None and index < len(job.get("plan", [])):
            step = job["plan"][index]
    if step:
        step["status"] = status
        if detail:
            step["detail"] = detail


def _normalized_verification(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "status": "rejected",
            "passed": False,
            "requiredChecks": [],
            "evidenceIds": [],
            "issues": ["서버 검증 결과가 누락되었습니다."],
        }
    status = str(value.get("status") or "rejected")
    issues = [redact_secrets(str(item))[:700] for item in value.get("issues", []) if str(item).strip()][:8]
    passed = bool(value.get("passed") is True and status in ("verified", "asserted") and not issues)
    if not passed and not issues:
        issues = ["완료 기준을 통과한 검증 결과가 없습니다."]
    return {
        "status": status if passed else "rejected",
        "passed": passed,
        "requiredChecks": [redact_secrets(str(item))[:500] for item in value.get("requiredChecks", []) if str(item).strip()][:20],
        "evidenceIds": list(dict.fromkeys(str(item)[:240] for item in value.get("evidenceIds", []) if str(item).strip()))[:40],
        "issues": issues,
    }


def _record_replan(job: dict, report: dict, verification: dict, now: str) -> None:
    revision = int(job.get("planRevision", 1)) + 1
    job["planRevision"] = revision
    failure_signature = " | ".join(
        verification.get("issues") or report.get("limitations") or [str(report.get("outcome", "partial"))]
    )[:1200]
    contract = job.get("goalContract")
    if isinstance(contract, dict):
        history = contract.setdefault("strategyHistory", [])
        history.append({
            "revision": revision,
            "attempt": int(job.get("attempt", 0)),
            "at": now,
            "outcome": report.get("outcome", "partial"),
            "failureSignature": failure_signature,
            "changedStrategy": "성공한 증거는 보존하고 미충족 기준만 다른 행동으로 다시 검증한다.",
        })
        contract["strategyHistory"] = history[-8:]
        contract.update({
            "status": "committed",
            "planVersion": revision,
            "nextAction": "미충족 성공 기준에 필요한 증거만 다시 관찰",
            "nextWakeAt": now,
            "blockers": list(verification.get("issues", []))[:8],
            "updatedAt": now,
        })
    _set_plan_phase(job, "move", "done")
    _set_plan_phase(job, "act", "active", f"{revision}차 계획으로 미충족 증거를 다시 관찰")
    _set_plan_phase(job, "verify", "blocked", failure_signature)
    _set_plan_phase(job, "reflect", "pending")


def _finish_goal_contract(job: dict, success: bool, verification: dict, report_id: str, now: str) -> None:
    contract = job.get("goalContract")
    if not isinstance(contract, dict):
        return
    evidence_ids = list(dict.fromkeys([*verification.get("evidenceIds", []), f"report_{report_id}"]))[:40]
    for criterion in contract.get("successCriteria", []):
        if not isinstance(criterion, dict):
            continue
        criterion["status"] = "passed" if success else "failed" if criterion.get("required", True) else "skipped"
        criterion["evidenceIds"] = evidence_ids if success else list(verification.get("evidenceIds", []))[:40]
    contract.update({
        "status": "completed" if success else "failed",
        "planVersion": int(job.get("planRevision", contract.get("planVersion", 1))),
        "nextAction": "다음 대표 요청 대기" if success else "실패 리포트와 재시도 조건 보존",
        "nextWakeAt": None,
        "blockers": [] if success else list(verification.get("issues", []))[:8],
        "updatedAt": now,
        "completedAt": now,
    })


def _participants_for(agent_id: str, tool: str, intent: dict[str, Any], command: str) -> list[str]:
    """Choose specialists deterministically while keeping the addressed agent accountable."""
    if tool == "meeting-room":
        return list(AGENT_IDS)
    selected = [agent_id]
    lowered = command.lower()
    if intent.get("needsSearch") or intent.get("needsDirectBrowser"):
        selected.append("doyun")
    if intent.get("needsCode") or intent.get("needsRuntime"):
        selected.append("jun")
    if any(marker in lowered for marker in ("디자인", "사용자", "화면", "ui", "ux", "표현")):
        selected.append("harin")
    if len(set(selected)) > 2 or any(marker in lowered for marker in ("협업", "역할", "우선순위", "계획")):
        selected.append("minji")
    return list(dict.fromkeys(item for item in selected if item in AGENT_IDS))


def _role_assignments(participant_ids: list[str], primary_id: str) -> list[dict[str, Any]]:
    return [
        {
            "agentId": participant_id,
            "assignment": "accountable-owner" if participant_id == primary_id else "independent-reviewer",
            "responsibility": ROLE_RESPONSIBILITIES.get(participant_id, "독립 검토"),
            "status": "assigned",
            "evidenceIds": [],
        }
        for participant_id in participant_ids
    ]


def _public_decision(command: str, tool: str) -> dict:
    return {
        "observations": [f"대표의 명령을 수신함: {redact_secrets(command)[:180]}", f"필요한 물리 도구: {TOOL_LABELS[tool]}"],
        "objective": "검증 가능한 산출물과 최종 리포트를 생성한다.",
        "hypotheses": [{"statement": "물리 도구에 도착해 실제 결과를 관찰하면 완료 기준을 검증할 수 있다.", "confidence": 0.78, "evidenceRefs": ["대표 명령", "공유 월드 도구 규칙"]}],
        "chosenAction": f"{TOOL_LABELS[tool]}까지 이동한 뒤 작업을 실행한다.",
        "rationale": "사무실 규칙상 물리적 도구에 도착한 에이전트만 해당 작업을 수행할 수 있다.",
        "alternatives": [{"action": "즉시 결과를 작성", "expectedBenefit": "빠른 응답", "rejectedBecause": "도구 실행과 근거가 없는 결과가 되기 때문"}],
        "evidenceRefs": ["대표 명령", "공유 월드 도구 규칙"],
        "uncertainties": ["실제 도구 결과를 관찰하기 전에는 최종 품질을 확정할 수 없다."],
        "expectedResult": "도구 증거와 대응하는 리포트가 생성된다.",
        "actualResult": "아직 실행 전",
        "nextChecks": ["도구 도착", "실제 입출력", "성공 기준별 증거"],
        "blockers": [], "confidence": 0.78,
    }


def _station_for(agent_id: str) -> dict:
    index = AGENT_IDS.index(agent_id) if agent_id in AGENT_IDS else 0
    return COMPUTER_STATIONS[index]


def _target_for(agent_id: str, tool: str) -> tuple[dict, str | None]:
    if tool == "computer":
        station = _station_for(agent_id)
        return deepcopy(station["position"]), station["id"]
    if tool == "meeting-room":
        seat = next((item for item in MEETING_SEATS if item["id"] == f"seat-{agent_id}"), MEETING_SEATS[0])
        return deepcopy(seat["position"]), seat["id"]
    return deepcopy(TOOL_SPOTS["whiteboard"]), None


def _safe_json(value: str) -> dict | None:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, json.JSONDecodeError):
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(value[start:end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
    return None


async def _groq_completion(client: httpx.AsyncClient, key: str, payload: dict[str, Any]) -> httpx.Response:
    response = await client.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        try:
            raw_error = response.json().get("error", {})
        except (TypeError, ValueError):
            raw_error = {}
        if isinstance(raw_error, dict):
            details = " · ".join(
                str(raw_error.get(field, "")).strip()
                for field in ("message", "type", "code")
                if str(raw_error.get(field, "")).strip()
            )
        else:
            details = str(raw_error).strip()
        safe_details = redact_secrets(details or response.reason_phrase or "request rejected")[:700]
        raise RuntimeError(f"Groq API {response.status_code}: {safe_details}") from error
    return response


def _normalize_summary(value: Any, fallback: dict) -> dict:
    item = value if isinstance(value, dict) else {}
    alternatives = item.get("alternatives") if isinstance(item.get("alternatives"), list) else fallback["alternatives"]
    safe_alternatives = []
    for alternative in alternatives[:4]:
        if isinstance(alternative, dict):
            safe_alternatives.append({
                "action": redact_secrets(str(alternative.get("action", "다른 접근")))[:240],
                "expectedBenefit": redact_secrets(str(alternative.get("expectedBenefit", "다른 장점")))[:360],
                "rejectedBecause": redact_secrets(str(alternative.get("rejectedBecause", "현재 목표에 덜 적합")))[:360],
            })
    hypotheses = item.get("hypotheses") if isinstance(item.get("hypotheses"), list) else fallback.get("hypotheses", [])
    safe_hypotheses = []
    for hypothesis in hypotheses[:4]:
        if not isinstance(hypothesis, dict):
            continue
        try:
            hypothesis_confidence = max(0.0, min(1.0, float(hypothesis.get("confidence", 0.5))))
        except (TypeError, ValueError):
            hypothesis_confidence = 0.5
        safe_hypotheses.append({
            "statement": redact_secrets(str(hypothesis.get("statement", "검증할 가설")))[:700],
            "confidence": hypothesis_confidence,
            "evidenceRefs": [redact_secrets(str(entry))[:300] for entry in hypothesis.get("evidenceRefs", [])[:6]] if isinstance(hypothesis.get("evidenceRefs"), list) else [],
        })
    def strings(key: str, default: list[str]) -> list[str]:
        raw = item.get(key)
        return [redact_secrets(str(entry))[:500] for entry in raw[:8]] if isinstance(raw, list) else default
    confidence = item.get("confidence", fallback["confidence"])
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = fallback["confidence"]
    return {
        "observations": strings("observations", fallback["observations"]),
        "objective": redact_secrets(str(item.get("objective", fallback["objective"])))[:700],
        "hypotheses": safe_hypotheses or fallback.get("hypotheses", []),
        "chosenAction": redact_secrets(str(item.get("chosenAction", fallback["chosenAction"])))[:700],
        "rationale": redact_secrets(str(item.get("rationale", fallback["rationale"])))[:1000],
        "alternatives": safe_alternatives or fallback["alternatives"],
        "evidenceRefs": strings("evidenceRefs", fallback["evidenceRefs"]),
        "uncertainties": strings("uncertainties", fallback.get("uncertainties", [])),
        "expectedResult": redact_secrets(str(item.get("expectedResult", fallback.get("expectedResult", "결과를 재관찰한다."))))[:700],
        "actualResult": redact_secrets(str(item.get("actualResult", fallback.get("actualResult", "아직 관찰 전"))))[:900],
        "nextChecks": strings("nextChecks", fallback.get("nextChecks", [])),
        "blockers": strings("blockers", fallback["blockers"]),
        "confidence": confidence,
    }


def _command_intent(command: str) -> dict[str, Any]:
    lowered = command.lower()
    direct_urls = command_urls(command)
    needs_knowledge = any(marker.lower() in lowered for marker in KNOWLEDGE_MARKERS)
    closure_continuity = bool(
        re.search(r"(?:브라우저|탭|페이지|접속|연결)[^.\n]{0,20}(?:종료|닫|끊)[^.\n]{0,30}(?:계속|영속|유지|실행|작업|에이전트|서버)", lowered)
        or re.search(r"(?:계속|영속|유지|실행|작업|에이전트|서버)[^.\n]{0,30}(?:브라우저|탭|페이지|접속|연결)[^.\n]{0,20}(?:종료|닫|끊)", lowered)
    )
    direct_runtime_context = any(marker in lowered for marker in (
        "운영계", "cashcow", "캐시카우", "이 사이트", "우리 사이트", "서버 런타임",
    ))
    infrastructure_marker = any(marker in lowered for marker in ("ecs", "fargate", "dynamodb", "다이나모"))
    persistence_marker = any(marker in lowered for marker in ("영속 실행", "영속성", "다른 브라우저", "다른 pc"))
    app_context = direct_runtime_context or any(marker in lowered for marker in ("에이전트", "브라우저", "이 앱", "현재 서비스"))
    needs_runtime = direct_runtime_context or closure_continuity or ((infrastructure_marker or persistence_marker) and app_context)
    failure_text = re.sub(r"(?:문제|오류|에러|실패|장애)\s*없이", "", lowered)
    needs_failure_audit = needs_runtime and bool(
        re.search(r"(?:최근|에이전트|작업|운영계)[^.\n]{0,24}(?:오류|에러|실패|장애)", failure_text)
        or re.search(r"(?:오류|에러|실패|장애)[^.\n]{0,18}(?:내역|원인|로그|보고|해결|분석|점검)", failure_text)
        or re.search(r"운영계[^.\n]{0,20}문제[^.\n]{0,12}(?:원인|로그|보고|해결|분석|점검)", failure_text)
    )
    explicit_write = False
    for marker in KNOWLEDGE_WRITE_MARKERS:
        for match in re.finditer(re.escape(marker), lowered):
            tail = lowered[match.start():match.start() + 70]
            if not (
                re.match(rf"{re.escape(marker)}\s*(?:을|를)?\s*(?:해|하|시켜)", tail)
                or re.match(rf"{re.escape(marker)}[^.\n]{{0,16}}(?:부탁|요청)", tail)
            ):
                continue
            is_non_action = bool(re.match(
                rf"{re.escape(marker)}\s*(?:을|를)?\s*(?:하지|해?\s*(?:둔|놓은|놓았|두었던)|한\s*(?:내용|정보|것)|해야\s*(?:하는지|할지)|해도\s*(?:되는지|될지)|할\s*수\s*있는지)",
                tail,
            )) or any(phrase in tail[:45] for phrase in ("여부", "판단만", "확인만", "읽기만", "조회만", "검색만"))
            if not is_non_action:
                explicit_write = True
                break
        if explicit_write:
            break
    if not explicit_write and re.search(r"(?:남겨|메모해)\s*(?:줘|주세요|두|놓|부탁|요청)", lowered):
        explicit_write = True
    needs_direct_browser = bool(direct_urls)
    return {
        "needsCode": any(word in lowered for word in (
            "코드", "스크립트", "계산", "자동화", "파이썬", "python", "코딩", "데이터 분석", "수치 분석",
        )),
        "needsSearch": needs_direct_browser or any(word in lowered for word in ("인터넷", "웹 검색", "외부 검색", "최신", "출처", "조사"))
        or ("검색" in lowered and not needs_knowledge and not needs_runtime),
        "needsDirectBrowser": needs_direct_browser,
        "directUrls": direct_urls,
        "needsKnowledge": needs_knowledge,
        "writesKnowledge": needs_knowledge and explicit_write,
        "needsRuntime": needs_runtime,
        "needsFailureAudit": needs_failure_audit,
        "requiresEcs": needs_runtime and any(marker in lowered for marker in ("ecs", "fargate")),
        "requiresSharedPersistence": needs_runtime and any(marker in lowered for marker in (
            "영속", "브라우저 종료", "브라우저를 종료", "브라우저를 닫", "브라우저 창을 닫",
            "창을 닫", "탭을 닫", "페이지를 닫", "접속을 끊", "연결을 끊", "다른 브라우저", "다른 pc",
        )),
    }


def _claims_permission_denial(value: str) -> bool:
    cleaned = value.lower()
    correction_patterns = (
        r"권한(?:이)?\s*(?:없|부족)[^.\n]{0,30}(?:아니|아님)",
        r"권한(?:이)?\s*(?:없지는|부족하지)\s*않",
        r"권한(?:이)?\s*부여되지\s*않[^.\n]{0,30}(?:아니|아님)",
        r"(?:접근할 수 없|열람이 불가능|접근 불가|읽기 불가|등록 불가|저장 불가)[^.\n]{0,30}(?:아니|아님)",
        r"(?:permission denied|not authorized|unauthorized|do not have access|cannot access|access denied)[^.\n]{0,30}(?:false|not the case)",
        r"(?:we|i|the agent)\s+do\s+have\s+access",
    )
    for pattern in correction_patterns:
        cleaned = re.sub(pattern, "", cleaned)
    for sentence in re.split(r"[.!?\n]+", cleaned):
        explicit_shared_context = (
            "shared knowledge" in sentence or "public information" in sentence
            or any(marker.lower() in sentence for marker in KNOWLEDGE_MARKERS)
        )
        external_source_context = any(marker in sentence for marker in (
            "외부", "원문", "url", "웹", "출처", "사이트", "페이지", "paywall",
        ))
        has_shared_context = explicit_shared_context or (
            not external_source_context
            and ("권한" in sentence or "permission" in sentence or "authoriz" in sentence or "privilege" in sentence)
        )
        if has_shared_context and any(marker in sentence for marker in PERMISSION_DENIAL_MARKERS):
            return True
    return False


def _claims_knowledge_write(value: str) -> bool:
    cleaned = value.lower()
    cleaned = re.sub(
        r"(?:등록|저장|기록|추가|작성)[^.\n]{0,20}(?:하지\s*않|하지\s*않았|안\s*했|하지\s*못|없었)",
        "", cleaned,
    )
    return "shared_knowledge.register" in cleaned or bool(re.search(
        r"(?:공용|공유)\s*(?:정보|지식)[^.\n]{0,40}(?:등록|저장|기록|추가|작성)\s*(?:을|를)?\s*(?:완료|했습니다|했다|하였습니다|하였다|됐|되었습니다|됨|에\s*성공)",
        cleaned,
    ))


def _urls(value: str) -> set[str]:
    normalized: set[str] = set()
    for match in re.findall(r"https?://[^\s\)\]\}>\"']+", value, flags=re.IGNORECASE):
        candidate = match.rstrip(".,;:")
        # Korean prose commonly attaches a particle directly to a URL (for
        # example, ``https://example.com/source를``).  It is not part of the
        # source URL and must not make valid tool-backed citations look forged.
        candidate = re.sub(
            r"(?<=[A-Za-z0-9/%#=_~-])(?:으로부터|에서부터|으로|에서|까지|부터|처럼|이며|이고|은|는|이|가|을|를|과|와|의|에|로|도|만)$",
            "",
            candidate,
        )
        parsed = urlsplit(candidate)
        query = urlencode(sorted(
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in ("gclid", "fbclid")
        ))
        normalized.add(urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query, "")))
    return normalized


def _relevant_knowledge(command: str, knowledge: list[dict], limit: int = 20) -> list[dict]:
    stopwords = {
        "공용", "공유", "정보", "지식", "검색", "등록", "저장", "기록", "추가", "작성",
        "업데이트", "갱신", "해줘", "해주세요", "확인", "검증", "관련", "내용", "모두",
        "전부", "최근", "전체", "읽어줘", "보여줘", "알려줘", "목록", "조회",
    }
    generic_prefixes = ("공용", "공유", "정보", "지식", "저장", "등록", "기록", "추가", "작성", "읽", "보여", "알려", "조회", "검색")
    terms = {
        token for token in re.findall(r"[0-9a-zA-Z가-힣_-]{2,}", command.lower())
        if token not in stopwords and not token.startswith(generic_prefixes)
    }
    ranked: list[tuple[int, str, dict]] = []
    for item in knowledge:
        if item.get("status", "active") != "active":
            continue
        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        if verification.get("passed") is not True or verification.get("status") in ("rejected", "invalidated"):
            continue
        haystack = f"{item.get('title', '')} {item.get('body', '')}".lower()
        score = sum(2 if term in str(item.get("title", "")).lower() else 1 for term in terms if term in haystack)
        if terms and score == 0:
            continue
        ranked.append((score, str(item.get("createdAt", "")), item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [deepcopy(item) for _score, _created_at, item in ranked[:limit]]


def _evidence_verification(
    intent: dict[str, bool], raw_content: str, report: dict, tool_runs: list[dict], runtime_status: dict | None,
) -> dict:
    published = "\n".join((
        str(report.get("reply", "")), str(report.get("publicDecision", "")),
        str(report.get("summary", "")), str(report.get("body", "")),
    )).lower()
    combined = f"{raw_content}\n{published}".lower()
    tool_names = [str(item.get("tool", "")).lower() for item in tool_runs]
    evidence_ids = [str(item.get("evidenceId")) for item in tool_runs if item.get("evidenceId")]
    required_checks: list[str] = []
    issues: list[str] = []

    if intent["needsKnowledge"]:
        required_checks.append("shared_knowledge.search 서버 결과 확인")
        if not any(name == "shared_knowledge.search" for name in tool_names):
            issues.append("공용 정보 서버 검색 결과가 없습니다.")
        if _claims_permission_denial(combined):
            issues.append("서버가 허용한 공용 정보 읽기·쓰기 권한을 모델이 잘못 부정했습니다.")
        if not intent.get("writesKnowledge") and _claims_knowledge_write(combined):
            issues.append("조회 전용 명령인데 모델이 공용 정보 등록을 완료했다고 잘못 보고했습니다.")

    if intent["needsSearch"]:
        required_checks.append("URL이 포함된 실제 외부 검색 결과 확인")
        browser_runs = [item for item in tool_runs if "browser" in str(item.get("tool", "")).lower() or str(item.get("tool", "")).lower() == "search"]
        if not browser_runs:
            issues.append("필수 외부 검색 도구가 실행되지 않았습니다.")
        else:
            tool_urls = set().union(*(_urls(str(item.get("output", ""))) for item in browser_runs))
            # Only URLs that survive into the public reply/report are citations.
            # Intermediate stage drafts can contain exploratory links that are
            # intentionally omitted from the final audited result.
            reported_urls = _urls(published)
            if not tool_urls:
                issues.append("외부 검색 도구 출력에서 검증 가능한 URL을 찾지 못했습니다.")
            elif not reported_urls:
                issues.append("검색 결과 리포트에 검증 가능한 출처 URL이 없습니다.")
            elif not reported_urls.issubset(tool_urls):
                issues.append("리포트 URL 중 실제 검색 도구 출력에 없는 출처가 있습니다.")

    if intent.get("needsDirectBrowser"):
        required_checks.extend(("격리 브라우저에서 렌더링 DOM 관찰", "행동 후 URL·DOM 재관찰"))
        direct_observations = [item for item in tool_runs if str(item.get("tool", "")).lower() == "browser.dom.observe"]
        if not direct_observations:
            issues.append("명시된 URL의 렌더링 DOM/ARIA 관찰 증거가 없습니다.")
        else:
            observation_text = "\n".join(str(item.get("output", "")) for item in direct_observations).lower()
            if "domhash" not in observation_text or "screenshothash" not in observation_text.replace("sha256", "hash"):
                issues.append("렌더링 화면의 DOM·스크린샷 해시를 확인하지 못했습니다.")
            if any(url.lower() not in observation_text for url in intent.get("directUrls", [])[:1]):
                issues.append("대표가 지정한 URL이 실제 렌더링 관찰 기록에 없습니다.")

    if intent["needsCode"]:
        required_checks.append("실제 코드 인터프리터 실행 결과 확인")
        if not any("code" in name or "interpreter" in name or name == "python" for name in tool_names):
            issues.append("필수 코드 인터프리터가 실행되지 않았습니다.")

    if intent["needsRuntime"]:
        required_checks.extend(("Cashcow 서버 runner 활성 확인", "브라우저 연결 불필요 확인", "영속 저장소 확인"))
        if not runtime_status or not any(name == "runtime.status" for name in tool_names):
            issues.append("Cashcow 내부 runtime.status 증거가 없습니다.")
        else:
            execution = runtime_status.get("execution", {})
            persistence = runtime_status.get("persistence", {})
            platform = runtime_status.get("platform", {})
            if not execution.get("runnerActive"):
                issues.append("서버 작업 runner가 활성 상태가 아닙니다.")
            if execution.get("runnerHealthy") is not True:
                issues.append("서버 작업 runner의 최근 반복 실행이 정상 상태가 아닙니다.")
            if execution.get("browserConnectionRequired") is not False:
                issues.append("브라우저와 독립된 서버 실행을 확인하지 못했습니다.")
            if not persistence.get("durableStorageConfigured"):
                issues.append("프로세스 재시작 후 유지되는 저장소를 확인하지 못했습니다.")
            if intent.get("requiresSharedPersistence") and persistence.get("backend") != "dynamodb":
                issues.append("다른 브라우저·PC에서도 공유되는 DynamoDB 저장소를 확인하지 못했습니다.")
            if intent.get("requiresEcs") and (
                platform.get("verified") is not True
                or platform.get("launchType") != "FARGATE"
                or platform.get("knownStatus") != "RUNNING"
            ):
                issues.append("현재 프로세스가 실행 중인 ECS Fargate task라는 직접 증거가 없습니다.")
            if intent.get("needsFailureAudit"):
                required_checks.append("runtime.status.failureAudit 최근 실패 기록 확인")
                failure_audit = runtime_status.get("state", {}).get("failureAudit")
                if not isinstance(failure_audit, dict) or not isinstance(failure_audit.get("recentFailures"), list):
                    issues.append("최근 에이전트 실패를 확인할 서버 감사 기록이 없습니다.")
                else:
                    recent_failures = failure_audit["recentFailures"]
                    if recent_failures and any(phrase in combined for phrase in ("오류 없음", "실패 없음", "문제 없음", "오류 0건", "실패 0건")):
                        issues.append("서버 감사 기록에 최근 실패가 있는데 결과가 실패 없음으로 보고했습니다.")
                    if recent_failures and not any(str(item.get("id", ""))[:8].lower() in combined for item in recent_failures):
                        issues.append("최근 실패 작업 식별자를 결과에 반영하지 않았습니다.")
        if not any(marker.lower() in combined for marker in RUNTIME_EVIDENCE_MARKERS):
            issues.append("모델 결과가 Cashcow 내부 런타임 증거를 반영하지 않았습니다.")
        if re.search(r"service\s*worker|서비스\s*워커", combined):
            disavows_service_worker = bool(re.search(
                r"(?:service\s*worker|서비스\s*워커)[^.\n]{0,80}(?:근거(?:로)?\s*(?:사용하지\s*않|삼지\s*않)|근거가\s*아니(?:다|며|고)(?:\s|[.,;:]|$)|증명(?:하지\s*못|할\s*수\s*없)|무관하(?:다|며|고)(?:\s|[.,;:]|$)|대체할\s*수\s*없)",
                combined,
            ))
            if not disavows_service_worker:
                issues.append("일반 Service Worker 자료는 Cashcow ECS/DynamoDB 운영 상태의 증거가 아닙니다.")

    issues = list(dict.fromkeys(issues))
    status = "rejected" if issues else "verified" if required_checks else "asserted"
    return {
        "status": status, "passed": not issues, "requiredChecks": required_checks,
        "evidenceIds": evidence_ids, "issues": issues,
    }


class WorldEngine:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.runner_task: asyncio.Task | None = None
        self.executions: dict[str, asyncio.Task] = {}
        self.closed = False
        self.started_at = utc_now()
        self.last_loop_at = self.started_at
        self.last_successful_loop_at = self.started_at
        self.consecutive_loop_failures = 0
        self.restored_state = {"jobs": 0, "reports": 0, "knowledge": 0, "loadedExistingState": False}
        self.recovered_jobs_this_boot: list[str] = []
        self.platform_evidence: dict[str, Any] = {
            "verified": False, "launchType": "LOCAL", "family": "local", "revision": "unknown",
            "knownStatus": "UNKNOWN", "desiredStatus": "UNKNOWN",
        }

    async def start(self) -> None:
        await self.store.initialize()
        restored = await self.store.snapshot()
        self.restored_state = {
            "jobs": len(restored.get("jobs", {})),
            "reports": len(restored.get("reports", [])),
            "knowledge": len(restored.get("knowledge", [])),
            "loadedExistingState": bool(restored.get("jobs") or restored.get("reports") or restored.get("knowledge")),
        }
        await self._capture_platform_evidence()
        await self._recover_executions()
        self.runner_task = asyncio.create_task(self._run_loop(), name="cashcow-world-loop")

    async def stop(self) -> None:
        self.closed = True
        if self.runner_task:
            self.runner_task.cancel()
        for task in self.executions.values():
            task.cancel()
        await asyncio.gather(*(list(self.executions.values()) + ([self.runner_task] if self.runner_task else [])), return_exceptions=True)

    async def _recover_executions(self) -> None:
        async with self.lock:
            def recover(data: dict):
                recovered: list[str] = []
                for job in data["jobs"].values():
                    if job.get("status") == "active" and job.get("phase") == "executing":
                        job["phase"] = "working"
                        recovery_now = utc_now()
                        job["updatedAt"] = recovery_now
                        if isinstance(job.get("goalContract"), dict):
                            job["goalContract"].update({
                                "nextAction": "중단된 실행의 마지막 증거부터 안전하게 재검증",
                                "nextWakeAt": recovery_now,
                                "updatedAt": recovery_now,
                            })
                        job["events"].append(_event(job["id"], "status", "서버 재시작 후 안전한 실행 단계 복구", "working", output="검색·격리 코드 실행처럼 재실행 가능한 단계만 다시 검증합니다."))
                        recovered.append(job["id"])
                active_ids = {job["id"] for job in data["jobs"].values() if job.get("status") in ("queued", "active")}
                for agent in data["world"]["agents"]:
                    for field in ("activeJobId", "supportingJobId"):
                        if agent.get(field) and agent[field] not in active_ids:
                            agent[field] = None
                    if not agent.get("activeJobId") and not agent.get("supportingJobId"):
                        agent.update({"activity": "다음 요청을 기다리는 중", "focus": "공용 오피스 관찰", "progress": 0, "currentTool": None, "toolStation": None})
                    else:
                        assigned_id = agent.get("activeJobId") or agent.get("supportingJobId")
                        assigned_job = data["jobs"].get(assigned_id)
                        contract = assigned_job.get("goalContract") if isinstance(assigned_job, dict) else None
                        current_contract = agent.get("cognition", {}).get("commitment")
                        if isinstance(contract, dict) and (
                            not isinstance(current_contract, dict) or current_contract.get("jobId") != assigned_id
                        ):
                            memory_ids = set(assigned_job.get("recalledMemoryIdsByAgent", {}).get(agent["id"], []))
                            memories = [item for item in agent.get("memories", []) if item.get("id") in memory_ids]
                            begin_commitment(agent, contract, memories, utc_now())
                occupancies = data["world"].setdefault("toolOccupancies", {})
                for station in list(occupancies):
                    if occupancies[station].get("jobId") not in active_ids:
                        occupancies.pop(station, None)
                return recovered
            self.recovered_jobs_this_boot = await self.store.mutate(recover)

    async def _capture_platform_evidence(self) -> None:
        metadata_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4", "").strip()
        execution_env = os.getenv("AWS_EXECUTION_ENV", "").strip()
        if not metadata_uri:
            self.platform_evidence = {
                "verified": False,
                "launchType": "FARGATE" if "FARGATE" in execution_env.upper() else "LOCAL",
                "family": "unknown", "revision": "unknown", "knownStatus": "UNKNOWN", "desiredStatus": "UNKNOWN",
            }
            return
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=1.0)) as client:
                response = await client.get(f"{metadata_uri.rstrip('/')}/task")
                response.raise_for_status()
                payload = response.json()
            task_arn = str(payload.get("TaskARN", ""))
            self.platform_evidence = {
                "verified": True,
                "launchType": str(payload.get("LaunchType") or ("FARGATE" if "FARGATE" in execution_env.upper() else "UNKNOWN")),
                "family": redact_secrets(str(payload.get("Family") or "unknown"))[:120],
                "revision": redact_secrets(str(payload.get("Revision") or "unknown"))[:40],
                "knownStatus": redact_secrets(str(payload.get("KnownStatus") or "UNKNOWN"))[:40],
                "desiredStatus": redact_secrets(str(payload.get("DesiredStatus") or "UNKNOWN"))[:40],
                "taskSuffix": redact_secrets(task_arn.rsplit("/", 1)[-1])[:16] if task_arn else None,
            }
        except Exception as error:
            self.platform_evidence = {
                "verified": False,
                "launchType": "FARGATE" if "FARGATE" in execution_env.upper() else "UNKNOWN",
                "family": "unknown", "revision": "unknown", "knownStatus": "UNKNOWN", "desiredStatus": "UNKNOWN",
                "observationError": redact_secrets(f"{type(error).__name__}: metadata unavailable")[:120],
            }

    def _runtime_status(self, data: dict) -> dict:
        now = datetime.now(timezone.utc)
        try:
            loop_at = datetime.fromisoformat(self.last_loop_at.replace("Z", "+00:00"))
            loop_age = max(0.0, (now - loop_at).total_seconds())
        except (TypeError, ValueError):
            loop_age = None
        try:
            successful_loop_at = datetime.fromisoformat(self.last_successful_loop_at.replace("Z", "+00:00"))
            successful_loop_age = max(0.0, (now - successful_loop_at).total_seconds())
        except (TypeError, ValueError):
            successful_loop_age = None
        backend_type = type(self.store).__name__
        durable = backend_type in ("DynamoStore", "FileStore")
        statuses: dict[str, int] = {}
        for job in data.get("jobs", {}).values():
            status = str(job.get("status", "unknown"))
            statuses[status] = statuses.get(status, 0) + 1
        failure_candidates = sorted(
            (
                job for job in data.get("jobs", {}).values()
                if job.get("status") == "failed" or job.get("attemptResults")
            ),
            key=lambda item: str(item.get("updatedAt", item.get("createdAt", ""))), reverse=True,
        )[:8]
        recent_failures = []
        for job in failure_candidates:
            recent_failures.append({
                "id": str(job.get("id", "")), "agentId": str(job.get("agentId", "")),
                "status": str(job.get("status", "unknown")), "phase": str(job.get("phase", "unknown")),
                "attempt": int(job.get("attempt", 0)),
                "command": redact_secrets(str(job.get("command", "")))[:400],
                "attemptResults": [
                    {
                        "attempt": item.get("attempt"), "outcome": item.get("outcome"),
                        "summary": redact_secrets(str(item.get("summary", "")))[:500],
                        "qualityIssues": [redact_secrets(str(issue))[:300] for issue in item.get("qualityIssues", [])[:5]],
                        "failureSignature": redact_secrets(str(item.get("failureSignature", "")))[:500],
                    }
                    for item in job.get("attemptResults", [])[-3:] if isinstance(item, dict)
                ],
            })
        runner_active = bool(self.runner_task and not self.runner_task.done() and not self.closed and (loop_age is None or loop_age < 5.0))
        runner_healthy = bool(
            runner_active
            and self.consecutive_loop_failures < 3
            and (successful_loop_age is None or successful_loop_age < 5.0)
        )
        evidence_id = f"runtime-status-{uuid.uuid4()}"
        return {
            "evidenceId": evidence_id, "source": "cashcow-runtime", "capturedAt": utc_now(),
            "execution": {
                "owner": "server", "runnerActive": runner_active, "loopAgeSeconds": loop_age,
                "runnerHealthy": runner_healthy, "successfulLoopAgeSeconds": successful_loop_age,
                "consecutiveLoopFailures": self.consecutive_loop_failures,
                "browserConnectionRequired": False, "paused": bool(data.get("world", {}).get("paused", False)),
                "processStartedAt": self.started_at,
            },
            "persistence": {
                "backend": "dynamodb" if backend_type == "DynamoStore" else "filesystem" if backend_type == "FileStore" else backend_type.lower(),
                "scope": "shared-server", "durableStorageConfigured": durable,
                "loadedExistingState": self.restored_state["loadedExistingState"],
                "restoredCounts": deepcopy(self.restored_state), "survivesContainerReplacement": backend_type == "DynamoStore",
            },
            "state": {
                "revision": int(data.get("revision", 0)), "worldVersion": int(data.get("world", {}).get("version", 0)),
                "jobsByStatus": statuses, "recoveredJobsThisBoot": list(self.recovered_jobs_this_boot),
                "failureAudit": {"observedJobCount": len(data.get("jobs", {})), "recentFailures": recent_failures},
            },
            "platform": deepcopy(self.platform_evidence),
            "deployment": {"buildSha": redact_secrets(os.getenv("RUNTIME_BUILD_SHA", "unknown"))[:64]},
        }

    async def snapshot(self, actor_id: str | None = None) -> dict:
        async with self.lock:
            if actor_id:
                await self.store.mutate(lambda data: _ensure_player(data["world"], actor_id))
            data = await self.store.snapshot()
            return self._public_snapshot(data, actor_id)

    async def board_revision(self, version: int) -> dict:
        async with self.lock:
            data = await self.store.snapshot()
            document = data.get("boardVersions", {}).get(str(version))
            if not document:
                raise KeyError("요청한 화이트보드 버전을 찾을 수 없습니다.")
            return deepcopy(document) | {"history": deepcopy(data["boardHistory"][:BOARD_HISTORY_LIMIT])}

    async def job_detail(self, job_id: str) -> dict:
        async with self.lock:
            data = await self.store.snapshot()
            job = data["jobs"].get(job_id)
            if not job:
                raise KeyError("요청한 작업 감사 로그를 찾을 수 없습니다.")
            report = next((item for item in data["reports"] if item["planId"] == job_id), None)
            return {"job": deepcopy(job), "report": deepcopy(report)}

    def _public_snapshot(self, data: dict, actor_id: str | None = None) -> dict:
        world = deepcopy(data["world"])
        jobs = data["jobs"]
        reports_by_plan = {item["planId"]: item for item in data["reports"]}
        for agent in world["agents"]:
            relevant = sorted([job for job in jobs.values() if agent["id"] in job.get("participantIds", [job["agentId"]])], key=lambda item: item["createdAt"], reverse=True)
            assigned_id = agent.get("activeJobId") or agent.get("supportingJobId")
            current = jobs.get(assigned_id) if assigned_id else None
            if not current or current.get("status") not in ("queued", "active"):
                queued = sorted((job for job in relevant if job.get("status") == "queued"), key=lambda item: item["createdAt"])
                current = queued[0] if queued else None
            if current:
                agent["plan"] = deepcopy(current["plan"])
                agent["events"] = deepcopy(current["events"][-40:])
                agent["activeJob"] = {
                    "id": current["id"], "command": current["command"], "status": current["status"],
                    "phase": current["phase"], "createdAt": current["createdAt"],
                    "assignment": "owner" if current["agentId"] == agent["id"] else "supporter",
                }
                if current["id"] in reports_by_plan:
                    agent["report"] = deepcopy(reports_by_plan[current["id"]])
            else:
                agent["plan"] = []
                agent["events"] = []
                agent.pop("activeJob", None)
                latest_report_job = next((job for job in relevant if job["id"] in reports_by_plan), None)
                if latest_report_job:
                    agent["report"] = deepcopy(reports_by_plan[latest_report_job["id"]])
                else:
                    agent.pop("report", None)
            agent["memories"] = agent.get("memories", [])[-20:][::-1]
        board = deepcopy(data["board"])
        board["history"] = deepcopy(data["boardHistory"][:30])
        player = deepcopy(world.get("players", {}).get(actor_id)) if actor_id else None
        messages = deepcopy(data["messages"][-60:])
        if player:
            messages = [message for message in messages if message.get("kind") == "system" or message.get("speakerId") == actor_id or (message.get("originPosition") and distance(player["position"], message["originPosition"]) <= HEARING_RADIUS)]
        return {
            "worldVersion": world["version"], "serverTime": utc_now(), "agentsPaused": world["paused"],
            "agents": world["agents"], "messages": messages, "player": player,
            "reports": deepcopy(sorted(data["reports"], key=lambda item: item["createdAt"], reverse=True)[:30]),
            "whiteboard": board,
        }

    async def runtime_health(self) -> dict:
        async with self.lock:
            data = await self.store.snapshot()
            status = self._runtime_status(data)
            active = sum(1 for job in data.get("jobs", {}).values() if job.get("status") == "active")
            healthy = bool(status["execution"]["runnerHealthy"])
            return {
                "status": "ok" if healthy else "degraded", "worldVersion": status["state"]["worldVersion"],
                "paused": status["execution"]["paused"], "runnerActive": healthy,
                "loopAgeSeconds": status["execution"]["loopAgeSeconds"], "activeJobs": active,
                "buildSha": status["deployment"]["buildSha"],
            }

    async def invalidate_job_artifacts(self, job_id: str, reason: str, actor_id: str) -> dict:
        reason = redact_secrets(reason.strip())[:1000]
        if not reason:
            raise ValueError("무효화 사유는 비어 있을 수 없습니다.")
        async with self.lock:
            def invalidate(data: dict):
                job = data["jobs"].get(job_id)
                if not job:
                    raise KeyError("요청한 작업 감사 로그를 찾을 수 없습니다.")
                if job.get("invalidation"):
                    return deepcopy(job["invalidation"]) | {"alreadyInvalidated": True}
                if job.get("status") not in ("completed", "failed"):
                    raise ValueError("종료된 작업만 운영 재검증으로 무효화할 수 있습니다.")
                now = utc_now()
                report = next((item for item in data.get("reports", []) if item.get("planId") == job_id), None)
                if not report:
                    raise ValueError("현재 보관 범위에서 작업 리포트를 찾을 수 없어 안전하게 무효화할 수 없습니다.")
                previous_completed = bool(report and report.get("outcome") == "completed")
                invalidated_report_id = None
                if report:
                    invalidated_report_id = report["id"]
                    original_summary = report.get("summary", "")
                    original_body = report.get("body", "")
                    report.update({
                        "outcome": "failed", "verificationStatus": "invalidated",
                        "verification": {
                            "status": "invalidated", "passed": False,
                            "evidenceIds": list((report.get("verification") or {}).get("evidenceIds", [])),
                            "requiredChecks": list((report.get("verification") or {}).get("requiredChecks", [])),
                            "issues": [reason],
                        },
                        "summary": f"운영 재검증으로 완료 판정을 취소했습니다. {reason}"[:1800],
                        "body": f"## 운영 재검증\n\n{reason}\n\n## 무효화 전 원문\n\n{original_body or original_summary}"[:12000],
                        "limitations": list(dict.fromkeys([reason, *(report.get("limitations") or [])]))[:8],
                        "invalidatedAt": now, "invalidatedBy": actor_id,
                    })
                invalidated_knowledge = 0
                for item in data.get("knowledge", []):
                    if item.get("sourceJobId") != job_id or item.get("status") == "invalidated":
                        continue
                    item.update({
                        "status": "invalidated", "verificationStatus": "invalidated",
                        "verification": {
                            "status": "invalidated", "passed": False,
                            "evidenceIds": list((item.get("verification") or {}).get("evidenceIds", [])),
                            "requiredChecks": list((item.get("verification") or {}).get("requiredChecks", [])),
                            "issues": [reason],
                        },
                        "invalidatedAt": now, "invalidatedBy": actor_id, "invalidReason": reason,
                    })
                    invalidated_knowledge += 1
                invalidated_memories = 0
                for agent in data["world"]["agents"]:
                    for memory in agent.get("memories", []):
                        if invalidated_report_id and memory.get("evidence") in (f"report_{invalidated_report_id}", f"job_{job_id}"):
                            memory.update({"verificationStatus": "invalidated", "confidence": 0.0, "invalidatedAt": now, "invalidReason": reason})
                            invalidated_memories += 1
                    if previous_completed and agent["id"] == job.get("agentId"):
                        awarded_delta = int(job.get("scoreAwarded", 1))
                        agent["score"] = max(0, int(agent.get("score", 0)) - awarded_delta)
                job.update({
                    "status": "failed", "phase": "failed", "updatedAt": now,
                    "invalidation": {
                        "jobId": job_id, "reportId": invalidated_report_id, "knowledgeCount": invalidated_knowledge,
                        "memoryCount": invalidated_memories, "invalidatedAt": now, "invalidatedBy": actor_id,
                        "reason": reason,
                    },
                })
                if job.get("plan"):
                    job["plan"][-1]["status"] = "blocked"
                    job["plan"][-1]["detail"] = "운영 재검증에서 완료 근거가 무효화됨"
                job.setdefault("events", []).append(_event(job_id, "verification", "운영 재검증으로 완료 판정 취소", "blocked", input=reason, output=f"공용 정보 {invalidated_knowledge}건·기억 {invalidated_memories}건 격리"))
                data["world"]["version"] += 1
                data["world"]["updatedAt"] = now
                return deepcopy(job["invalidation"]) | {"alreadyInvalidated": False}
            return await self.store.mutate(invalidate)

    async def enqueue(self, agent_id: str, command: str, actor_id: str, idempotency_key: str) -> dict:
        command = redact_secrets(command.strip())[:800]
        if not command:
            raise ValueError("명령은 비어 있을 수 없습니다.")
        async with self.lock:
            def create(data: dict):
                world = data["world"]
                if world["paused"]:
                    raise RuntimeError("대표가 에이전트 활동을 일시정지했습니다.")
                agent = next((item for item in world["agents"] if item["id"] == agent_id), None)
                if not agent:
                    raise ValueError("알 수 없는 에이전트입니다.")
                existing = next((job for job in data["jobs"].values() if job.get("actorId") == actor_id and job.get("idempotencyKey") == idempotency_key), None)
                if existing:
                    return {"jobId": existing["id"], "heardBy": existing.get("heardBy", []), "agentName": agent["name"], "status": existing["status"]}
                speaker_position = _ensure_player(world, actor_id)["position"]
                heard_by = [item["name"] for item in world["agents"] if distance(speaker_position, item["position"]) <= HEARING_RADIUS]
                if agent["name"] not in heard_by:
                    raise PermissionError(f"{agent['name']}은(는) 들을 수 있는 거리 밖에 있습니다.")
                now = utc_now()
                job_id = str(uuid.uuid4())
                tool = target_tool_from_command(command)
                intent = _command_intent(command)
                participants = _participants_for(agent_id, tool, intent, command)
                contract = make_goal_contract(command, intent, tool, now)
                contract["jobId"] = job_id
                recalled_memories = relevant_memories(agent, command)
                job = {
                    "id": job_id, "actorId": actor_id, "idempotencyKey": idempotency_key, "heardBy": heard_by, "agentId": agent_id, "command": command,
                    "tool": tool, "participantIds": participants, "status": "queued", "phase": "queued",
                    "plan": _plan(command, tool), "planRevision": 1, "events": [], "checkpoints": [],
                    "goalContract": contract, "recalledMemoryIds": [item.get("id") for item in recalled_memories],
                    "roleAssignments": _role_assignments(participants, agent_id),
                    "attempt": 0, "attemptResults": [],
                    "createdAt": now, "updatedAt": now, "completedAt": None,
                }
                decision = _public_decision(command, tool)
                job["events"].extend([
                    _event(job_id, "command", "대표 명령 수신", input=command, output=f"{TOOL_LABELS[tool]} 작업 큐에 영속 저장"),
                    _event(job_id, "status", "완료 계약과 작업 기억 체크포인트 저장", "working", input=json.dumps(contract["successCriteria"], ensure_ascii=False), output=f"검증 기억 {len(recalled_memories)}개 활성화 · nextAction={contract['nextAction']}"),
                    _event(job_id, "decision", "초기 판단 요약", summary=decision, input=command, output=decision["chosenAction"]),
                ])
                data["jobs"][job_id] = job
                data["messages"].append(_message(actor_id, "대표님", command, "user", heard_by, job_id, speaker_position))
                data["messages"] = data["messages"][-60:]
                self._activate_waiting(data)
                world["version"] += 1
                world["updatedAt"] = now
                return {"jobId": job_id, "heardBy": heard_by, "agentName": agent["name"], "status": job["status"]}
            return await self.store.mutate(create)

    async def move_player(self, actor_id: str, target: dict, facing: str) -> dict:
        async with self.lock:
            def update(data: dict):
                world = data["world"]
                player = _ensure_player(world, actor_id)
                current = player["position"]
                desired = {"x": float(target["x"]), "y": float(target["y"])}
                gap = distance(current, desired)
                if gap > 1.6:
                    scale = 1.6 / gap
                    desired = {"x": current["x"] + (desired["x"] - current["x"]) * scale, "y": current["y"] + (desired["y"] - current["y"]) * scale}
                if is_blocked(desired):
                    desired = current
                safe_facing = facing if facing in ("north", "east", "south", "west") else direction_from_delta(desired["x"] - current["x"], desired["y"] - current["y"], player["facing"])
                if distance(current, desired) < 0.001 and safe_facing == player["facing"]:
                    return deepcopy(player)
                player.update({"position": desired, "facing": safe_facing, "updatedAt": utc_now()})
                world["version"] += 1
                world["updatedAt"] = player["updatedAt"]
                return deepcopy(player)
            return await self.store.mutate(update)

    def _activate_waiting(self, data: dict) -> None:
        world = data["world"]
        occupancies = world.setdefault("toolOccupancies", {})
        queued = sorted((job for job in data["jobs"].values() if job["status"] == "queued"), key=lambda item: item["createdAt"])
        for job in queued:
            participants = [next(item for item in world["agents"] if item["id"] == participant_id) for participant_id in job["participantIds"]]
            if any(agent.get("activeJobId") or agent.get("supportingJobId") for agent in participants):
                continue
            claims = []
            for participant in participants:
                _target, station = _target_for(participant["id"], job["tool"])
                claims.append(station or "whiteboard-main")
            if any(occupancies.get(station, {}).get("jobId") not in (None, job["id"]) for station in claims):
                continue
            now = utc_now()
            if not isinstance(job.get("goalContract"), dict):
                job["goalContract"] = make_goal_contract(job["command"], _command_intent(job["command"]), job["tool"], now)
                job["goalContract"]["jobId"] = job["id"]
            if not isinstance(job.get("roleAssignments"), list):
                job["roleAssignments"] = _role_assignments(job["participantIds"], job["agentId"])
            for station, participant in zip(claims, participants):
                occupancies[station] = {"jobId": job["id"], "agentId": participant["id"], "claimedAt": now}
            job["claimedStations"] = claims
            job.setdefault("recalledMemoryIdsByAgent", {})
            for participant in participants:
                target, station = _target_for(participant["id"], job["tool"])
                participant_memories = relevant_memories(participant, job["command"])
                job["recalledMemoryIdsByAgent"][participant["id"]] = [item.get("id") for item in participant_memories]
                begin_commitment(participant, job["goalContract"], participant_memories, now)
                assignment = next(
                    (item for item in job.get("roleAssignments", []) if item.get("agentId") == participant["id"]),
                    None,
                )
                if isinstance(assignment, dict):
                    assignment["status"] = "active"
                    assignment["startedAt"] = now
                    if isinstance(participant.get("cognition", {}).get("commitment"), dict):
                        participant["cognition"]["commitment"]["nextAction"] = assignment.get("responsibility", "할당 역할 수행")
                participant["target"] = target
                participant["toolStation"] = station
                participant["currentTool"] = None
                participant["focus"] = job["command"][:80]
                participant["activity"] = f"{TOOL_LABELS[job['tool']]}(으)로 이동 중"
                participant["progress"] = 8
                if participant["id"] == job["agentId"]:
                    participant["activeJobId"] = job["id"]
                else:
                    participant["supportingJobId"] = job["id"]
            job["status"] = "active"
            job["phase"] = "moving"
            job["updatedAt"] = now
            if isinstance(job.get("goalContract"), dict):
                job["goalContract"].update({"nextAction": f"{TOOL_LABELS[job['tool']]}까지 실제 이동", "nextWakeAt": now, "updatedAt": now})
            job["events"].append(_event(job["id"], "status", "공유 월드에서 이동 시작", "working", output=f"참여자 {', '.join(agent['name'] for agent in participants)}"))

    async def set_paused(self, paused: bool, actor_id: str) -> dict:
        if paused:
            for task in list(self.executions.values()):
                task.cancel()
        async with self.lock:
            def update(data: dict):
                world = data["world"]
                world["paused"] = paused
                world["version"] += 1
                world["updatedAt"] = utc_now()
                if paused:
                    for job in data["jobs"].values():
                        if job["status"] == "active" and job["phase"] == "executing":
                            job["phase"] = "working"
                            job["events"].append(_event(job["id"], "status", "대표가 실행을 일시정지", "blocked", output="현재 체크포인트를 저장하고 외부 호출을 중단했습니다."))
                data["messages"].append(_message("system", "시스템", "에이전트 활동이 일시정지됐습니다." if paused else "에이전트 활동이 재개됐습니다.", "system", [], None))
                return {"agentsPaused": paused, "updatedAt": world["updatedAt"], "updatedBy": actor_id}
            return await self.store.mutate(update)

    async def save_board(self, value: dict, actor_id: str, actor_name: str) -> dict:
        async with self.lock:
            def update(data: dict):
                board = data["board"]
                if int(value["expectedVersion"]) != int(board["version"]):
                    raise FileExistsError("다른 사용자가 먼저 수정했습니다. 최신 보드를 다시 불러와 주세요.")
                now = utc_now()
                existing_blocks = {block["id"]: block for block in board.get("textBlocks", [])}
                text_blocks = []
                for block in value["textBlocks"]:
                    previous = existing_blocks.get(block["id"])
                    if previous:
                        provenance = {key: previous.get(key) for key in ("authorType", "authorId", "authorName", "createdAt")}
                    else:
                        provenance = {"authorType": "user", "authorId": actor_id, "authorName": actor_name, "createdAt": now}
                    text_blocks.append(block | provenance | {"updatedAt": now, "lastEditorId": actor_id, "lastEditorName": actor_name})
                next_board = {
                    "title": redact_secrets(value["title"].strip())[:40], "width": 256, "height": 256,
                    "palette": value["palette"], "pixelsBase64": value["pixelsBase64"],
                    "textBlocks": text_blocks, "version": board["version"] + 1,
                    "authorType": "user", "authorId": actor_id, "authorName": actor_name,
                    "changeSummary": redact_secrets(value.get("changeSummary") or "사용자가 그림과 텍스트 영역을 편집")[:180],
                    "updatedAt": now,
                }
                data["board"] = next_board
                data.setdefault("boardVersions", {})[str(next_board["version"])] = deepcopy(next_board)
                data["boardHistory"].insert(0, board_meta(next_board))
                data["boardHistory"] = data["boardHistory"][:BOARD_HISTORY_LIMIT]
                retained = {str(item["version"]) for item in data["boardHistory"]}
                data["boardVersions"] = {key: document for key, document in data["boardVersions"].items() if key in retained}
                data["world"]["version"] += 1
                data["world"]["updatedAt"] = now
                return deepcopy(next_board) | {"history": deepcopy(data["boardHistory"])}
            return await self.store.mutate(update)

    async def apply_review(self, scores: dict, actor_id: str) -> dict:
        async with self.lock:
            def update(data: dict):
                now = utc_now()
                cycle_id = scores.get("cycleId", "current")
                applied = data["world"].setdefault("appliedReviews", {})
                if cycle_id in applied:
                    return {"reflectedAgents": 0, "actorId": actor_id, "alreadyApplied": True}
                lesson = redact_secrets(scores.get("comment") or f"개인 {scores['individual']}점·팀 {scores['team']}점 평가를 다음 행동에 반영한다.")[:1000]
                for agent in data["world"]["agents"]:
                    agent["score"] = round(((scores["individual"] + scores["team"]) / 10) * 100)
                    agent.setdefault("memories", []).append({
                        "id": str(uuid.uuid4()), "kind": "lesson", "at": now, "summary": lesson,
                        "evidence": f"weekly_review_{cycle_id}", "confidence": 0.95, "verificationStatus": "verified",
                    })
                    agent["memories"] = agent["memories"][-20:]
                data["world"]["version"] += 1
                data["world"]["updatedAt"] = now
                applied[cycle_id] = {"actorId": actor_id, "appliedAt": now, "individual": scores["individual"], "team": scores["team"]}
                return {"reflectedAgents": len(data["world"]["agents"]), "actorId": actor_id, "alreadyApplied": False}
            return await self.store.mutate(update)

    async def _run_loop(self) -> None:
        while not self.closed:
            self.last_loop_at = utc_now()
            try:
                await self._tick()
                self.last_successful_loop_at = utc_now()
                self.consecutive_loop_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as error:
                self.consecutive_loop_failures += 1
                print("world_tick_failed", type(error).__name__, str(error)[:300], flush=True)
            await asyncio.sleep(0.4)

    async def _tick(self) -> None:
        jobs_to_execute: list[str] = []
        async with self.lock:
            def advance(data: dict):
                world = data["world"]
                if world["paused"]:
                    return []
                changed = False
                now = datetime.now(timezone.utc)
                moving_jobs = [job for job in data["jobs"].values() if job["status"] == "active" and job["phase"] == "moving"]
                elapsed = 0.4
                if moving_jobs:
                    try:
                        previous = datetime.fromisoformat(world["lastTickAt"].replace("Z", "+00:00"))
                        elapsed = max(0.05, min(1.0, (now - previous).total_seconds()))
                    except (KeyError, ValueError):
                        elapsed = 0.4
                    world["lastTickAt"] = now.isoformat().replace("+00:00", "Z")
                for job in moving_jobs:
                    participants = [agent for agent in world["agents"] if agent["id"] in job["participantIds"]]
                    all_arrived = True
                    for agent in participants:
                        if distance(agent["position"], agent["target"]) <= 2.4:
                            continue
                        all_arrived = False
                        point, facing = navigate_toward(agent["position"], agent["target"], 2.15 * elapsed, agent["facing"])
                        agent["position"] = point
                        agent["facing"] = facing
                        agent["activity"] = f"{TOOL_LABELS[job['tool']]}(으)로 이동 중"
                        agent["progress"] = min(38, agent["progress"] + elapsed * 2)
                        changed = True
                    if all_arrived:
                        for agent in participants:
                            agent["currentTool"] = job["tool"]
                            agent["activity"] = f"{TOOL_LABELS[job['tool']]}에서 작업 준비"
                            agent["progress"] = 42
                        job["phase"] = "working"
                        _set_plan_phase(job, "move", "done")
                        _set_plan_phase(job, "act", "active")
                        if isinstance(job.get("goalContract"), dict):
                            job["goalContract"].update({"nextAction": "실제 도구 관찰과 첫 행동 실행", "nextWakeAt": utc_now(), "updatedAt": utc_now()})
                        job["events"].append(_event(job["id"], "status", f"{TOOL_LABELS[job['tool']]} 도착·점유 확인", "working", output="물리적 도착 좌표와 도구 점유를 검증했습니다."))
                        job["updatedAt"] = utc_now()
                        changed = True
                for job in data["jobs"].values():
                    if job["status"] == "active" and job["phase"] == "executing" and job["id"] not in self.executions:
                        job["phase"] = "working"
                        job["updatedAt"] = utc_now()
                        job["events"].append(_event(job["id"], "verification", "중단된 실행 단계 자동 복구", "working", output="실행 task가 없는 executing 체크포인트를 재계획 큐로 되돌렸습니다."))
                        changed = True
                    if job["status"] == "active" and job["phase"] == "working" and job["id"] not in self.executions:
                        job["phase"] = "executing"
                        job["attempt"] += 1
                        job["updatedAt"] = utc_now()
                        jobs_to_execute.append(job["id"])
                        for agent in world["agents"]:
                            if agent["id"] in job["participantIds"]:
                                agent["activity"] = f"{TOOL_LABELS[job['tool']]}에서 실제 작업 중"
                                agent["progress"] = 55
                                update_checkpoint(agent, "도구 도착을 확인했고 실제 결과를 관찰할 차례다.", "허용된 도구로 첫 관찰 실행", 0.72, utc_now())
                        changed = True
                if changed:
                    world["version"] += 1
                    world["updatedAt"] = utc_now()
                return jobs_to_execute
            jobs_to_execute = await self.store.mutate(advance)
        for job_id in jobs_to_execute:
            task = asyncio.create_task(self._execute_job(job_id), name=f"cashcow-job-{job_id}")
            self.executions[job_id] = task
            task.add_done_callback(lambda _task, identifier=job_id: self.executions.pop(identifier, None))

    async def _execute_job(self, job_id: str) -> None:
        try:
            data = await self.store.snapshot()
            job = deepcopy(data["jobs"].get(job_id))
            if not job:
                return
            agent = next(item for item in data["world"]["agents"] if item["id"] == job["agentId"])
            participants = [item for item in data["world"]["agents"] if item["id"] in job["participantIds"]]
            if _command_intent(job["command"])["needsRuntime"]:
                await self._capture_platform_evidence()
            result = await self._groq_work(
                job, agent, participants, data.get("knowledge", []), data.get("reports", []), self._runtime_status(data),
            )
            await self._complete_job(job_id, result)
        except asyncio.CancelledError:
            return
        except Exception as error:
            try:
                await self._fail_or_retry(job_id, error)
            except Exception as handler_error:
                print(
                    "job_failure_handler_failed", job_id,
                    redact_secrets(f"{type(handler_error).__name__}: {handler_error}")[:300], flush=True,
                )

    async def _record_tool_checkpoint(self, job_id: str, tool_run: dict, observation: str, next_action: str) -> None:
        """Persist observable progress while a long external action is still running."""
        async with self.lock:
            def record(data: dict):
                job = data.get("jobs", {}).get(job_id)
                if not job or job.get("status") != "active":
                    return False
                evidence_id = tool_run.get("evidenceId")
                if evidence_id and evidence_id in job.setdefault("recordedEvidenceIds", []):
                    return True
                now = utc_now()
                job["events"].append(_event(
                    job_id, "tool_request", f"{tool_run['tool']}에 전달", "working",
                    tool=redact_secrets(str(tool_run["tool"]))[:120], input=redact_secrets(str(tool_run.get("input", "")))[:6000],
                    output="허용 정책을 통과한 실제 도구 실행 요청", evidenceId=evidence_id,
                ))
                job["events"].append(_event(
                    job_id, "tool_result", f"{tool_run['tool']} 반환", "success",
                    tool=redact_secrets(str(tool_run["tool"]))[:120], input=redact_secrets(str(tool_run.get("input", "")))[:6000],
                    output=redact_secrets(str(tool_run.get("output", "")))[:9000], evidenceId=evidence_id,
                ))
                if evidence_id:
                    job["recordedEvidenceIds"].append(evidence_id)
                    job["recordedEvidenceIds"] = job["recordedEvidenceIds"][-80:]
                job.setdefault("checkpoints", []).append({
                    "id": str(uuid.uuid4()), "at": now, "evidenceId": evidence_id,
                    "observation": redact_secrets(observation)[:700], "nextAction": redact_secrets(next_action)[:700],
                })
                job["checkpoints"] = job["checkpoints"][-30:]
                job["updatedAt"] = now
                if isinstance(job.get("goalContract"), dict):
                    job["goalContract"].update({"nextAction": next_action[:500], "nextWakeAt": now, "updatedAt": now})
                for participant in data["world"]["agents"]:
                    if participant["id"] in job.get("participantIds", []):
                        update_checkpoint(participant, observation, next_action, 0.76, now)
                        participant["activity"] = f"{TOOL_LABELS[job['tool']]} 결과를 관찰·검증 중"
                        participant["progress"] = min(84, float(participant.get("progress", 55)) + 4)
                data["world"]["version"] += 1
                data["world"]["updatedAt"] = now
                return True
            recorded = await self.store.mutate(record)
        if recorded:
            tool_run["recorded"] = True

    async def _collect_direct_browser(
        self, job: dict, intent: dict[str, Any], agent: dict,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not (intent.get("needsDirectBrowser") or intent.get("directUrls")):
            return [], []
        if not os.getenv("BROWSER_WORKER_URL", "").strip():
            return [], []
        try:
            return await self._run_direct_browser(job, intent, agent)
        except Exception as error:
            return [{
                "evidenceId": f"browser-error-{uuid.uuid4()}",
                "tool": "browser.dom.observe",
                "input": json.dumps({"urls": intent.get("directUrls", []), "objective": job.get("command", "")}, ensure_ascii=False)[:6000],
                "output": f"격리 브라우저 관찰 실패: {redact_secrets(type(error).__name__)[:80]}",
            }], []

    async def _pick_browser_action(
        self,
        job: dict,
        agent: dict,
        observation: dict[str, Any],
        step: int,
        trace: BrowseTrace,
        client: httpx.AsyncClient | None,
        key: str,
    ) -> dict[str, Any]:
        traits = ((agent.get("cognition") or {}).get("traits") if isinstance(agent.get("cognition"), dict) else {}) or {}
        ranked = ranked_public_read_actions(observation, job["command"], step, trace, traits)
        chosen = ranked[0]
        if client and key and len(ranked) > 1:
            options = [
                {
                    "index": index,
                    "action": item.get("action"),
                    "ref": item.get("ref"),
                    "value": item.get("value"),
                    "reason": item.get("reason"),
                }
                for index, item in enumerate(ranked[:5])
            ]
            try:
                response = await _groq_completion(client, key, {
                    "model": os.getenv("GROQ_STRUCTURED_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
                    "temperature": 0.0,
                    "max_completion_tokens": 400,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "당신은 성실한 직원처럼 공개 페이지만 읽습니다. 숨은 사고과정은 쓰지 마세요. 주어진 후보 중에서만 고르고, 로그인·결제·게시·삭제는 거부합니다. JSON 키는 index 하나뿐입니다."},
                        {"role": "user", "content": json.dumps({
                            "objective": job["command"][:500],
                            "url": observation.get("url"),
                            "title": observation.get("title"),
                            "renderedText": str(observation.get("renderedText", ""))[:1800],
                            "staff": staff_brief(agent)["duty"],
                            "options": options,
                        }, ensure_ascii=False)},
                    ],
                })
                payload = _safe_json((((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "{}")) or {}
                index = int(payload.get("index", 0))
                if 0 <= index < len(ranked):
                    chosen = ranked[index]
                    if chosen.get("action") == "done" and ranked[0].get("action") != "done":
                        chosen = ranked[0]
            except Exception:
                chosen = ranked[0]
        return trace.commit(chosen)

    def _report_from_direct_observations(
        self, job: dict, agent: dict, intent: dict[str, Any], tool_runs: list[dict], observations: list[dict],
    ) -> dict:
        lines = []
        for item in observations:
            lines.append(
                f"## {item.get('title') or item.get('url')}\n\n"
                f"- URL: {item.get('url')}\n"
                f"- HTTP: {item.get('httpStatus')}\n"
                f"- DOM: {item.get('domHash')}\n"
                f"- 화면 해시: {item.get('screenshotSha256')}\n\n"
                f"{item.get('renderedText') or '렌더링 텍스트 없음'}"
            )
        body = "\n\n".join(lines) or "격리 브라우저가 공개 화면을 반환하지 못했습니다."
        first_url = str((observations[0].get("url") if observations else "") or (intent.get("directUrls") or [""])[0])
        report = {
            "title": f"{agent.get('name', '에이전트')}의 공개 페이지 관찰",
            "outcome": "partial" if observations else "failed",
            "summary": redact_secrets(
                f"{first_url} 화면의 렌더링 텍스트와 DOM/ARIA를 격리 워커에서 관찰했습니다."
            )[:1800],
            "body": redact_secrets(body)[:12000],
            "limitations": ["모델 키가 없어 추가 해석 없이 관찰 원문만 보고합니다."],
            "lessons": ["공개 URL은 추측이 아니라 실제 렌더링 관찰로 확인한다."],
        }
        if not observations:
            report["limitations"] = ["격리 브라우저가 지정 URL의 화면을 반환하지 못했습니다."]
        verification = _evidence_verification(intent, body, report | {"reply": report["summary"]}, tool_runs, None)
        if verification["issues"]:
            report["limitations"] = list(dict.fromkeys([*report["limitations"], *verification["issues"]]))[:8]
            report["outcome"] = "partial" if observations else "failed"
        return {
            "reply": report["summary"][:1000],
            "publicDecision": _normalize_summary({
                "observations": [f"격리 워커가 {first_url}를 렌더링함", "DOM·스크린샷 해시를 기록함"],
                "objective": job.get("command", ""),
                "chosenAction": "공개 화면을 읽고 필요한 읽기 전용 동작만 수행",
                "rationale": "대표가 지정한 URL은 실제 화면 코드로 확인해야 한다.",
                "alternatives": [{"action": "URL만 인용", "rejectedBecause": "렌더링되지 않은 인용은 증거가 아님"}],
                "evidenceRefs": [str(item.get("evidenceId")) for item in tool_runs if item.get("evidenceId")][:8],
                "blockers": verification.get("issues", []),
                "confidence": 0.72 if observations else 0.2,
            }, _public_decision(job["command"], job["tool"])),
            "toolRuns": tool_runs,
            "collaboration": [],
            "registerKnowledge": False,
            "verification": verification,
            "report": report,
        }

    async def _run_direct_browser(
        self, job: dict, intent: dict[str, Any], agent: dict | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        browser = RemoteBrowserClient(os.getenv("BROWSER_WORKER_URL", "").strip())
        tool_runs: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        agent = agent or {}
        key = os.getenv("GROQ_API_KEY", "").strip()
        groq_client: httpx.AsyncClient | None = None
        if key:
            groq_client = httpx.AsyncClient(timeout=httpx.Timeout(18.0, connect=5.0))

        try:
            for url in intent.get("directUrls", [])[:2]:
                trace = BrowseTrace()

                async def planner(observation: dict[str, Any], step: int, *, active_trace=trace) -> dict[str, Any]:
                    return await self._pick_browser_action(
                        job, agent, observation, step, active_trace, groq_client, key,
                    )

                async def checkpoint(run: dict[str, Any]) -> None:
                    output = str(run.get("output", ""))
                    snippet = output[:240]
                    try:
                        parsed = json.loads(output)
                        if isinstance(parsed, dict):
                            snippet = str(parsed.get("renderedText") or parsed.get("title") or parsed.get("url") or "")[:240]
                    except (TypeError, json.JSONDecodeError):
                        pass
                    await self._record_tool_checkpoint(
                        job["id"],
                        run,
                        f"{run.get('tool', 'browser.dom.observe')}: {redact_secrets(snippet)}",
                        "읽기 전용 동작 뒤 URL·DOM·스크린샷 해시를 재검증",
                    )

                result = await browser.run(url, job["command"], planner, checkpoint)
                for run in result.get("toolRuns", []):
                    if not isinstance(run, dict):
                        continue
                    run["input"] = redact_secrets(str(run.get("input", "")))[:6000]
                    run["output"] = redact_secrets(str(run.get("output", "")))[:9000]
                    tool_runs.append(run)
                final = result.get("finalObservation") if isinstance(result.get("finalObservation"), dict) else {}
                observations.append({
                    "url": redact_secrets(str(final.get("url", url)))[:2048],
                    "title": redact_secrets(str(final.get("title", "")))[:300],
                    "httpStatus": final.get("httpStatus"),
                    "renderedText": redact_secrets(str(final.get("renderedText", "")))[:4500],
                    "renderedHtmlExcerpt": redact_secrets(str(final.get("renderedHtmlExcerpt", "")))[:2500],
                    "ariaSnapshot": redact_secrets(str(final.get("ariaSnapshot", "")))[:3000],
                    "domHash": final.get("domHash"),
                    "screenshotSha256": final.get("screenshotSha256"),
                    "attemptedRequestCount": int(final.get("attemptedRequestCount", 0)),
                    "allowedRequestCount": int(final.get("allowedRequestCount", 0)),
                    "blockedRequestCount": int(final.get("blockedRequestCount", 0)),
                    "untrustedObservation": True,
                })
        finally:
            if groq_client is not None:
                await groq_client.aclose()
        return tool_runs, observations

    async def _groq_work(
        self, job: dict, agent: dict, participants: list[dict], knowledge: list[dict], reports: list[dict], runtime_status: dict,
    ) -> dict:
        fallback_decision = _public_decision(job["command"], job["tool"])
        intent = _command_intent(job["command"])
        direct_tool_runs, direct_observations = await self._collect_direct_browser(job, intent, agent)
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            if direct_tool_runs:
                return self._report_from_direct_observations(job, agent, intent, direct_tool_runs, direct_observations)
            return {
                "reply": "요청은 실행했지만 Groq 키가 없어 모델 산출물을 만들 수 없었습니다.",
                "publicDecision": fallback_decision, "toolRuns": [], "collaboration": [],
                "report": {
                    "title": f"{agent['name']}의 작업 결과", "outcome": "partial",
                    "summary": "물리적 이동과 도구 점유는 완료했지만 모델 실행 키가 없어 산출물 생성이 제한됐습니다.",
                    "body": "## 실행 결과\n\n공유 월드 이동과 도구 점유를 검증했습니다. GROQ_API_KEY 설정 후 같은 명령을 다시 실행해야 합니다.",
                    "limitations": ["Groq 실행 키가 설정되지 않음"], "lessons": ["실행 전 외부 도구 자격 증명을 확인한다."],
                },
            }
        needs_code = intent["needsCode"]
        needs_search = intent["needsSearch"]
        needs_knowledge = intent["needsKnowledge"]
        needs_runtime = intent["needsRuntime"]
        used_isolated_browser = bool(direct_observations)
        tool_sequence = (
            (["browser_search"] if needs_search and not used_isolated_browser else [])
            + (["code_interpreter"] if needs_code else [])
        )
        knowledge_matches = _relevant_knowledge(job["command"], knowledge) if needs_knowledge else []
        recent_verified_reports = [
            item for item in sorted(reports, key=lambda entry: str(entry.get("createdAt", "")), reverse=True)
            if item.get("outcome") == "completed"
            and isinstance(item.get("verification"), dict)
            and item["verification"].get("passed") is True
            and item["verification"].get("status") == "verified"
        ][:10]
        verified_lessons = [
            item for item in agent.get("memories", [])
            if item.get("verificationStatus") in ("verified", "asserted")
        ][-5:]
        work_prompt = {
            "agent": {key: agent[key] for key in ("name", "team", "role", "rank")},
            "staffState": staff_brief(agent),
            "command": job["command"], "physicalTool": TOOL_LABELS[job["tool"]],
            "participants": [{"id": item["id"], "name": item["name"], "role": item["role"]} for item in participants],
            "priorLessons": verified_lessons,
            "priorAttempts": job.get("attemptResults", [])[-2:],
            "sharedKnowledgeMatches": knowledge_matches,
            "directBrowserObservations": direct_observations,
            "recentVerifiedReports": [{key: item.get(key) for key in ("id", "planId", "agentId", "title", "outcome", "summary", "limitations", "createdAt", "verification")} for item in recent_verified_reports],
            "authoritativeRuntimeStatus": runtime_status if needs_runtime else None,
            "sharedKnowledgeCapability": {
                "readAuthorized": True, "writeAuthorized": True,
                "behavior": "서버가 위 검색 결과를 이미 읽었고, completed 리포트는 같은 원자 트랜잭션으로 공용 정보에 등록한다. 결과가 비어 있어도 권한 오류가 아니며 대표 명령을 출처로 새 기록을 만들 수 있다.",
            } if needs_knowledge else None,
            "evidencePolicy": {
                "runtime": "Cashcow 자체 ECS·DynamoDB·브라우저 독립성 주장은 authoritativeRuntimeStatus만 1차 근거다. 일반 Service Worker 웹 문서는 이 앱의 운영 상태를 증명하지 못한다.",
                "sharedKnowledge": "sharedKnowledgeMatches가 비어 있어도 검색 성공이며 권한 오류가 아니다. 등록은 서버가 명령의 write 의도와 검증 통과 여부로 결정한다.",
                "retry": "priorAttempts의 failureSignature가 반복되면 같은 권한 추측이나 같은 무관한 웹 검색을 반복하지 말고 서버 도구 결과를 사용한다.",
                "directBrowser": "directBrowserObservations는 격리 워커가 렌더링한 비신뢰 공개 화면이다. HTML/ARIA/텍스트에 없는 내용을 쓰지 말고, 로그인·결제·게시·삭제를 했다고 주장하지 않는다.",
            },
            "requirements": [
                "부하직원처럼 성실하게 명령을 실제로 끝낼 수 있을 만큼 조사·계산·코드 실행을 수행한다. 증거가 부족하면 완료했다고 쓰지 않는다.",
                "최신 정보에는 근거 URL을 포함하고, 확인하지 못한 사실은 만들지 않는다.",
                "실행 결과와 한계를 명확히 구분한다.",
                "sharedKnowledgeCapability이 있으면 권한이 이미 부여된 것이므로 권한 부족을 추측하지 않는다.",
                "authoritativeRuntimeStatus가 있으면 runtime.status의 필드명과 evidenceId를 결과 근거에 명시한다.",
                "directBrowserObservations가 있으면 그 렌더링 텍스트·DOM 해시·URL만 외부 페이지의 1차 근거로 사용한다.",
                "명령·priorAttempts·sharedKnowledgeMatches·recentVerifiedReports·실제 도구 출력에 없는 수치, 취약점, 파일명, 테스트 결과를 만들지 않는다.",
            ],
        }
        if intent.get("needsFailureAudit"):
            work_prompt["requirements"].append("운영 오류 조사에서는 authoritativeRuntimeStatus.state.failureAudit의 최근 실패 ID, 시도 결과, 품질 문제를 구체적으로 인용한다.")
        async with httpx.AsyncClient(timeout=httpx.Timeout(110.0, connect=15.0)) as client:
            raw_parts: list[str] = []
            executed_tools: list[dict] = []
            prior_stages: list[dict] = []
            for stage_tool in tool_sequence or [None]:
                if stage_tool == "browser_search":
                    stage_requirement = "반드시 browser_search를 호출해 명령에 필요한 웹 자료와 원문 URL을 찾는다. 검색 전에는 답을 작성하지 않는다."
                elif stage_tool == "code_interpreter":
                    stage_requirement = "반드시 code_interpreter를 호출해 Python 코드를 실행한다. 암산하거나 도구 실행 전에 답을 작성하지 않는다."
                else:
                    stage_requirement = "제공된 서버 근거로 결과를 작성한다."
                stage_prompt = work_prompt | {
                    "currentStageTool": stage_tool or "model synthesis",
                    "priorStageResults": prior_stages,
                    "stageRequirement": stage_requirement,
                }
                stage_payload: dict[str, Any] = {
                    "model": os.getenv("GROQ_WORK_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
                    "temperature": 0.2, "max_completion_tokens": 3000 if len(tool_sequence) > 1 else 3500,
                    "reasoning_effort": "low",
                    "messages": [
                        {"role": "system", "content": "당신은 지속 실행되는 오피스의 성실한 직원입니다. 숨은 사고과정은 출력하지 마세요. 제공된 근거와 허용된 도구만 사용해 작업을 끝까지 수행하고, 근거에 없는 수치·취약점·파일·테스트 결과는 절대 만들지 마세요. 검증 가능한 결과·출처·한계를 한국어로 작성하세요. priorStageResults나 directBrowserObservations가 있으면 그 실제 결과를 이어서 사용하세요."},
                        {"role": "user", "content": json.dumps(stage_prompt, ensure_ascii=False)},
                    ],
                }
                if stage_tool:
                    stage_payload["tools"] = [{"type": stage_tool}]
                    stage_payload["tool_choice"] = "required"
                try:
                    stage_response = await _groq_completion(client, key, stage_payload)
                except RuntimeError as first_error:
                    if not stage_tool or "tool_use_failed" not in str(first_error):
                        raise RuntimeError(f"{stage_tool or 'synthesis'} 단계: {first_error}") from first_error
                    compact_context = json.dumps(prior_stages[-1:], ensure_ascii=False)[:8000]
                    compact_payload = {
                        "model": stage_payload["model"], "temperature": 0.1,
                        "max_completion_tokens": 2200, "reasoning_effort": "low",
                        "messages": [
                            {"role": "system", "content": stage_requirement},
                            {"role": "user", "content": f"대표 명령: {job['command']}\n직전 단계 실제 결과: {compact_context}\n지금 허용된 도구를 반드시 실행하세요."},
                        ],
                        "tools": [{"type": stage_tool}], "tool_choice": "required",
                    }
                    try:
                        stage_response = await _groq_completion(client, key, compact_payload)
                    except RuntimeError as retry_error:
                        raise RuntimeError(f"{stage_tool} 단계: {retry_error}") from retry_error
                stage_message = (((stage_response.json().get("choices") or [{}])[0].get("message")) or {})
                stage_content = redact_secrets(str(stage_message.get("content") or ""))[:7000]
                stage_tools = stage_message.get("executed_tools") if isinstance(stage_message.get("executed_tools"), list) else []
                executed_tools.extend(entry for entry in stage_tools if isinstance(entry, dict))
                raw_parts.append(stage_content)
                prior_stages.append({
                    "tool": stage_tool or "model synthesis", "modelOutput": stage_content,
                    "toolOutputs": [
                        redact_secrets(str(entry.get("output") or entry.get("search_results") or ""))[:6000]
                        for entry in stage_tools[:4] if isinstance(entry, dict)
                    ],
                })
            raw_content = "\n\n".join(part for part in raw_parts if part)[:12000]
            safe_tools = []
            safe_tools.extend(direct_tool_runs)
            if needs_knowledge:
                safe_tools.append({
                    "evidenceId": f"shared-knowledge-{uuid.uuid4()}",
                    "tool": "shared_knowledge.search", "input": job["command"],
                    "output": json.dumps(knowledge_matches, ensure_ascii=False)[:9000] if knowledge_matches else "일치하는 기존 공용 정보가 없습니다.",
                })
            if needs_runtime:
                safe_tools.append({
                    "evidenceId": runtime_status["evidenceId"], "tool": "runtime.status",
                    "input": "Cashcow 서버 실행·브라우저 독립성·영속 저장소 상태를 직접 관측",
                    "output": json.dumps(runtime_status, ensure_ascii=False)[:9000],
                })
            for entry in executed_tools[:12]:
                if not isinstance(entry, dict):
                    continue
                safe_tools.append({
                    "evidenceId": f"external-tool-{uuid.uuid4()}",
                    "tool": redact_secrets(str(entry.get("name") or entry.get("type") or "built_in_tool"))[:80],
                    "input": redact_secrets(str(entry.get("arguments") or ""))[:6000],
                    "output": redact_secrets(str(entry.get("output") or entry.get("search_results") or ""))[:9000],
                })
            actual_collaboration = []
            for collaborator in participants:
                if collaborator["id"] == agent["id"]:
                    continue
                collaboration_input = {
                    "command": job["command"], "primaryAgent": agent["name"],
                    "yourIdentity": {key: collaborator[key] for key in ("name", "team", "role", "rank")},
                    "primaryResult": raw_content,
                    "request": "독립적으로 결과를 검토하고 빠진 근거·위험·다음 플랜을 한국어로 답하세요. 숨은 사고과정은 쓰지 마세요.",
                }
                try:
                    collaborator_response = await _groq_completion(
                        client, key, {
                            "model": os.getenv("GROQ_STRUCTURED_MODEL", "openai/gpt-oss-120b"), "temperature": 0.2,
                            "max_completion_tokens": 1000,
                            "messages": [
                                {"role": "system", "content": f"당신은 {collaborator['name']}이며 {collaborator['role']}입니다. 전달받은 작업을 독립적으로 검토해 공개 가능한 응답만 작성하세요."},
                                {"role": "user", "content": json.dumps(collaboration_input, ensure_ascii=False)},
                            ],
                        },
                    )
                    response_text = redact_secrets(str((((collaborator_response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")))[:4000]
                except Exception as error:
                    response_text = f"협업 응답을 받지 못했습니다: {redact_secrets(type(error).__name__)[:80]}"
                actual_collaboration.append({
                    "toAgentId": collaborator["id"], "purpose": "독립 검토와 플랜 조정",
                    "message": json.dumps(collaboration_input, ensure_ascii=False), "response": response_text,
                })
            structure_prompt = {
                "command": job["command"], "agent": agent["name"], "participants": [item["name"] for item in participants],
                "rawModelResult": raw_content, "actualToolRuns": safe_tools, "actualCollaborationTurns": actual_collaboration,
                "evidencePolicy": work_prompt["evidencePolicy"],
            }
            try:
                second = await _groq_completion(
                    client, key, {
                        "model": os.getenv("GROQ_STRUCTURED_MODEL", "openai/gpt-oss-120b"), "temperature": 0.1,
                        "max_completion_tokens": 2600, "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": "모델 초안과 실제 도구 결과를 한국어 공개 감사 JSON으로 구조화하세요. 숨은 사고과정이나 비공개 chain-of-thought는 쓰지 마세요. 키: reply, publicDecision(observations[],objective,chosenAction,rationale,alternatives[{action,rejectedBecause}],evidenceRefs[],blockers[],confidence 0..1), report(title,outcome completed|partial|failed,summary,body,limitations[],lessons[]). actualToolRuns에 없는 사실은 추가하지 마세요. Cashcow 운영 주장은 runtime.status만 근거로 사용하고 일반 Service Worker 문서를 운영 증거로 쓰지 마세요. shared_knowledge.search가 성공했다면 빈 결과도 권한 오류가 아닙니다."},
                            {"role": "user", "content": json.dumps(structure_prompt, ensure_ascii=False)},
                        ],
                    },
                )
            except RuntimeError as error:
                raise RuntimeError(f"structured-report 단계: {error}") from error
            structured_content = (((second.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "{}")
        structured = _safe_json(structured_content) or {}
        report = structured.get("report") if isinstance(structured.get("report"), dict) else {}
        outcome = report.get("outcome") if report.get("outcome") in ("completed", "partial", "failed") else ("completed" if raw_content else "partial")
        normalized_report = {
            "title": redact_secrets(str(report.get("title") or f"{agent['name']}의 {TOOL_LABELS[job['tool']]} 작업 리포트"))[:240],
            "outcome": outcome,
            "summary": redact_secrets(str(report.get("summary") or raw_content[:700] or "작업 결과가 제한적입니다."))[:1800],
            "body": redact_secrets(str(report.get("body") or raw_content or "산출물이 없습니다."))[:12000],
            "limitations": [redact_secrets(str(item))[:700] for item in (report.get("limitations") if isinstance(report.get("limitations"), list) else [])[:8]],
            "lessons": [redact_secrets(str(item))[:700] for item in (report.get("lessons") if isinstance(report.get("lessons"), list) else [])[:8]],
        }
        verification_input = normalized_report | {
            "reply": structured.get("reply", ""), "publicDecision": structured.get("publicDecision", {}),
        }
        verification = _evidence_verification(intent, raw_content, verification_input, safe_tools, runtime_status if needs_runtime else None)
        if verification["passed"] and intent["writesKnowledge"] and not (needs_runtime or needs_search or needs_code):
            verification["status"] = "asserted"
        if verification["issues"]:
            normalized_report["limitations"] = list(dict.fromkeys([*normalized_report["limitations"], *verification["issues"]]))[:8]
            if normalized_report["outcome"] == "completed":
                normalized_report["outcome"] = "partial"
        if needs_runtime:
            evidence_appendix = "\n\n## 서버가 직접 관측한 runtime.status\n\n```json\n" + json.dumps(runtime_status, ensure_ascii=False, indent=2) + "\n```"
            normalized_report["body"] = (normalized_report["body"] + evidence_appendix)[:12000]
        public_decision = _normalize_summary(structured.get("publicDecision"), fallback_decision)
        if needs_runtime and runtime_status["evidenceId"] not in public_decision["evidenceRefs"]:
            public_decision["evidenceRefs"] = [*public_decision["evidenceRefs"], runtime_status["evidenceId"]][:8]
        register_knowledge = bool(intent["writesKnowledge"] and normalized_report["outcome"] == "completed" and verification["passed"])
        canonical_knowledge = None
        if register_knowledge and needs_runtime:
            canonical_knowledge = {
                "title": "Cashcow 운영 런타임 상태",
                "body": "Cashcow WorldEngine은 서버 프로세스에서 실행되며 브라우저 연결이 필요하지 않습니다. 영속 저장소와 현재 실행 플랫폼은 facts 필드의 서버 직접 관측값을 따릅니다.",
                "facts": deepcopy(runtime_status), "sourceScope": "cashcow-runtime",
            }
        normalized = {
            "reply": redact_secrets(str(structured.get("reply") or f"{agent['name']}입니다. 작업을 마치고 리포트를 저장했습니다."))[:1000],
            "publicDecision": public_decision,
            "toolRuns": safe_tools,
            "collaboration": actual_collaboration,
            "registerKnowledge": register_knowledge,
            "knowledgeRecord": canonical_knowledge,
            "verification": verification,
            "report": normalized_report,
        }
        return normalized

    async def _complete_job(self, job_id: str, result: dict) -> None:
        async with self.lock:
            def complete(data: dict):
                job = data["jobs"].get(job_id)
                if not job or job["status"] != "active":
                    return False
                world = data["world"]
                agent = next(item for item in world["agents"] if item["id"] == job["agentId"])
                verification = _normalized_verification(result.get("verification"))
                job["events"].append(_event(job_id, "decision", "실행 후 판단 요약", summary=result["publicDecision"], input="검증된 도구 결과", output=result["publicDecision"]["chosenAction"]))
                for tool_run in result["toolRuns"]:
                    if tool_run.get("recorded"):
                        continue
                    evidence_id = tool_run.get("evidenceId")
                    job["events"].append(_event(job_id, "tool_request", f"{tool_run['tool']}에 전달", tool=tool_run["tool"], input=tool_run["input"], output="도구 실행 요청 전달", evidenceId=evidence_id))
                    job["events"].append(_event(job_id, "tool_result", f"{tool_run['tool']} 반환", tool=tool_run["tool"], input=tool_run["input"], output=tool_run["output"], evidenceId=evidence_id))
                participant_names = {item["id"]: item["name"] for item in world["agents"]}
                participant_positions = {item["id"]: item["position"] for item in world["agents"]}
                collaboration_peer_ids: set[str] = set()
                for raw in result["collaboration"][:12]:
                    if not isinstance(raw, dict):
                        continue
                    recipient_id = str(raw.get("toAgentId") or "")
                    if recipient_id not in participant_names or recipient_id not in job.get("participantIds", []) or recipient_id == agent["id"]:
                        continue
                    message = redact_secrets(str(raw.get("message") or "진행 결과 검토를 요청합니다."))[:1500]
                    response = redact_secrets(str(raw.get("response") or "검토 결과를 공유했습니다."))[:1500]
                    purpose = redact_secrets(str(raw.get("purpose") or "협업 검토"))[:240]
                    collaboration_peer_ids.add(recipient_id)
                    job["events"].append(_event(job_id, "agent_message", purpose, recipientAgentId=recipient_id, recipientName=participant_names[recipient_id], input=message, output=response))
                    data["messages"].append(_message(agent["id"], agent["name"], message, "agent", [participant_names[recipient_id]], job_id, agent["position"]))
                    data["messages"].append(_message(recipient_id, participant_names[recipient_id], response, "agent", [agent["name"]], job_id, participant_positions[recipient_id]))
                now = utc_now()
                report_value = deepcopy(result["report"])
                verified_completion = bool(report_value.get("outcome") == "completed" and verification["passed"])
                if report_value.get("outcome") == "completed" and not verified_completion:
                    report_value["outcome"] = "partial" if int(job.get("attempt", 0)) < MAX_ATTEMPTS else "failed"
                    report_value["limitations"] = list(dict.fromkeys([
                        *report_value.get("limitations", []), *verification.get("issues", []),
                    ]))[:8]
                if report_value["outcome"] != "completed" and int(job.get("attempt", 0)) < MAX_ATTEMPTS:
                    failure_signature = " | ".join(verification.get("issues") or report_value.get("limitations") or [report_value["outcome"]])[:1200]
                    job.setdefault("attemptResults", []).append({
                        "attempt": job["attempt"], "outcome": report_value["outcome"],
                        "summary": report_value["summary"], "limitations": report_value["limitations"], "at": now,
                        "toolNames": [item.get("tool") for item in result["toolRuns"]],
                        "evidenceIds": list(verification.get("evidenceIds", [])),
                        "qualityIssues": list(verification.get("issues", [])),
                        "failureSignature": failure_signature,
                        "retryDirective": "같은 실패 원인을 반복하지 말고 서버가 제공한 실제 도구 증거와 권한 상태를 우선 사용한다.",
                    })
                    job["attemptResults"] = job["attemptResults"][-MAX_ATTEMPTS:]
                    job["events"].append(_event(job_id, "verification", f"{job['attempt']}차 결과 미충족 · 자동 재계획", "blocked", output=report_value["summary"]))
                    job["phase"] = "working"
                    job["updatedAt"] = now
                    _record_replan(job, report_value, verification, now)
                    for participant in world["agents"]:
                        if participant["id"] in job["participantIds"]:
                            participant["activity"] = "결과 미충족 · 다음 행동 재계획"
                            participant["progress"] = min(82, 56 + job["attempt"] * 8)
                            update_checkpoint(
                                participant,
                                "직전 결과가 완료 기준을 충족하지 못했다.",
                                "미충족 성공 기준에 필요한 증거만 다시 관찰",
                                0.46,
                                now,
                            )
                    world["version"] += 1
                    world["updatedAt"] = now
                    return True
                report = {
                    "id": str(uuid.uuid4()), "planId": job_id, "agentId": agent["id"],
                    "title": report_value["title"], "outcome": report_value["outcome"],
                    "summary": report_value["summary"], "body": report_value["body"],
                    "limitations": report_value["limitations"], "lessons": report_value["lessons"], "createdAt": now,
                    "verificationStatus": verification.get("status", "asserted"), "verification": deepcopy(verification),
                }
                job_success = bool(report["outcome"] == "completed" and verification["passed"])
                job["events"].append(_event(job_id, "verification", "서버 완료 조건 검증", "success" if job_success else "blocked", output=f"결과: {report['outcome']} · 근거 상태 {verification.get('status')} · 실제 도구 기록 {len(result['toolRuns'])}건"))
                job["events"].append(_event(job_id, "report", "최종 리포트 발행", output=report["summary"]))
                job["status"] = "completed" if job_success else "failed"
                job["phase"] = "completed" if job_success else "failed"
                job["completedAt"] = now
                job["updatedAt"] = now
                _finish_goal_contract(job, job_success, verification, report["id"], now)
                for step in job["plan"]:
                    phase = step.get("phase")
                    if job_success or phase in ("contract", "memory", "move", "act"):
                        step["status"] = "done"
                    elif phase in ("verify", "reflect") or step is job["plan"][-1]:
                        step["status"] = "blocked"
                data["reports"].append(report)
                data["reports"] = sorted(data["reports"], key=lambda item: str(item.get("createdAt", "")))[-30:]
                data["messages"].append(_message(agent["id"], agent["name"], result["reply"], "agent", ["대표님"], job_id, agent["position"]))
                data["messages"] = data["messages"][-60:]
                lesson_items = (report["lessons"] if job_success else []) or ["완료 조건이 충족되지 않은 결과는 사실 근거로 재사용하지 않는다."]
                for lesson in lesson_items[:3]:
                    agent.setdefault("memories", []).append({
                        "id": str(uuid.uuid4()), "kind": "lesson", "at": now, "summary": lesson,
                        "evidence": f"report_{report['id']}",
                        "confidence": 0.88 if job_success else 0.55,
                        "verificationStatus": verification.get("status") if job_success else "failed",
                    })
                previous_score = int(agent.get("score", 0))
                next_score = min(100, previous_score + (1 if job_success else 0))
                agent["score"] = next_score
                job["scoreAwarded"] = next_score - previous_score
                if result.get("registerKnowledge") and job_success:
                    record = result.get("knowledgeRecord") if isinstance(result.get("knowledgeRecord"), dict) else {}
                    knowledge = {
                        "id": str(uuid.uuid4()), "title": redact_secrets(str(record.get("title") or report["title"]))[:240],
                        "body": redact_secrets(str(record.get("body") or report["summary"]))[:4000],
                        "sourceJobId": job_id, "authorAgentId": agent["id"], "authorName": agent["name"], "createdAt": now,
                        "status": "active", "verificationStatus": verification.get("status", "asserted"),
                        "verification": deepcopy(verification), "sourceScope": record.get("sourceScope", "owner-command"),
                    }
                    if isinstance(record.get("facts"), dict):
                        knowledge["facts"] = deepcopy(record["facts"])
                    data.setdefault("knowledge", []).insert(0, knowledge)
                    data["knowledge"] = data["knowledge"][:200]
                    job["events"].append(_event(job_id, "tool_result", "공용 정보 등록 완료", tool="shared_knowledge.register", input=report["summary"], output=f"공용 정보 {knowledge['id']} 저장"))
                for assignment in job.get("roleAssignments", []):
                    if not isinstance(assignment, dict):
                        continue
                    assignment.update({
                        "status": "completed" if job_success else "failed",
                        "completedAt": now,
                        "evidenceIds": list(verification.get("evidenceIds", []))[:20],
                    })
                for participant in world["agents"]:
                    if participant["id"] not in job["participantIds"]:
                        continue
                    episode = complete_reflection(participant, job, report, verification, now)
                    participant.setdefault("memories", []).append(episode)
                    if job_success:
                        consolidate_procedure(participant, job, verification, now)
                    participant["memories"] = participant["memories"][-20:]
                for peer_id in collaboration_peer_ids:
                    peer = next((item for item in world["agents"] if item["id"] == peer_id), None)
                    if peer is None:
                        continue
                    update_relationship(agent, peer_id, job_success, now)
                    update_relationship(peer, agent["id"], job_success, now)
                for participant in world["agents"]:
                    if participant["id"] not in job["participantIds"]:
                        continue
                    participant["activeJobId"] = None if participant.get("activeJobId") == job_id else participant.get("activeJobId")
                    participant["supportingJobId"] = None if participant.get("supportingJobId") == job_id else participant.get("supportingJobId")
                    participant["currentTool"] = None
                    participant["toolStation"] = None
                    participant["activity"] = "다음 요청을 기다리는 중"
                    participant["focus"] = "공용 오피스 관찰"
                    participant["progress"] = 0
                for station in job.get("claimedStations", []):
                    if world.setdefault("toolOccupancies", {}).get(station, {}).get("jobId") == job_id:
                        world["toolOccupancies"].pop(station, None)
                if job["tool"] == "whiteboard" and report["outcome"] == "completed":
                    self._write_agent_board(data, agent, job, report)
                self._activate_waiting(data)
                world["version"] += 1
                world["updatedAt"] = now
                return True
            await self.store.mutate(complete)

    def _write_agent_board(self, data: dict, agent: dict, job: dict, report: dict) -> None:
        board = data["board"]
        now = utc_now()
        text_blocks = list(board.get("textBlocks") or [])
        candidates = [(8, 58), (132, 58), (8, 124), (132, 124), (8, 190), (132, 190)]
        def overlaps(x: int, y: int) -> bool:
            return any(x < int(item["x"]) + int(item["width"]) and x + 116 > int(item["x"]) and y < int(item["y"]) + int(item["height"]) and y + 58 > int(item["y"]) for item in text_blocks)
        position = next(((x, y) for x, y in candidates if not overlaps(x, y)), None)
        started_new_page = position is None
        if started_new_page:
            text_blocks = []
            position = candidates[0]
        x, y = position
        block = {
            "id": str(uuid.uuid4()), "x": x, "y": y,
            "width": 116, "height": 58, "text": f"{report['title']}\n\n{report['summary'][:240]}",
            "color": "#26394f", "background": "#f9f4dfef", "fontSize": 8,
            "authorType": "agent", "authorId": agent["id"], "authorName": agent["name"],
            "createdAt": now, "updatedAt": now,
        }
        text_blocks.append(block)
        next_board = deepcopy(board)
        next_board.update({
            "title": report["title"][:40], "textBlocks": text_blocks, "version": board["version"] + 1,
            "authorType": "agent", "authorId": agent["id"], "authorName": agent["name"],
            "changeSummary": ("이전 본문을 버전 보관함에 보존하고 새 페이지 생성 · " if started_new_page else "") + f"작업 {job['id'][:8]} 리포트 배치", "updatedAt": now,
        })
        data["board"] = next_board
        data.setdefault("boardVersions", {})[str(next_board["version"])] = deepcopy(next_board)
        data["boardHistory"].insert(0, board_meta(next_board))
        data["boardHistory"] = data["boardHistory"][:BOARD_HISTORY_LIMIT]
        retained = {str(item["version"]) for item in data["boardHistory"]}
        data["boardVersions"] = {key: document for key, document in data["boardVersions"].items() if key in retained}
        job["events"].append(_event(job["id"], "tool_result", f"화이트보드 v{next_board['version']} 저장", tool="whiteboard", input=report["summary"], output=f"텍스트 블록 {block['id']} · 작성자 {agent['name']}"))

    async def _fail_or_retry(self, job_id: str, error: Exception) -> None:
        fallback = None
        async with self.lock:
            def update(data: dict):
                job = data["jobs"].get(job_id)
                if not job or job["status"] != "active":
                    return None
                message = redact_secrets(f"{type(error).__name__}: {error}")[:900]
                failure_signature = f"exception:{type(error).__name__}:{message[:240]}"
                job.setdefault("attemptResults", []).append({
                    "attempt": job.get("attempt", 0), "outcome": "error", "summary": message,
                    "limitations": [message], "qualityIssues": [message], "failureSignature": failure_signature,
                    "retryDirective": "성공한 이전 단계는 반복하지 말고 오류가 난 단계만 재시도한다.", "at": utc_now(),
                })
                job["attemptResults"] = job["attemptResults"][-MAX_ATTEMPTS:]
                if job["attempt"] < MAX_ATTEMPTS and not data["world"]["paused"]:
                    job["phase"] = "working"
                    job["events"].append(_event(job_id, "verification", "실행 오류 후 재계획", "blocked", output=message))
                    job["updatedAt"] = utc_now()
                    return None
                return {
                    "reply": "작업을 완료하지 못해 실패 리포트를 남겼습니다.",
                    "publicDecision": _public_decision(job["command"], job["tool"]),
                    "toolRuns": [], "collaboration": [],
                    "verification": {
                        "status": "rejected", "passed": False, "requiredChecks": [],
                        "evidenceIds": [], "issues": [message],
                    },
                    "report": {
                        "title": "작업 실패 리포트", "outcome": "failed", "summary": "세 차례 실행 후에도 외부 도구 오류가 계속됐습니다.",
                        "body": f"실행 오류: {message}", "limitations": [message], "lessons": ["외부 도구 실패를 재시도 한도와 함께 기록한다."],
                    },
                }
            fallback = await self.store.mutate(update)
        if fallback:
            await self._complete_job(job_id, fallback)
