"""Bounded crawler for public websites explicitly supplied by the user."""

import asyncio
import hashlib
import html
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any


TOOL_META = {
    "name": "web_crawl",
    "description": (
        "Crawl public pages from a user-provided site URL. It honors robots.txt, "
        "stays on the same origin, limits rate/depth/page count, deduplicates URLs, "
        "and can store content hashes for incremental change detection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Public seed URL"},
            "max_pages": {"type": "integer", "default": 20, "description": "Maximum pages (1-50)"},
            "max_depth": {"type": "integer", "default": 2, "description": "Maximum link depth (0-4)"},
            "include_sitemaps": {"type": "boolean", "default": True},
            "crawl_delay": {"type": "number", "default": 1.0, "description": "Minimum seconds between requests"},
            "max_retries": {"type": "integer", "default": 2, "description": "Retries for transient request failures (0-3)"},
            "state_path": {"type": "string", "default": "", "description": "Optional JSON file for incremental state"},
            "render": {
                "type": "string",
                "enum": ["auto", "never", "always"],
                "default": "auto",
                "description": "Use an isolated browser for JavaScript pages only when needed, never, or always",
            },
            "output_format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
        },
        "required": ["url"],
    },
    "path_args": ["state_path"],
    "permission_level": "write",
    "network_access": True,
    "approval_policy": "policy",
    "side_effect": "write",
    "execution_environment": "host",
    "timeout_seconds": 180,
    "concurrency": "serial",
}

USER_AGENT = "AgentSmithCrawler/1.0"
MAX_PAGES = 50
MAX_DEPTH = 4
MAX_DOCUMENT_BYTES = 512 * 1024
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class _CrawlCancelled(Exception):
    """Internal signal that stops a cancelled synchronous crawl safely."""


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _CrawlCancelled()


@dataclass(frozen=True)
class _RobotRule:
    path: str
    allow: bool


@dataclass(frozen=True)
class _RobotsPolicy:
    rules: tuple[_RobotRule, ...]
    crawl_delay: float | None
    sitemaps: tuple[str, ...]


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())[:300]


