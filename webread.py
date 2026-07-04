# server.py
"""WebRead MCP server for Xiaozhi.

Provides a single tool ``web_read`` that fetches a URL and returns its text
content in either "reader" (article-extracted, Markdown) or "raw" (HTML) mode,
with optional line-based pagination. Mirrors the essentials of a coding-agent
``read`` tool applied to web URLs.

Proxy is honored via the standard ``http_proxy`` / ``https_proxy`` env vars,
which the xiaozhi.service unit already exports.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Optional
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("WebRead")

# Fix UTF-8 encoding for Windows console (kept for parity with sibling servers)
if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

mcp = FastMCP("WebRead")

DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap on downloaded body
DEFAULT_LIMIT_LINES = 400
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 XiaozhiWebRead/1.0"
)


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("url is empty")
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    if not parsed.netloc:
        raise ValueError("url is missing host")
    return url


def _fetch(url: str, timeout: float) -> httpx.Response:
    # trust_env=True picks up http_proxy / https_proxy from the environment.
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        trust_env=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    ) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError(f"response exceeds {MAX_BYTES} bytes")
                chunks.append(chunk)
            resp._content = b"".join(chunks)  # type: ignore[attr-defined]
        return resp


def _decode(resp: httpx.Response) -> str:
    # Trust httpx's charset detection first; fall back to utf-8 with replacement.
    try:
        return resp.text
    except Exception:
        return resp.content.decode("utf-8", errors="replace")


def _extract_title(html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None


def _to_reader_markdown(html: str, url: str) -> str:
    """Extract the readable article portion of ``html`` and return Markdown."""
    # 1) Try trafilatura — best-in-class boilerplate stripper.
    try:
        import trafilatura

        md = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_images=False,
            include_tables=True,
            favor_recall=True,
        )
        if md and md.strip():
            return md.strip()
    except Exception as exc:  # pragma: no cover - fallback path
        logger.warning(f"trafilatura extract failed: {exc}")

    # 2) Fallback: bs4 → strip script/style/nav → markdownify.
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify as md_convert

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
            tag.decompose()
        body = soup.body or soup
        md = md_convert(str(body), heading_style="ATX", strip=["a"]).strip()
        # Collapse >2 blank lines.
        md = re.sub(r"\n{3,}", "\n\n", md)
        return md
    except Exception as exc:
        logger.warning(f"bs4 fallback failed: {exc}")
        return re.sub(r"<[^>]+>", "", html)


def _paginate(text: str, start: int, limit: int) -> tuple[str, int, int, bool]:
    """Return (slice, start_line, end_line, truncated) using 1-indexed lines."""
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return "", 1, 0, False
    start = max(1, start)
    if start > total:
        return "", start, start - 1, False
    end = min(total, start + max(1, limit) - 1)
    truncated = end < total
    return "\n".join(lines[start - 1 : end]), start, end, truncated


@mcp.tool()
def web_read(
    url: str,
    mode: str = "reader",
    start: int = 1,
    limit: int = DEFAULT_LIMIT_LINES,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Fetch a URL and return its text content.

    Parameters
    ----------
    url:
        Absolute URL. Bare hosts (``example.com``) are treated as ``http://``.
    mode:
        ``"reader"`` (default) extracts the readable article as Markdown.
        ``"raw"`` returns the decoded HTML/text response body verbatim.
    start:
        1-indexed starting line for pagination. Use with ``limit`` to page
        through long pages.
    limit:
        Maximum number of lines to return in this call (default 400).
    timeout:
        Per-request timeout in seconds (default 20).

    Returns a dict with success, url, final_url, title, mime, status,
    total_lines, start_line, end_line, truncated, content.
    """
    try:
        url_norm = _normalize_url(url)
    except Exception as exc:
        return {"success": False, "error": f"invalid url: {exc}"}

    mode_norm = (mode or "reader").lower()
    if mode_norm not in ("reader", "raw"):
        return {"success": False, "error": f"invalid mode: {mode}"}

    try:
        resp = _fetch(url_norm, timeout=timeout)
    except httpx.HTTPStatusError as exc:
        return {
            "success": False,
            "url": url_norm,
            "status": exc.response.status_code,
            "error": f"http {exc.response.status_code}",
        }
    except Exception as exc:
        return {"success": False, "url": url_norm, "error": f"fetch failed: {exc}"}

    text = _decode(resp)
    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    title = _extract_title(text) if "html" in mime or text.lstrip().startswith("<") else None

    if mode_norm == "reader":
        content_full = _to_reader_markdown(text, url_norm)
    else:
        content_full = text

    body, start_line, end_line, truncated = _paginate(content_full, start, limit)
    total_lines = len(content_full.splitlines())

    logger.info(
        f"web_read url={url_norm} mode={mode_norm} status={resp.status_code} "
        f"lines={start_line}-{end_line}/{total_lines} truncated={truncated}"
    )

    return {
        "success": True,
        "url": url_norm,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "mime": mime or None,
        "title": title,
        "mode": mode_norm,
        "total_lines": total_lines,
        "start_line": start_line,
        "end_line": end_line,
        "truncated": truncated,
        "content": body,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
