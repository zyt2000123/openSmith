"""Web fetch tool provider — validated, DNS-pinned HTTP text fetches."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import re
import socket
import time
import urllib.parse
import ssl
from html.parser import HTMLParser
from typing import Any

TOOL_META = {
    "name": "web_fetch",
    "description": (
        "Fetch a URL and return its main text content. Redirect targets and "
        "resolved addresses are validated before every network request. "
        "Use web_search first to discover URLs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch"
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds (default 30, max 60)",
                "default": 30
            }
        },
        "required": ["url"]
    },
    "permission_level": "read",
    "network_access": True,
    "approval_policy": "never",
    "side_effect": "none",
    "execution_environment": "host",
}

MAX_RESPONSE_BYTES = 512 * 1024
MAX_OUTPUT_CHARS = 40_000
MAX_TIMEOUT = 60
BLOCKED_SCHEMES = {"file", "ftp", "data"}
BLOCKED_HOSTS = {"localhost"}
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
HTML_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
HTML_SKIP_TAGS = {"script", "style", "svg", "noscript", "template"}
ALLOWED_PORTS = {80, 443}
ALLOWED_CONTENT_TYPES = (
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/ld+json",
    "text/",
)
_FETCH_CONCURRENCY = asyncio.Semaphore(2)


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in HTML_SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in HTML_SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _blocked_ip_reason(host: str) -> str | None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None

    if ip.is_loopback:
        return "loopback address"
    if ip.is_private:
        return "private network address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_unspecified:
        return "unspecified address"
    if not ip.is_global:
        return "non-public address"
    return None


def _validate_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in BLOCKED_SCHEMES:
        return f"scheme '{scheme}' is not allowed. Only http/https supported."
    if scheme not in {"http", "https"}:
        return "URL must start with http:// or https://"
    if not parsed.hostname:
        return "URL must include a hostname"
    if parsed.username is not None or parsed.password is not None:
        return "URLs containing credentials are not allowed"

    host = parsed.hostname.rstrip(".").lower()
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        return "localhost targets are not allowed"

    reason = _blocked_ip_reason(host)
    if reason:
        return f"host resolves to a blocked {reason}"

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        return str(exc)
    if port not in ALLOWED_PORTS:
        return "only destination ports 80 and 443 are allowed"

    try:
        _safe_addresses(host, port)
    except (OSError, ValueError) as exc:
        return str(exc)

    return None


def _safe_addresses(host: str, port: int) -> list[tuple[Any, Any, int, str, tuple[Any, ...]]]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host '{host}': {exc}") from exc
    if not infos:
        raise ValueError(f"could not resolve host '{host}'")

    for info in infos:
        reason = _blocked_ip_reason(str(info[4][0]))
        if reason:
            raise ValueError(f"host resolves to a blocked {reason}: {info[4][0]}")
    return infos


def _validated_redirect_url(current_url: str, location: str) -> str:
    target = urllib.parse.urljoin(current_url, location)
    validation_error = _validate_url(target)
    if validation_error:
        raise ValueError(validation_error)
    return target


def _html_to_text(raw: str) -> str:
    parser = _PlainTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return ""

    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


async def execute(*, url: str, timeout: int = 30) -> str:
    if not isinstance(url, str):
        return "Error: url must be a string"
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return "Error: timeout must be an integer"
    timeout = min(max(1, timeout), MAX_TIMEOUT)

    validation_error = _validate_url(url)
    if validation_error:
        return f"Error: {validation_error}"

    async with _FETCH_CONCURRENCY:
        try:
            return await asyncio.wait_for(_fetch_plain(url, timeout), timeout=timeout)
        except asyncio.TimeoutError:
            return "URL Error: request timed out"


async def _fetch_plain(url: str, timeout: int) -> str:
    """Fetch text without following unvalidated redirects or re-resolving DNS."""
    return await asyncio.to_thread(_fetch_pinned, url, timeout)


def _fetch_pinned(url: str, timeout: int) -> str:
    current_url = url
    deadline = time.monotonic() + timeout
    for _ in range(6):
        parsed = urllib.parse.urlparse(current_url)
        host = parsed.hostname
        if host is None:
            return "URL Error: URL must include a hostname"
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            return f"URL Error: {exc}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "URL Error: request timed out"
        try:
            infos = _safe_addresses(host, port)
            connection, response = _request_pinned(parsed, infos, remaining)
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
            return f"URL Error: {exc}"

        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    return f"HTTP Error: {response.status} redirect without Location"
                try:
                    current_url = _validated_redirect_url(current_url, location)
                except ValueError as exc:
                    return f"URL Error: redirect blocked: {exc}"
                continue

            if not 200 <= response.status < 300:
                return f"HTTP Error: {response.status} for {current_url}"

            content_type = (response.getheader("Content-Type") or "").lower()
            if content_type and not any(kind in content_type for kind in ALLOWED_CONTENT_TYPES):
                return f"HTTP Error: unsupported content type '{content_type}'"

            data = response.read(MAX_RESPONSE_BYTES + 1)
            truncated = len(data) > MAX_RESPONSE_BYTES
            charset = response.headers.get_content_charset() or "utf-8"
            raw = data[:MAX_RESPONSE_BYTES].decode(charset, errors="replace")
            is_html = any(kind in content_type for kind in HTML_CONTENT_TYPES) or raw.lstrip().startswith("<")
            body = _html_to_text(raw) if is_html else raw.strip()
            if not body:
                body = raw.strip()
            output_truncated = len(body) > MAX_OUTPUT_CHARS
            body = body[:MAX_OUTPUT_CHARS]
            body_type = "text extracted from html" if is_html else "text"
            source = current_url.replace('"', "%22")
            return (
                f"[UNTRUSTED_EXTERNAL_CONTENT source=\"{source}\"]\n"
                f"[status={response.status}, pinned HTTP, {body_type}"
                f"{', truncated' if truncated or output_truncated else ''}]\n{body}\n"
                "[/UNTRUSTED_EXTERNAL_CONTENT]"
            )
        finally:
            connection.close()

    return "URL Error: too many redirects"


def _request_pinned(
    parsed: urllib.parse.ParseResult,
    infos: list[tuple[Any, Any, int, str, tuple[Any, ...]]],
    timeout: float,
    user_agent: str = "AgentSmith/1.0",
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    host = parsed.hostname
    assert host is not None
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    host_header = host if port in {80, 443} else f"{host}:{port}"
    last_error: OSError | ssl.SSLError | None = None

    for family, socktype, proto, _, sockaddr in infos:
        raw_socket: socket.socket | None = None
        connection = http.client.HTTPConnection(host, port=port, timeout=timeout)
        try:
            raw_socket = socket.socket(family, socktype, proto)
            raw_socket.settimeout(timeout)
            raw_socket.connect(sockaddr)
            connection.sock = raw_socket
            if parsed.scheme.lower() == "https":
                connection.sock = ssl.create_default_context().wrap_socket(
                    raw_socket,
                    server_hostname=host,
                )
            else:
                connection.sock = raw_socket
            connection.request(
                "GET",
                target,
                headers={"Host": host_header, "User-Agent": user_agent},
            )
            return connection, connection.getresponse()
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
            connection.close()
    raise last_error or OSError("unable to connect to resolved host")
