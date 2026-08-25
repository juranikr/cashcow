from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from playwright.async_api import Browser, BrowserContext, Page, Route, WebSocketRoute, async_playwright

from egress import PinnedHttpsProxy
from policy import (
    MAX_DNS_WORKERS,
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


MAX_SESSIONS = 1
MAX_ACTIONS = 8
MAX_NAVIGATIONS = 4
MAX_NETWORK_REQUESTS = 120
SESSION_TTL_SECONDS = 75
SESSION_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
SAFE_RESOURCE_TYPES = frozenset(
    {"document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "manifest", "other"}
)

BROWSER_ENV_ALLOWLIST = frozenset(
    {
        "FONTCONFIG_PATH",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
    }
)
FORBIDDEN_WORKER_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_CLIENT_SECRET",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "RUNTIME_SERVICE_TOKEN",
        "ALL_PROXY",
        "CLOUDSDK_CONFIG",
        "DOCKER_CONFIG",
        "GIT_ASKPASS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "KUBECONFIG",
        "NETRC",
        "SSH_AUTH_SOCK",
    }
)
SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:ACCESS_KEY|API_KEY|CREDENTIALS?|PASSWORD|PRIVATE_KEY|SECRET|TOKENS?)(?:$|_)",
    re.IGNORECASE,
)


def present_forbidden_worker_environment() -> tuple[str, ...]:
    """Return credential-bearing variables that must never reach this worker."""
    return tuple(
        sorted(
            name
            for name, value in os.environ.items()
            if value and (name.upper() in FORBIDDEN_WORKER_ENV or SENSITIVE_ENV_NAME.search(name))
        )
    )

BROWSER_HARDENING_SCRIPT = r"""() => {
  const hide = (name) => {
    try { Object.defineProperty(globalThis, name, {value: undefined, configurable: false, writable: false}); } catch (_) {}
  };
  ['WebSocket', 'WebTransport', 'EventSource', 'RTCPeerConnection', 'webkitRTCPeerConnection',
   'Worker', 'SharedWorker', 'PaymentRequest', 'PasswordCredential', 'FederatedCredential',
   'PublicKeyCredential', 'Notification', 'showOpenFilePicker', 'showSaveFilePicker',
   'showDirectoryPicker'].forEach(hide);
  try { Object.defineProperty(navigator, 'sendBeacon', {value: () => false, configurable: false}); } catch (_) {}
  try { Object.defineProperty(navigator, 'credentials', {value: undefined, configurable: false}); } catch (_) {}
  try { Object.defineProperty(navigator, 'clipboard', {value: undefined, configurable: false}); } catch (_) {}
  try { Object.defineProperty(navigator, 'registerProtocolHandler', {value: undefined, configurable: false}); } catch (_) {}
  try { Object.defineProperty(window, 'open', {value: () => null, configurable: false}); } catch (_) {}
  try {
    HTMLFormElement.prototype.submit = function() { throw new DOMException('Blocked by read-only policy', 'SecurityError'); };
    HTMLFormElement.prototype.requestSubmit = function() { throw new DOMException('Blocked by read-only policy', 'SecurityError'); };
    addEventListener('submit', event => {
      event.preventDefault(); event.stopImmediatePropagation();
    }, true);
  } catch (_) {}
  try {
    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, ...args) {
      if (!['GET', 'HEAD'].includes(String(method).toUpperCase()))
        throw new DOMException('Blocked by read-only policy', 'SecurityError');
      return originalOpen.call(this, method, ...args);
    };
  } catch (_) {}
  try {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = function(resource, init = {}) {
      const method = String(init.method || (resource && resource.method) || 'GET').toUpperCase();
      if (!['GET', 'HEAD'].includes(method))
        return Promise.reject(new DOMException('Blocked by read-only policy', 'SecurityError'));
      return originalFetch.call(this, resource, init);
    };
  } catch (_) {}
}"""


