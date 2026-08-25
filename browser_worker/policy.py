from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import SplitResult, parse_qsl, unquote_plus, urlsplit, urlunsplit


BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.aws.internal",
    "instance-data",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")
METADATA_IPS = {"169.254.169.254", "169.254.170.2"}
MAX_URL_LENGTH = 2048
MAX_DNS_ADDRESSES = 16
MAX_DNS_WORKERS = 8
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD"})
BLOCKED_CREDENTIAL_HEADERS = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "cookie2",
        "proxy-authorization",
        "x-access-token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-goog-api-key",
        "x-session-token",
        "x-xsrf-token",
    }
)
BLOCKED_IPV6_TRANSITION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("2001::/32"),
    ipaddress.ip_network("2002::/16"),
)
DNS_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_DNS_WORKERS, thread_name_prefix="cashcow-public-dns")


class BrowserPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    hostname: str
    resolved_ips: tuple[str, ...]


def _canonical_hostname(raw_hostname: str) -> str:
    candidate = raw_hostname.rstrip(".")
    # Scoped IPv6 literals are meaningful only relative to a local interface and
    # create parser/route ambiguity. Percent-escaped hostnames are never needed
    # for a public browser destination.
    if "%" in candidate:
        raise BrowserPolicyError("URL 호스트에는 퍼센트 또는 IPv6 영역 ID를 사용할 수 없습니다.")
    try:
        literal = ipaddress.ip_address(candidate.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal)
    try:
        hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise BrowserPolicyError("유효하지 않은 국제화 도메인입니다.") from error
    if (
        not hostname
        or len(hostname) > 253
        or "%" in hostname
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not label.replace("-", "").isalnum()
            for label in hostname.split(".")
        )
    ):
        raise BrowserPolicyError("유효하지 않은 URL 호스트입니다.")
    return hostname


def _normalized_parts(raw_url: str) -> tuple[SplitResult, str]:
    value = raw_url.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise BrowserPolicyError("URL이 비어 있거나 허용 길이를 초과했습니다.")
    # WHATWG URL parsing treats backslashes as path separators for HTTPS while
    # urllib does not. Reject them (and raw controls) rather than risk the
    # validator and Chromium disagreeing about the authority.
    if "\\" in value or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
        raise BrowserPolicyError("URL에 허용되지 않는 문자가 있습니다.")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise BrowserPolicyError("유효하지 않은 URL입니다.") from error
    if parts.scheme.lower() != "https":
        raise BrowserPolicyError("격리 브라우저는 공개 HTTPS URL만 엽니다.")
    if parts.username or parts.password:
        raise BrowserPolicyError("사용자 정보가 포함된 URL은 열 수 없습니다.")
    if not parts.hostname:
        raise BrowserPolicyError("URL 호스트가 없습니다.")
    try:
        port = parts.port
    except ValueError as error:
        raise BrowserPolicyError("유효하지 않은 URL 포트입니다.") from error
    if port not in (None, 443):
        raise BrowserPolicyError("비표준 포트는 열 수 없습니다.")
    hostname = _canonical_hostname(parts.hostname)
    if hostname in BLOCKED_HOSTS or hostname.endswith(BLOCKED_SUFFIXES):
        raise BrowserPolicyError("내부 또는 로컬 호스트는 열 수 없습니다.")
    normalized_netloc = f"[{hostname}]" if ":" in hostname else hostname
    normalized = urlunsplit(("https", normalized_netloc, parts.path or "/", parts.query, ""))
    return parts, normalized


def assert_public_ip(raw_ip: str) -> str:
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError as error:
        raise BrowserPolicyError("DNS가 유효하지 않은 주소를 반환했습니다.") from error
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    if (
        str(address) in METADATA_IPS
        or (
            address.version == 6
            and any(address in network for network in BLOCKED_IPV6_TRANSITION_NETWORKS)
        )
        or not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise BrowserPolicyError("공개 인터넷 주소가 아닌 목적지는 차단됩니다.")
    return str(address)


def normalize_public_https_url(raw_url: str) -> tuple[str, str]:
    """Validate URL syntax/authority without resolving it."""
    parts, normalized = _normalized_parts(raw_url)
    return normalized, _canonical_hostname(parts.hostname or "")


async def validate_public_url(raw_url: str) -> ValidatedUrl:
    normalized, hostname = normalize_public_https_url(raw_url)
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (assert_public_ip(str(literal)),)
    else:
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.run_in_executor(
                    DNS_EXECUTOR,
                    lambda: socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM),
                ),
                timeout=3.0,
            )
        except (asyncio.TimeoutError, socket.gaierror) as error:
            raise BrowserPolicyError("공개 DNS 확인에 실패했습니다.") from error
        public_addresses = {assert_public_ip(record[4][0]) for record in records}
        addresses = tuple(
            sorted(public_addresses, key=lambda value: (ipaddress.ip_address(value).version, value))
        )
        if not addresses:
            raise BrowserPolicyError("DNS 주소를 찾지 못했습니다.")
        if len(addresses) > MAX_DNS_ADDRESSES:
            raise BrowserPolicyError("DNS가 허용된 수보다 많은 주소를 반환했습니다.")
    return ValidatedUrl(normalized, hostname, addresses)


def allowed_resource_scheme(raw_url: str) -> bool:
    try:
        return urlsplit(raw_url).scheme.lower() in {"https", "data", "blob"}
    except ValueError:
        return False


