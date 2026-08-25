from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from world import redact_secrets


URL_PATTERN = re.compile(r"https?://[^\s\)\]\}>\"']+", re.IGNORECASE)
STOPWORDS = {
    "https", "http", "www", "com", "html", "http", "the", "and", "for", "with",
    "페이지", "사이트", "화면", "코드", "열어", "접속", "클릭", "눌러", "입력",
    "살펴", "확인", "해줘", "해주세요", "읽어", "조사", "내용", "관련", "대표",
    "부탁", "요청", "좀", "좀요", "그리고", "이것", "저것", "에서", "으로",
}
RISKY_LABEL = re.compile(
    r"login|sign\s?in|password|checkout|purchase|\bbuy\b|\bpay\b|subscribe|delete|"
    r"submit|publish|\bpost\b|upload|download|로그인|비밀번호|결제|구매|주문|삭제|"
    r"전송|발송|게시|업로드|다운로드|가입|구독",
    re.IGNORECASE,
)
SEARCH_HINT = re.compile(r"검색|찾아|query|search|keyword", re.IGNORECASE)


@dataclass
class BrowseTrace:
    used_refs: set[str] = field(default_factory=set)
    scrolled: int = 0
    last_action: str | None = None
    last_ref: str | None = None
    last_dom_hash: str | None = None
    unchanged_streak: int = 0

    def observe(self, observation: dict[str, Any]) -> None:
        current_hash = str(observation.get("domHash") or "")
        if self.last_action and self.last_dom_hash and current_hash and current_hash == self.last_dom_hash:
            self.unchanged_streak += 1
            if self.last_ref:
                self.used_refs.add(self.last_ref)
        elif current_hash:
            self.unchanged_streak = 0
        if current_hash:
            self.last_dom_hash = current_hash

    def commit(self, action: dict[str, Any]) -> dict[str, Any]:
        self.last_action = str(action.get("action") or "done")
        self.last_ref = str(action.get("ref") or "") or None
        if self.last_action == "scroll":
            self.scrolled += int(action.get("viewports") or 1)
        if self.last_ref:
            self.used_refs.add(self.last_ref)
        return action


def objective_tokens(objective: str) -> set[str]:
    cleaned = URL_PATTERN.sub(" ", objective.lower())
    tokens = {
        token for token in re.findall(r"[0-9a-zA-Z가-힣_-]{2,}", cleaned)
        if token not in STOPWORDS and len(token) > 1
    }
    return tokens


def search_query(objective: str) -> str:
    text = URL_PATTERN.sub(" ", objective)
    text = re.sub(
        r"페이지|사이트|화면|DOM|HTML|코드|열어|접속|클릭|살펴|확인|해줘|해주세요|읽어|조사|부탁",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())[:180]


def _haystack(element: dict[str, Any]) -> str:
    return " ".join(
        str(element.get(key, "") or "")
        for key in ("accessibleName", "href", "placeholder", "name", "role", "tag")
    ).lower()


def _is_safe_element(element: dict[str, Any]) -> bool:
    if not element.get("enabled", True):
        return False
    if element.get("download"):
        return False
    label = str(element.get("accessibleName", "") or "")
    href = str(element.get("href", "") or "")
    if RISKY_LABEL.search(label) or RISKY_LABEL.search(href):
        return False
    if href:
        try:
            parts = urlsplit(href)
        except ValueError:
            return False
        if parts.scheme and parts.scheme.lower() != "https":
            return False
    return True


def _score(element: dict[str, Any], tokens: set[str]) -> float:
    text = _haystack(element)
    if not tokens:
        return 0.15 if element.get("tag") in ("a", "summary") or element.get("disclosureKind") else 0.0
    overlap = sum(1.6 if token in text else 0.0 for token in tokens)
    if element.get("disclosureKind"):
        overlap += 0.4
    if element.get("tag") == "a":
        overlap += 0.2
    return overlap


def _coverage(observation: dict[str, Any], tokens: set[str]) -> float:
    text = f"{observation.get('title', '')} {observation.get('renderedText', '')} {observation.get('ariaSnapshot', '')}".lower()
    if not tokens:
        return 1.0 if len(str(observation.get("renderedText", ""))) >= 80 else 0.2
    hits = sum(1 for token in tokens if token in text)
    return hits / max(1, len(tokens))


def _done(observation: dict[str, Any], objective: str, reason: str) -> dict[str, Any]:
    title = redact_secrets(str(observation.get("title") or ""))[:120]
    excerpt = redact_secrets(str(observation.get("renderedText") or ""))[:240]
    summary = " ".join(part for part in (title, excerpt) if part).strip() or "공개 화면의 렌더링 텍스트를 관찰했습니다."
    return {
        "action": "done",
        "reason": reason,
        "expectedChange": "추가 화면 이동 없이 관찰 근거로 보고",
        "summary": summary[:1000],
    }