def search_navigation_url(metadata: dict[str, Any], value: str, current_url: str) -> str:
    field_name = str(metadata.get("name", "")).strip()
    form_action = str(metadata.get("formAction", "")).strip()
    if not field_name or len(field_name) > 180 or not form_action:
        raise BrowserPolicyError("명시적인 공개 GET 검색 폼만 사용할 수 있습니다.")
    try:
        action_parts = urlsplit(form_action)
        current_parts = urlsplit(current_url)
        action_port = action_parts.port
        current_port = current_parts.port
    except ValueError as error:
        raise BrowserPolicyError("유효하지 않은 검색 폼 URL입니다.") from error
    if (
        action_parts.scheme.lower() != "https"
        or (action_parts.hostname or "").rstrip(".").lower() != (current_parts.hostname or "").rstrip(".").lower()
        or action_port not in (None, 443)
        or current_port not in (None, 443)
    ):
        raise BrowserPolicyError("검색은 현재 공개 사이트의 HTTPS GET 폼으로만 제한됩니다.")
    query = urlencode([(field_name, value)])
    return urlunsplit(("https", action_parts.netloc, action_parts.path or "/", query, ""))


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    objective: str | None = Field(default=None, max_length=800)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    observationVersion: int = Field(ge=1)
    action: Literal["follow_link", "click_disclosure", "search", "scroll"]
    ref: str | None = Field(default=None, max_length=80)
    value: str | None = Field(default=None, max_length=180)
    direction: Literal["up", "down"] | None = None
    viewports: int = Field(default=1, ge=1, le=3)