def is_safe_http_method(method: str) -> bool:
    return method.upper() in SAFE_HTTP_METHODS


def without_credential_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop ambient credentials and common app-token headers before egress."""
    return {
        key: value
        for key, value in headers.items()
        if not is_credential_header_name(key)
    }


def is_credential_header_name(raw_name: str) -> bool:
    normalized = raw_name.strip().lower().replace("-", "_")
    exact = {name.replace("-", "_") for name in BLOCKED_CREDENTIAL_HEADERS}
    return normalized in exact or normalized in {
        "credential", "key", "password", "secret", "signature", "token"
    } or normalized.endswith(
        (
            "_access_key",
            "_api_key",
            "_auth_token",
            "_authorization",
            "_credential",
            "_credentials",
            "_csrf_token",
            "_password",
            "_secret",
            "_session_token",
            "_signature",
            "_token",
            "_xsrf_token",
        )
    )


def has_credential_headers(headers: Mapping[str, str]) -> bool:
    return any(value and is_credential_header_name(name) for name, value in headers.items())


RISKY_ACTION_TERMS = (
    "login", "log in", "sign in", "signin", "password", "otp", "verify account",
    "buy", "purchase", "checkout", "pay", "subscribe", "delete", "remove account",
    "send", "submit", "publish", "post", "upload", "download", "permission",
    "로그인", "비밀번호", "인증번호", "결제", "구매", "주문", "삭제", "전송", "발송",
    "게시", "등록", "업로드", "다운로드", "권한", "구독",
    "save", "create", "update", "cancel", "accept", "approve", "join", "register",
    "authorize", "connect", "revoke", "reset", "invite",
    "저장", "수정", "취소", "승인", "참여", "가입", "초대",
)
SENSITIVE_FIELD_TERMS = (
    "password", "passcode", "otp", "email", "phone", "card", "cvv", "token", "secret", "key",
    "비밀번호", "인증번호", "이메일", "전화", "카드", "토큰", "비밀", "키",
)
RISKY_URL_TERMS = frozenset(
    {
        "activate", "add-to-cart", "add_to_cart", "buy", "checkout", "confirm",
        "delete", "destroy", "download", "follow", "like", "login", "logout", "order",
        "pay", "payment", "publish", "purchase", "remove", "send", "signin", "signout", "submit",
        "subscribe", "unsubscribe", "upload", "verify", "vote", "accept", "approve", "authorize",
        "cancel", "connect", "create", "disconnect", "invite", "join", "leave", "register",
        "reset", "revoke", "save", "update",
        "결제", "구매", "로그인", "발송", "삭제", "전송", "주문", "구독", "업로드",
        "가입", "수정", "저장", "취소", "승인", "참여", "초대",
    }
)
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token", "api-key", "api_key", "apikey", "auth", "authorization", "code",
        "cookie", "credential", "jwt", "key", "oauth_token", "otp", "password", "passcode",
        "secret", "session", "sessionid", "sig", "signature", "token",
        "ticket",
        "x-amz-credential", "x-amz-signature", "x-goog-credential", "x-goog-signature",
    }
)


def is_risky_label(label: str) -> bool:
    lowered = " ".join(label.lower().split())
    return any(term in lowered for term in RISKY_ACTION_TERMS)


def is_sensitive_field(metadata: dict) -> bool:
    combined = " ".join(str(metadata.get(key, "")) for key in ("name", "accessibleName", "placeholder", "type")).lower()
    return any(term in combined for term in SENSITIVE_FIELD_TERMS)


def is_risky_url(raw_url: str) -> bool:
    try:
        parts = urlsplit(raw_url)
        decoded_path = _recursively_unquote(parts.path).lower()
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=50)
    except (UnicodeError, ValueError):
        return True
    path_tokens = set(re.findall(r"[a-z0-9가-힣]+", decoded_path))
    if path_tokens.intersection(RISKY_URL_TERMS):
        return True
    normalized_path = decoded_path.replace("_", "-")
    if "/add-to-cart" in normalized_path:
        return True
    action_keys = {"action", "command", "do", "intent", "operation", "op"}
    for raw_key, raw_value in query:
        key = _recursively_unquote(raw_key).lower()
        key_tokens = set(re.findall(r"[a-z0-9가-힣]+", key))
        value_tokens = set(re.findall(r"[a-z0-9가-힣]+", _recursively_unquote(raw_value).lower()))
        if key_tokens.intersection(RISKY_URL_TERMS) or (
            key in action_keys and value_tokens.intersection(RISKY_URL_TERMS)
        ):
            return True
    return False


def has_credential_query(raw_url: str) -> bool:
    try:
        parts = urlsplit(raw_url)
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=50)
    except (UnicodeError, ValueError):
        return True
    for raw_key, _ in query:
        key = _recursively_unquote(raw_key).strip().lower()
        normalized_key = key.replace("-", "_")
        if key in SENSITIVE_QUERY_KEYS or normalized_key in SENSITIVE_QUERY_KEYS:
            return True
        if normalized_key.endswith(("_token", "_secret", "_password", "_signature")):
            return True
    return False


def _recursively_unquote(value: str) -> str:
    """Decode a bounded number of layers so double encoding cannot hide policy terms."""
    current = value
    for _ in range(4):
        decoded = unquote_plus(current)
        if decoded == current:
            break
        current = decoded
    return current