def _action(kind: str, element: dict[str, Any] | None, *, reason: str, expected: str, summary: str, value: str | None = None) -> dict[str, Any]:
    result = {
        "action": kind,
        "ref": str(element.get("ref")) if element and element.get("ref") else None,
        "value": value,
        "direction": "down",
        "viewports": 1,
        "reason": reason[:500],
        "expectedChange": expected[:500],
        "summary": summary[:1000],
    }
    if kind != "search":
        result["value"] = None
    return result


def ranked_public_read_actions(
    observation: dict[str, Any],
    objective: str,
    step: int,
    trace: BrowseTrace,
    traits: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return safe next actions, most useful first. Never includes login/pay/submit."""
    trace.observe(observation)
    tokens = objective_tokens(objective)
    coverage = _coverage(observation, tokens)
    conscientiousness = float((traits or {}).get("conscientiousness", 0.9) or 0.9)
    text_len = len(str(observation.get("renderedText") or ""))
    elements = [item for item in observation.get("interactiveElements", []) if isinstance(item, dict) and _is_safe_element(item)]
    candidates: list[tuple[float, dict[str, Any]]] = []

    if SEARCH_HINT.search(objective):
        for element in elements:
            if str(element.get("ref")) in trace.used_refs:
                continue
            field_name = str(element.get("name", "")).strip().lower()
            if element.get("tag") not in ("input", "textarea"):
                continue
            if element.get("type") not in ("", "text", "search"):
                continue
            if element.get("formMethod") and element.get("formMethod") != "get":
                continue
            if element.get("type") != "search" and field_name not in {"q", "query", "search", "search_query", "keyword", "keywords", "term"}:
                continue
            query = search_query(objective)
            if not query:
                continue
            score = 4.0 + _score(element, tokens)
            candidates.append((score, _action(
                "search", element,
                value=query,
                reason="명령의 검색 의도를 현재 페이지의 공개 GET 검색창으로 실행",
                expected="검색 결과 화면의 DOM/ARIA를 다시 관찰",
                summary=f"공개 검색어로 결과를 관찰합니다: {query}",
            )))

    for element in elements:
        ref = str(element.get("ref") or "")
        if not ref or ref in trace.used_refs:
            continue
        kind = None
        if element.get("disclosureKind") and str(element.get("expanded", "")).lower() != "true":
            kind = "click_disclosure"
        elif element.get("tag") == "a" and element.get("href"):
            kind = "follow_link"
        if not kind:
            continue
        score = _score(element, tokens)
        if score < 0.2 and tokens:
            continue
        label = redact_secrets(str(element.get("accessibleName") or ref))[:80]
        if kind == "click_disclosure":
            candidates.append((score + 0.8, _action(
                kind, element,
                reason=f"접힌 공개 상세 '{label}'를 펼쳐 본문을 관찰",
                expected="펼친 뒤 텍스트와 DOM 해시가 변한다",
                summary=f"공개 상세 '{label}'를 펼쳐 내용을 확인합니다.",
            )))
        else:
            candidates.append((score, _action(
                kind, element,
                reason=f"명령과 관련된 공개 링크 '{label}'로 이동",
                expected="다음 공개 페이지의 DOM/ARIA를 관찰",
                summary=f"관련 공개 문서 '{label}'로 이동합니다.",
            )))

    if trace.scrolled < 2 and (coverage < 0.55 or text_len < 240 or step == 1):
        candidates.append((1.1 if coverage < 0.35 else 0.6, _action(
            "scroll", None,
            reason="첫 화면 아래에서 명령과 관련된 공개 본문을 더 읽는다",
            expected="스크롤 뒤 화면 텍스트와 DOM 해시를 다시 기록",
            summary="현재 페이지의 아래 본문을 읽기 전용으로 관찰합니다.",
        )))

    evidence_ready = coverage >= (0.28 + (0.22 * conscientiousness)) and text_len >= 80
    if evidence_ready and step > 1:
        candidates.append((0.35, _done(
            observation, objective,
            "명령과 겹치는 공개 본문을 충분히 관찰해 보고 근거로 사용",
        )))
    elif step >= 6 or trace.unchanged_streak >= 2 or (not candidates and step > 1):
        candidates.append((0.2, _done(
            observation, objective,
            "더 이상 안전한 공개 읽기 동작이 없어 현재 관찰만 보고",
        )))
    elif step == 1 and not candidates:
        candidates.append((0.5, _action(
            "scroll", None,
            reason="상호작용 요소가 거의 없어 화면을 더 읽어 근거를 확보",
            expected="추가 본문 관찰",
            summary="공개 화면의 본문을 더 관찰합니다.",
        )))

    candidates.sort(key=lambda item: item[0], reverse=True)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _rank, action in candidates:
        key = (str(action.get("action")), str(action.get("ref") or action.get("direction") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(action)
        if len(unique) >= 5:
            break
    return unique or [_done(observation, objective, "관찰 가능한 공개 화면만 보고합니다.")]


def next_public_read_action(
    observation: dict[str, Any],
    objective: str,
    step: int,
    trace: BrowseTrace,
    traits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ranked = ranked_public_read_actions(observation, objective, step, trace, traits)
    return trace.commit(ranked[0])