@dataclass
class BrowserSession:
    id: str
    context: BrowserContext
    page: Page
    created_at: float = field(default_factory=time.monotonic)
    observation_version: int = 0
    actions: int = 0
    navigations: int = 1
    last_elements: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_dom_hash: str = ""
    last_status: int | None = None
    network_enabled: bool = False
    main_navigation_seen: bool = False
    attempted_requests: int = 0
    network_requests: int = 0
    blocked_request_count: int = 0
    blocked_requests: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Worker:
    def __init__(self) -> None:
        self.playwright = None
        self.browser: Browser | None = None
        self.proxy = PinnedHttpsProxy()
        self.sessions: dict[str, BrowserSession] = {}
        self.lock = asyncio.Lock()
        self.dns_validation_slots = asyncio.Semaphore(MAX_DNS_WORKERS)

    async def _validate_public_url(self, url: str):
        async with self.dns_validation_slots:
            return await validate_public_url(url)

    async def start(self) -> None:
        forbidden_environment = present_forbidden_worker_environment()
        if forbidden_environment:
            raise RuntimeError(
                "browser worker refuses credential-bearing environment: "
                + ", ".join(forbidden_environment)
            )
        await self.proxy.start()
        try:
            self.playwright = await async_playwright().start()
            browser_env = {key: value for key, value in os.environ.items() if key in BROWSER_ENV_ALLOWLIST}
            self.browser = await self.playwright.chromium.launch(
                channel="chromium",
                headless=True,
                chromium_sandbox=True,
                env=browser_env,
                proxy={"server": self.proxy.url, "bypass": "<-loopback>"},
                args=[
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-domain-reliability",
                    "--disable-extensions",
                    "--disable-quic",
                    "--disable-sync",
                    "--dns-over-https-mode=off",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    "--no-first-run",
                    "--proxy-bypass-list=<-loopback>",
                ],
            )
        except Exception:
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            await self.proxy.stop()
            raise

    async def stop(self) -> None:
        for session_id in list(self.sessions):
            await self.close(session_id)
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        await self.proxy.stop()

    async def _purge_expired(self) -> None:
        expired = [session.id for session in self.sessions.values() if time.monotonic() - session.created_at > SESSION_TTL_SECONDS]
        for session_id in expired:
            await self.close(session_id)

    @staticmethod
    def _blocked(session: BrowserSession, reason: str, url: str) -> None:
        session.blocked_request_count += 1
        try:
            host = (urlsplit(url).hostname or "unknown").rstrip(".").lower()
        except ValueError:
            host = "invalid"
        session.blocked_requests.append(f"{reason}:{host[:160]}")

    async def _route(self, session: BrowserSession, route: Route) -> None:
        request = route.request
        url = request.url
        if not allowed_resource_scheme(url):
            self._blocked(session, "scheme", url)
            await route.abort("blockedbyclient")
            return
        if url.startswith(("data:", "blob:")):
            if request.is_navigation_request():
                self._blocked(session, "non-https-navigation", url)
                await route.abort("blockedbyclient")
                return
            await route.continue_()
            return
        session.attempted_requests += 1
        if session.attempted_requests > MAX_NETWORK_REQUESTS:
            self._blocked(session, "request-budget", url)
            await route.abort("blockedbyclient")
            return
        if is_risky_url(url):
            self._blocked(session, "risky-url", url)
            await route.abort("blockedbyclient")
            return
        if has_credential_query(url):
            self._blocked(session, "credential-query", url)
            await route.abort("blockedbyclient")
            return
        if not session.network_enabled:
            self._blocked(session, "network-frozen", url)
            await route.abort("blockedbyclient")
            return
        if not is_safe_http_method(request.method) or request.post_data:
            self._blocked(session, f"method-{request.method.upper()}", url)
            await route.abort("blockedbyclient")
            return
        if request.resource_type not in SAFE_RESOURCE_TYPES:
            self._blocked(session, f"resource-{request.resource_type}", url)
            await route.abort("blockedbyclient")
            return
        try:
            if request.frame.page != session.page:
                self._blocked(session, "unexpected-page", url)
                await route.abort("blockedbyclient")
                return
        except Exception:
            self._blocked(session, "unattributed-request", url)
            await route.abort("blockedbyclient")
            return
        if request.is_navigation_request() and request.frame == session.page.main_frame:
            redirected_from = getattr(request, "redirected_from", None)
            if session.main_navigation_seen and redirected_from is None:
                self._blocked(session, "scripted-main-navigation", url)
                await route.abort("blockedbyclient")
                return
            session.main_navigation_seen = True
        try:
            headers = await request.all_headers()
        except Exception:
            self._blocked(session, "unreadable-headers", url)
            await route.abort("blockedbyclient")
            return
        if has_credential_headers(headers):
            self._blocked(session, "credential-header", url)
            await route.abort("blockedbyclient")
            return
        lowered_headers = {name.lower(): value for name, value in headers.items()}
        if (
            lowered_headers.get("upgrade")
            or "upgrade" in lowered_headers.get("connection", "").lower()
            or any(name.startswith("sec-websocket-") for name in lowered_headers)
        ):
            self._blocked(session, "protocol-upgrade", url)
            await route.abort("blockedbyclient")
            return
        try:
            validated = await self._validate_public_url(url)
            try:
                still_current_page = request.frame.page == session.page
            except Exception:
                still_current_page = False
            if not session.network_enabled or not still_current_page:
                self._blocked(session, "network-frozen-after-dns", url)
                await route.abort("blockedbyclient")
                return
            self.proxy.authorize(session.id, validated)
        except BrowserPolicyError as error:
            self._blocked(session, type(error).__name__, url)
            await route.abort("blockedbyclient")
            return
        session.network_requests += 1
        await route.continue_(headers=without_credential_headers(headers))

    async def _freeze_non_https_navigation(self, session: BrowserSession, page: Page) -> None:
        if page is not session.page or urlsplit(page.url).scheme.lower() == "https":
            return
        self._blocked(session, "committed-non-https-navigation", page.url)
        session.network_enabled = False
        with suppress(Exception):
            await session.context.set_offline(True)
        await self.proxy.revoke(session.id)

    def _install_page_guards(self, session: BrowserSession, page: Page) -> None:
        page.on("download", lambda download: asyncio.create_task(download.cancel()))
        page.on("popup", lambda popup: asyncio.create_task(popup.close()))
        page.on(
            "framenavigated",
            lambda frame: asyncio.create_task(self._freeze_non_https_navigation(session, page))
            if frame == page.main_frame
            else None,
        )

    async def _new_browser_context(self) -> BrowserContext:
        if not self.browser:
            raise RuntimeError("브라우저가 준비되지 않았습니다.")
        return await self.browser.new_context(
            accept_downloads=False,
            service_workers="block",
            locale="ko-KR",
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True,
            permissions=[],
            offline=True,
        )

    async def _replace_isolated_context(self, session: BrowserSession) -> None:
        old_context = session.context
        await self.proxy.revoke(session.id)
        with suppress(Exception):
            await old_context.set_offline(True)
        await old_context.close()

        context = await self._new_browser_context()
        session.context = context
        await context.route("**/*", lambda route: self._route(session, route))
        await context.add_init_script(script=BROWSER_HARDENING_SCRIPT)

        async def block_socket(socket: WebSocketRoute) -> None:
            self._blocked(session, "websocket", socket.url)
            await socket.close(code=1008, reason="Cashcow public-read policy")

        await context.route_web_socket("**/*", block_socket)
        session.main_navigation_seen = False
        session.page = await context.new_page()
        self._install_page_guards(session, session.page)

    async def _navigate(self, session: BrowserSession, url: str) -> None:
        if is_risky_url(url):
            raise BrowserPolicyError("상태 변경·인증·다운로드 가능성이 있는 URL은 열 수 없습니다.")
        if has_credential_query(url):
            raise BrowserPolicyError("자격 정보로 보이는 쿼리가 포함된 URL은 열 수 없습니다.")
        validated = await self._validate_public_url(url)
        session.network_enabled = False
        await self._replace_isolated_context(session)
        session.network_enabled = True
        await session.context.set_offline(False)
        final_url: str | None = None
        try:
            response = await session.page.goto(
                validated.url,
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            session.last_status = response.status if response else None
            final_url = session.page.url
        finally:
            session.network_enabled = False
            try:
                await session.context.set_offline(True)
            finally:
                await self.proxy.revoke(session.id)
        if final_url is None or is_risky_url(final_url):
            raise BrowserPolicyError("렌더링 결과가 상태 변경 가능성이 있는 URL로 이동했습니다.")
        if has_credential_query(final_url):
            raise BrowserPolicyError("렌더링 결과 URL에 자격 정보로 보이는 쿼리가 포함되었습니다.")
        await self._validate_public_url(final_url)

    async def open(self, request: OpenRequest) -> dict[str, Any]:
        if is_risky_url(request.url):
            raise BrowserPolicyError("상태 변경·인증·다운로드 가능성이 있는 URL은 열 수 없습니다.")
        if has_credential_query(request.url):
            raise BrowserPolicyError("자격 정보로 보이는 쿼리가 포함된 URL은 열 수 없습니다.")
        validated = await self._validate_public_url(request.url)
        async with self.lock:
            await self._purge_expired()
            if len(self.sessions) >= MAX_SESSIONS:
                raise BrowserPolicyError("격리 브라우저가 다른 공개 페이지를 처리 중입니다.")
            if not self.browser:
                raise RuntimeError("브라우저가 준비되지 않았습니다.")
            context = await self._new_browser_context()
            page = await context.new_page()
            session = BrowserSession(str(uuid.uuid4()), context, page)
            self.sessions[session.id] = session
            try:
                await self._navigate(session, validated.url)
                observation = await self._observe(session)
                return {
                    "sessionId": session.id,
                    "securityBoundary": "isolated-public-https-read-only-worker",
                    "observation": observation,
                }
            except Exception:
                await self.close(session.id)
                raise

    async def observe(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError("브라우저 세션을 찾지 못했습니다.")
        async with session.operation_lock:
            if time.monotonic() - session.created_at > SESSION_TTL_SECONDS:
                await self._close_unlocked(session_id, session)
                raise BrowserPolicyError("브라우저 세션 제한 시간이 지났습니다.")
            return await self._observe(session)

    async def _observe(self, session: BrowserSession) -> dict[str, Any]:
        if is_risky_url(session.page.url):
            raise BrowserPolicyError("상태 변경 가능성이 있는 화면은 관찰할 수 없습니다.")
        if has_credential_query(session.page.url):
            raise BrowserPolicyError("자격 정보로 보이는 URL은 관찰할 수 없습니다.")
        current_url = await self._validate_public_url(session.page.url)
        await session.page.locator("body").wait_for(state="attached", timeout=5_000)
        extracted = await session.page.evaluate(
            r"""() => {
              const cap = (value, length) => String(value || '').slice(0, length);
              const visible = (el) => {
                const s = getComputedStyle(el); const r = el.getBoundingClientRect();
                return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 1 && r.height > 1;
              };
              const nodes = [...document.querySelectorAll('a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="tab"],[aria-expanded]')]
                .filter(visible).slice(0, 80);
              const elements = nodes.map((el, i) => {
                const ref = `cc-${i + 1}`; el.setAttribute('data-cashcow-ref', ref);
                const form = el.form || el.closest('form');
                const tag = el.tagName.toLowerCase();
                const role = cap(el.getAttribute('role'), 40).toLowerCase();
                const name = cap(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.getAttribute('placeholder'), 180)
                  .replace(/\s+/g, ' ').trim();
                const rawHref = cap(el.href, 4096);
                const rawFormAction = cap(form && form.action, 4096);
                const disclosureKind = tag === 'summary' && el.closest('details') ? 'details'
                  : el.hasAttribute('aria-expanded') ? 'aria-expanded'
                  : role === 'tab' ? 'tab' : '';
                return {ref, tag, role, accessibleName: name,
                  type: cap(el.type || el.getAttribute('type'), 40).toLowerCase(),
                  name: cap(el.getAttribute('name'), 180), placeholder: cap(el.getAttribute('placeholder'), 180),
                  href: rawHref.length <= 2048 ? rawHref : '', enabled: !el.matches(':disabled'),
                  expanded: cap(el.getAttribute('aria-expanded'), 20), disclosureKind,
                  formMethod: form ? cap(form.method || 'get', 20).toLowerCase() : '',
                  formAction: rawFormAction.length <= 2048 ? rawFormAction : '',
                  download: el.hasAttribute('download')};
              });
              const clone = document.documentElement.cloneNode(true);
              clone.querySelectorAll('script,style,noscript,svg,canvas,template').forEach(el => el.remove());
              clone.querySelectorAll('input,textarea,select').forEach(el => { el.removeAttribute('value'); el.textContent = ''; });
              clone.querySelectorAll('[srcdoc]').forEach(el => el.removeAttribute('srcdoc'));
              return {elements, html: clone.outerHTML.slice(0, 14000), text: (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 9000)};
            }"""
        )
        try:
            aria = await session.page.locator("body").aria_snapshot(mode="ai", boxes=True, depth=5, timeout=5_000)
        except Exception:
            aria = "접근성 트리를 생성하지 못했습니다."
        screenshot = await session.page.screenshot(type="webp", quality=50, full_page=False, animations="disabled")
        content_hash = hashlib.sha256((extracted["html"] + current_url.url).encode("utf-8")).hexdigest()
        session.observation_version += 1
        session.last_elements = {item["ref"]: item for item in extracted["elements"]}
        previous_hash = session.last_dom_hash
        session.last_dom_hash = content_hash
        return {
            "observationVersion": session.observation_version,
            "url": current_url.url,
            "title": (await session.page.title())[:300],
            "httpStatus": session.last_status,
            "renderedText": extracted["text"],
            "renderedHtmlExcerpt": extracted["html"],
            "ariaSnapshot": aria[:18000],
            "interactiveElements": extracted["elements"],
            "domHash": content_hash,
            "previousDomHash": previous_hash or None,
            "domChanged": bool(previous_hash and previous_hash != content_hash),
            "screenshotSha256": hashlib.sha256(screenshot).hexdigest(),
            "screenshotViewport": "1280x720 WebP",
            "blockedRequestCount": session.blocked_request_count,
            "attemptedRequestCount": session.attempted_requests,
            "allowedRequestCount": session.network_requests,
            "blockedRequestSamples": list(session.blocked_requests)[-5:],
            "untrustedObservation": True,
        }

    async def act(self, session_id: str, request: ActionRequest) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError("브라우저 세션을 찾지 못했습니다.")
        async with session.operation_lock:
            if time.monotonic() - session.created_at > SESSION_TTL_SECONDS:
                await self._close_unlocked(session_id, session)
                raise BrowserPolicyError("브라우저 세션 제한 시간이 지났습니다.")
            if request.observationVersion != session.observation_version:
                raise BrowserPolicyError("이전 화면의 요소 참조는 재사용할 수 없습니다.")
            if session.actions >= MAX_ACTIONS:
                raise BrowserPolicyError("한 작업의 안전 행동 한도를 초과했습니다.")
            action = request.action
            if action == "scroll":
                await session.context.set_offline(True)
                direction = -1 if request.direction == "up" else 1
                await session.page.mouse.wheel(0, 650 * request.viewports * direction)
                await session.page.wait_for_timeout(100)
            else:
                metadata = session.last_elements.get(request.ref or "")
                if not metadata:
                    raise BrowserPolicyError("현재 화면에 없는 요소 참조입니다.")
                if not metadata.get("enabled"):
                    raise BrowserPolicyError("비활성 요소는 조작할 수 없습니다.")
                label = str(metadata.get("accessibleName", ""))
                if is_risky_label(label):
                    raise BrowserPolicyError("로그인·전송·결제·삭제 등 상태 변경 가능성이 있는 동작은 차단됩니다.")
                if action == "follow_link":
                    if metadata.get("tag") != "a" or not metadata.get("href") or metadata.get("download"):
                        raise BrowserPolicyError("공개 링크 이동만 허용됩니다.")
                    if session.navigations >= MAX_NAVIGATIONS:
                        raise BrowserPolicyError("페이지 이동 한도를 초과했습니다.")
                    await self._navigate(session, str(metadata["href"]))
                    session.navigations += 1
                elif action == "click_disclosure":
                    if not metadata.get("disclosureKind"):
                        raise BrowserPolicyError("탭·상세보기·접기/펼치기만 클릭할 수 있습니다.")
                    if metadata.get("type") == "submit" or metadata.get("formMethod"):
                        raise BrowserPolicyError("폼과 결합된 요소는 자동 클릭하지 않습니다.")
                    await session.context.set_offline(True)
                    locator = session.page.locator(f'[data-cashcow-ref="{request.ref}"]')
                    await locator.evaluate("(element) => element.click()", timeout=5_000)
                    await session.page.wait_for_timeout(100)
                elif action == "search":
                    if session.navigations >= MAX_NAVIGATIONS:
                        raise BrowserPolicyError("페이지 이동 한도를 초과했습니다.")
                    safe_search_name = str(metadata.get("name", "")).strip().lower() in {
                        "q", "query", "search", "search_query", "keyword", "keywords", "term"
                    }
                    if (
                        metadata.get("tag") not in ("input", "textarea")
                        or metadata.get("type") not in ("", "text", "search")
                        or (metadata.get("type") != "search" and not safe_search_name)
                    ):
                        raise BrowserPolicyError("명시적인 공개 검색 입력란만 사용할 수 있습니다.")
                    if is_sensitive_field(metadata) or metadata.get("formMethod") != "get":
                        raise BrowserPolicyError("민감 입력 또는 상태 변경 폼은 자동 입력하지 않습니다.")
                    value = (request.value or "").strip()
                    if not value:
                        raise BrowserPolicyError("검색어가 비어 있습니다.")
                    target = search_navigation_url(metadata, value, session.page.url)
                    await self._navigate(session, target)
                    session.navigations += 1
            session.actions += 1
            observation = await self._observe(session)
            return {"action": action, "ref": request.ref, "observation": observation}

    async def _close_unlocked(self, session_id: str, session: BrowserSession) -> None:
        if self.sessions.get(session_id) is not session:
            return
        self.sessions.pop(session_id, None)
        await self.proxy.revoke(session.id)
        with suppress(Exception):
            await session.context.close()

    async def close(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if not session:
            return
        async with session.operation_lock:
            await self._close_unlocked(session_id, session)


worker = Worker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await worker.start()
    try:
        yield
    finally:
        await worker.stop()


app = FastAPI(
    title="Cashcow Isolated Browser Worker",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def fail(error: Exception) -> HTTPException:
    if isinstance(error, (BrowserPolicyError, KeyError)):
        return HTTPException(status_code=400 if isinstance(error, BrowserPolicyError) else 404, detail=str(error))
    return HTTPException(status_code=502, detail=f"격리 브라우저 실행 실패: {type(error).__name__}")


@app.get("/health")
async def health():
    forbidden_environment = present_forbidden_worker_environment()
    aws_credentials_present = any(
        name.startswith("AWS_") for name in forbidden_environment
    )
    groq_key_present = "GROQ_API_KEY" in forbidden_environment
    return {
        "status": "ok" if worker.browser and worker.browser.is_connected() and worker.proxy.running else "degraded",
        "buildSha": os.getenv("BROWSER_WORKER_BUILD_SHA", "unknown"),
        "activeSessions": len(worker.sessions),
        "awsCredentialsPresent": aws_credentials_present,
        "groqKeyPresent": groq_key_present,
        "credentialIsolation": not forbidden_environment,
        "forbiddenCredentialEnvironmentPresent": bool(forbidden_environment),
        "networkProxy": worker.proxy.running,
        "pinnedHttpsProxy": worker.proxy.running,
        "chromiumSandboxRequired": True,
        "publicHttpsOnly": True,
        "safeMethodsOnly": True,
        "proxyAllowedConnections": worker.proxy.allowed_connections,
        "proxyBlockedConnections": worker.proxy.blocked_connections,
    }


@app.post("/sessions")
async def open_session(request: OpenRequest):
    try:
        return await worker.open(request)
    except Exception as error:
        raise fail(error) from error


@app.post("/sessions/{session_id}/observe")
async def observe(session_id: str = Path(min_length=36, max_length=36, pattern=SESSION_ID_PATTERN)):
    try:
        return await worker.observe(session_id)
    except Exception as error:
        raise fail(error) from error


@app.post("/sessions/{session_id}/actions")
async def act(
    request: ActionRequest,
    session_id: str = Path(min_length=36, max_length=36, pattern=SESSION_ID_PATTERN),
):
    try:
        return await worker.act(session_id, request)
    except Exception as error:
        raise fail(error) from error


@app.delete("/sessions/{session_id}")
async def close(session_id: str = Path(min_length=36, max_length=36, pattern=SESSION_ID_PATTERN)):
    await worker.close(session_id)
    return {"closed": True}