def _load_web_fetch():
    path = Path(__file__).with_name("web_fetch.py")
    spec = importlib.util.spec_from_file_location("_crawler_web_fetch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("web_fetch provider is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_WEB_FETCH = None


def _web_fetch_module():
    global _WEB_FETCH
    if _WEB_FETCH is None:
        _WEB_FETCH = _load_web_fetch()
    return _WEB_FETCH


def _normalize_url(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in {None, 80, 443}:
        return None
    netloc = host
    if port and port != (443 if scheme == "https" else 80):
        netloc = f"{host}:{port}"
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept = [
        (key, value)
        for key, value in params
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    if path == "/" and not parsed.path:
        path = ""
    return urllib.parse.urlunsplit((scheme, netloc, path, urllib.parse.urlencode(kept), ""))


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def _parse_robots(text: str) -> _RobotsPolicy:
    groups: list[tuple[list[str], list[_RobotRule], float | None]] = []
    agents: list[str] = []
    rules: list[_RobotRule] = []
    delay: float | None = None
    sitemaps: list[str] = []

    def flush() -> None:
        nonlocal agents, rules, delay
        if agents:
            groups.append((agents, rules, delay))
        agents, rules, delay = [], [], None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                flush()
            agents.append(value.lower())
        elif key in {"allow", "disallow"} and agents:
            if value:
                rules.append(_RobotRule(value, key == "allow"))
        elif key == "crawl-delay" and agents:
            try:
                delay = max(0.0, float(value))
            except ValueError:
                pass
        elif key == "sitemap":
            normalized = _normalize_url(value)
            if normalized:
                sitemaps.append(normalized)
    flush()

    target = USER_AGENT.split("/", 1)[0].lower()
    selected = next((group for group in groups if target in group[0]), None)
    if selected is None:
        selected = next((group for group in groups if "*" in group[0]), ([], [], None))
    return _RobotsPolicy(tuple(selected[1]), selected[2], tuple(dict.fromkeys(sitemaps)))


def _robots_allows(policy: _RobotsPolicy, url: str) -> bool:
    path = urllib.parse.urlsplit(url).path or "/"
    matches = [rule for rule in policy.rules if path.startswith(rule.path)]
    if not matches:
        return True
    longest = max(len(rule.path) for rule in matches)
    return any(rule.allow for rule in matches if len(rule.path) == longest)


def _extract_links(base_url: str, source: str) -> list[str]:
    parser = _LinkParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return []
    base_origin = _origin(base_url)
    urls: list[str] = []
    seen: set[str] = set()
    for href in parser.links:
        target = _normalize_url(urllib.parse.urljoin(base_url, html.unescape(href)))
        if not target or _origin(target) != base_origin or target in seen:
            continue
        seen.add(target)
        urls.append(target)
    return urls


def _parse_xml_urls(source: str, names: set[str]) -> list[str]:
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return []
    found: list[str] = []
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1].lower()
        if name in names:
            candidate = _normalize_url((node.text or "").strip())
            if candidate:
                found.append(candidate)
        elif name == "link" and "href" in node.attrib:
            candidate = _normalize_url(node.attrib["href"])
            if candidate:
                found.append(candidate)
    return list(dict.fromkeys(found))


def _parse_sitemap(source: str) -> list[str]:
    return _parse_xml_urls(source, {"loc"})


def _parse_feed(source: str) -> list[str]:
    return _parse_xml_urls(source, {"link", "guid", "id"})


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_record(url: str, title: str, content: str, previous: dict[str, Any]) -> dict[str, Any]:
    digest = _content_hash(content)
    old = previous.get(url, {})
    return {
        "url": url,
        "title": title,
        "content_hash": digest,
        "changed": old.get("content_hash") != digest,
        "fetched_at": int(time.time()),
        "text": content[:40_000],
    }


def _download(
    url: str,
    timeout: float,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str, str, str]:
    """Fetch a text document through web_fetch's validated, pinned connection."""
    fetch = _web_fetch_module()
    current = url
    deadline = time.monotonic() + timeout
    for _ in range(6):
        _raise_if_cancelled(cancel_event)
        validation = fetch._validate_url(current)
        if validation:
            raise ValueError(validation)
        parsed = urllib.parse.urlparse(current)
        host = parsed.hostname
        assert host is not None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request timed out")
        connection = None
        try:
            infos = fetch._safe_addresses(host, port)
            connection, response = fetch._request_pinned(
                parsed, infos, remaining, user_agent=USER_AGENT
            )
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError(f"redirect {response.status} without Location")
                current = fetch._validated_redirect_url(current, location)
                continue
            content_type = (response.getheader("Content-Type") or "").lower()
            data = response.read(MAX_DOCUMENT_BYTES + 1)
            _raise_if_cancelled(cancel_event)
            if len(data) > MAX_DOCUMENT_BYTES:
                raise ValueError("response exceeds 512 KiB limit")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, current, content_type, data.decode(charset, errors="replace")
        finally:
            if connection is not None:
                connection.close()
    raise ValueError("too many redirects")


def _download_with_retries(
    url: str,
    timeout: float,
    *,
    retries: int,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str, str, str]:
    """Retry only transient transport failures with a small bounded backoff."""
    for attempt in range(retries + 1):
        _raise_if_cancelled(cancel_event)
        try:
            if cancel_event is None:
                result = _download(url, timeout)
            else:
                result = _download(url, timeout, cancel_event=cancel_event)
            _raise_if_cancelled(cancel_event)
            return result
        except _CrawlCancelled:
            raise
        except (OSError, TimeoutError):
            if attempt >= retries:
                raise
            delay = 0.25 * (attempt + 1)
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise _CrawlCancelled()
            else:
                time.sleep(delay)
    raise RuntimeError("unreachable")


async def _render_with_playwright(url: str, timeout: float = 30.0) -> str:
    """Render a public page in a short-lived, request-filtered browser context."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "browser rendering requires the optional 'playwright' dependency and Chromium"
        ) from exc

    fetch = _web_fetch_module()
    validation = fetch._validate_url(url)
    if validation:
        raise ValueError(validation)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-quic", "--no-first-run"],
        )
        context = await browser.new_context(
            accept_downloads=False,
            user_agent=USER_AGENT,
        )

        async def guard_request(route, request):
            request_validation = fetch._validate_url(request.url)
            if request_validation:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", guard_request)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                await page.wait_for_load_state("networkidle", timeout=min(5_000, int(timeout * 1000)))
            except Exception:
                pass
            # Bounded scrolling supports common lazy-loaded public pages without
            # turning a single fetch into an unbounded crawl.
            for _ in range(3):
                before = await page.evaluate("document.body.scrollHeight")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(350)
                after = await page.evaluate("document.body.scrollHeight")
                if after <= before:
                    break
            return await page.content()
        finally:
            await context.close()
            await browser.close()


def _read_state(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return value.get("records", {}) if isinstance(value, dict) else {}


def _write_state(
    path: str,
    records: dict[str, dict[str, Any]],
    *,
    cancel_event: threading.Event | None = None,
) -> None:
    if not path:
        return
    _raise_if_cancelled(cancel_event)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    persisted = {
        url: {key: value for key, value in record.items() if key != "text"}
        for url, record in records.items()
    }
    _raise_if_cancelled(cancel_event)
    destination.write_text(json.dumps({"version": 1, "records": persisted}, indent=2), encoding="utf-8")


def _crawl(
    url: str,
    max_pages: int,
    max_depth: int,
    include_sitemaps: bool,
    crawl_delay: float,
    max_retries: int,
    state_path: str,
    render: str,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    _raise_if_cancelled(cancel_event)
    seed = _normalize_url(url)
    if not seed:
        raise ValueError("url must be a public http(s) URL")
    fetch = _web_fetch_module()
    validation = fetch._validate_url(seed)
    if validation:
        raise ValueError(validation)
    origin = _origin(seed)
    root = f"{origin[0]}://{origin[1]}" + (f":{origin[2]}" if origin[2] not in {80, 443} else "")
    previous = _read_state(state_path)
    _raise_if_cancelled(cancel_event)
    warnings: list[str] = []
    try:
        robots_status, _, _, robots_text = _download_with_retries(
            f"{root}/robots.txt", 15, retries=max_retries, cancel_event=cancel_event
        )
        _raise_if_cancelled(cancel_event)
        policy = _parse_robots(robots_text) if robots_status == 200 else _RobotsPolicy((), None, ())
    except _CrawlCancelled:
        raise
    except Exception as exc:
        raise ValueError(f"could not retrieve robots.txt: {exc}") from exc
    effective_delay = max(crawl_delay, policy.crawl_delay or 0.0)
    queue: list[tuple[str, int]] = [(seed, 0)]
    queued = {seed}
    if include_sitemaps:
        sitemaps = list(policy.sitemaps) or [f"{root}/sitemap.xml"]
        for sitemap in sitemaps:
            _raise_if_cancelled(cancel_event)
            if _origin(sitemap) != origin:
                continue
            try:
                status, _, kind, body = _download_with_retries(
                    sitemap, 15, retries=max_retries, cancel_event=cancel_event
                )
                _raise_if_cancelled(cancel_event)
                if status == 200 and ("xml" in kind or body.lstrip().startswith("<")):
                    for item in _parse_sitemap(body):
                        if _origin(item) == origin and item not in queued:
                            queue.append((item, 0))
                            queued.add(item)
            except _CrawlCancelled:
                raise
            except Exception as exc:
                warnings.append(f"sitemap skipped: {sitemap} ({exc})")
    records: dict[str, dict[str, Any]] = {}
    skipped_robots = 0
    last_request = 0.0
    while queue and len(records) < max_pages:
        _raise_if_cancelled(cancel_event)
        current, depth = queue.pop(0)
        if not _robots_allows(policy, current):
            skipped_robots += 1
            continue
        pause = effective_delay - (time.monotonic() - last_request)
        if pause > 0:
            if cancel_event is not None:
                if cancel_event.wait(pause):
                    raise _CrawlCancelled()
            else:
                time.sleep(pause)
        try:
            status, final_url, content_type, body = _download_with_retries(
                current, 30, retries=max_retries, cancel_event=cancel_event
            )
            _raise_if_cancelled(cancel_event)
            last_request = time.monotonic()
        except _CrawlCancelled:
            raise
        except Exception as exc:
            warnings.append(f"fetch failed: {current} ({exc})")
            continue
        if not 200 <= status < 300:
            warnings.append(f"HTTP {status}: {final_url}")
            continue
        is_xml = "xml" in content_type or body.lstrip().startswith("<?xml")
        should_render = render == "always" or (
            render == "auto" and not is_xml and len(_web_fetch_module()._html_to_text(body)) < 400
        )
        if should_render:
            try:
                _raise_if_cancelled(cancel_event)
                body = asyncio.run(_render_with_playwright(final_url, timeout=30))
                _raise_if_cancelled(cancel_event)
                content_type = "text/html"
                is_xml = False
            except _CrawlCancelled:
                raise
            except Exception as exc:
                if render == "always":
                    raise ValueError(f"browser render failed for {final_url}: {exc}") from exc
                warnings.append(f"browser render skipped: {final_url} ({exc})")
        if is_xml:
            links = _parse_feed(body)
            title = ""
            content = body
        elif "html" in content_type or body.lstrip().startswith("<"):
            parser = _LinkParser()
            parser.feed(body)
            parser.close()
            title = parser.title
            content = _web_fetch_module()._html_to_text(body)
            links = _extract_links(final_url, body)
        else:
            warnings.append(f"unsupported content type: {content_type or 'unknown'} ({final_url})")
            continue
        normalized_final = _normalize_url(final_url) or current
        records[normalized_final] = _build_record(normalized_final, title, content, previous)
        if depth < max_depth:
            for link in links:
                if link not in queued and _origin(link) == origin:
                    queue.append((link, depth + 1))
                    queued.add(link)
    _write_state(state_path, records, cancel_event=cancel_event)
    return {
        "seed": seed,
        "records": list(records.values()),
        "changed": sum(record["changed"] for record in records.values()),
        "unchanged": sum(not record["changed"] for record in records.values()),
        "skipped_robots": skipped_robots,
        "warnings": warnings,
    }


def _render_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"[UNTRUSTED_EXTERNAL_CONTENT source=\"{result['seed']}\"]",
        f"# Crawl: {result['seed']}",
        f"pages: {len(result['records'])}; changed: {result['changed']}; unchanged: {result['unchanged']}; robots skipped: {result['skipped_robots']}",
    ]
    for record in result["records"]:
        state = "changed" if record["changed"] else "unchanged"
        lines.extend([f"\n## {record['title'] or record['url']}", f"source: {record['url']} ({state})", record["text"]])
    if result["warnings"]:
        lines.extend(["\n## Warnings", *[f"- {warning}" for warning in result["warnings"]]])
    lines.append("[/UNTRUSTED_EXTERNAL_CONTENT]")
    return "\n".join(lines)


def _consume_background_crawl_result(task: asyncio.Task[dict[str, Any]]) -> None:
    """Observe a cancelled worker's terminal exception without surfacing it later."""
    try:
        task.result()
    except (asyncio.CancelledError, _CrawlCancelled):
        pass
    except Exception:
        # The caller has already received cancellation; a late worker error is
        # not useful there and must not become an unhandled-task warning.
        pass


async def execute(
    *,
    url: str,
    max_pages: int = 20,
    max_depth: int = 2,
    include_sitemaps: bool = True,
    crawl_delay: float = 1.0,
    max_retries: int = 2,
    state_path: str = "",
    render: str = "auto",
    output_format: str = "markdown",
) -> str:
    if not isinstance(url, str):
        return "Error: url must be a string"
    if isinstance(max_pages, bool) or not isinstance(max_pages, int):
        return "Error: max_pages must be an integer"
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return "Error: max_depth must be an integer"
    if not isinstance(crawl_delay, (int, float)) or isinstance(crawl_delay, bool):
        return "Error: crawl_delay must be a number"
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        return "Error: max_retries must be an integer"
    if output_format not in {"markdown", "json"}:
        return "Error: output_format must be 'markdown' or 'json'"
    if render not in {"auto", "never", "always"}:
        return "Error: render must be 'auto', 'never', or 'always'"
    max_pages = min(max(1, max_pages), MAX_PAGES)
    max_depth = min(max(0, max_depth), MAX_DEPTH)
    cancel_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _crawl,
            url,
            max_pages,
            max_depth,
            include_sitemaps,
            max(0.0, float(crawl_delay)),
            min(max(0, max_retries), 3),
            state_path,
            render,
            cancel_event=cancel_event,
        )
    )
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        # ``asyncio.to_thread`` cannot stop a running thread by itself.  Signal
        # cooperative cancellation, then wait for the worker to settle before
        # reporting this run as cancelled.  This keeps the engine from
        # returning a timeout while the crawler can still issue requests or
        # write state; any in-flight I/O remains bounded by its own timeout.
        cancel_event.set()
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A second cancellation (for example process shutdown) cannot
            # safely wait for a thread.  Preserve the cleanup observer in that
            # exceptional path so no late exception is lost.
            worker.add_done_callback(_consume_background_crawl_result)
            raise
        except _CrawlCancelled:
            pass
        except Exception:
            # Cancellation is the result visible to the caller; a worker
            # failure after its cancellation signal is only diagnostic.
            pass
        raise
    except Exception as exc:
        return f"Error: crawl failed: {exc}"
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False)
    return _render_markdown(result)
