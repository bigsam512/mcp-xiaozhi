"""Tavily search MCP server for Xiaozhi.

Provides ``tavily_search`` — a wrapper over the Tavily Search API that returns
ranked web results with URLs and snippets. Behavior mirrors the OMP agent
extension at ``~/.omp/agent/extensions/tavily/index.ts``:

- Reads config from environment first, then falls back to ``.env`` files in:
    1. ``./tavily.env`` (project-local override, if you want one)
    2. ``./.env`` (repo-wide)
    3. ``~/.omp/agent/extensions/tavily/.env`` (shared with the OMP extension)
- Tries a direct request first; on failure, retries via each proxy listed in
  ``TAVILY_PROXIES`` (comma/semicolon/whitespace separated) until one succeeds.
- ``TAVILY_ENDPOINT`` overrides the default ``https://api.tavily.com/search``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("Tavily")

if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")


DEFAULT_ENDPOINT = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 30.0
_WANTED_KEYS = ("TAVILY_API_KEY", "TAVILY_PROXIES", "TAVILY_ENDPOINT")
_DOTENV_CANDIDATES = (
    Path.cwd() / "tavily.env",
    Path.cwd() / ".env",
    Path.home() / ".omp" / "agent" / "extensions" / "tavily" / ".env",
)


def _load_dotenv_fallback() -> None:
    """Fill missing env vars from the first available .env file. Existing env wins."""
    for path in _DOTENV_CANDIDATES:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key not in _WANTED_KEYS or os.environ.get(key):
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
        except Exception as exc:
            logger.warning(f"failed to read {path}: {exc}")


_load_dotenv_fallback()


def _get_api_key() -> str:
    key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "TAVILY_API_KEY is not set. Put it in ./tavily.env, ./.env, or "
            "~/.omp/agent/extensions/tavily/.env"
        )
    return key


def _get_endpoint() -> str:
    return (os.environ.get("TAVILY_ENDPOINT") or DEFAULT_ENDPOINT).strip()


def _get_proxies() -> list[str]:
    raw = os.environ.get("TAVILY_PROXIES") or ""
    return [p.strip() for p in re.split(r"[,\s;]+", raw) if p.strip()]


mcp = FastMCP("Tavily")


def _post(endpoint: str, body: dict[str, Any], proxy: Optional[str]) -> tuple[int, str, str]:
    """POST JSON to endpoint. Returns (status, text, via_label)."""
    label = proxy or "direct"
    kwargs: dict[str, Any] = {
        "timeout": REQUEST_TIMEOUT,
        "trust_env": False,  # explicit per-attempt proxy choice
        "headers": {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "XiaozhiTavily/1.0",
        },
    }
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        resp = client.post(endpoint, content=json.dumps(body).encode("utf-8"))
        return resp.status_code, resp.text, label


def _try_endpoints(endpoint: str, body: dict[str, Any]) -> tuple[int, str, str, list[str]]:
    """Try direct then each configured proxy. Return first 2xx, else last non-2xx."""
    attempts: list[str] = []
    candidates: list[Optional[str]] = [None, *_get_proxies()]
    last: Optional[tuple[int, str, str]] = None
    for proxy in candidates:
        try:
            status, text, via = _post(endpoint, body, proxy)
        except Exception as exc:
            attempts.append(f"{proxy or 'direct'}: {exc}")
            continue
        attempts.append(f"{via}: HTTP {status}")
        if 200 <= status < 300:
            return status, text, via, attempts
        last = (status, text, via)
    if last is None:
        return 0, "; ".join(attempts) or "no attempts made", "none", attempts
    return (*last, attempts)


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {"error": text[:2000]}


RAW_CONTENT_CAP = 4000


@mcp.tool()
def tavily_search(
    query: str,
    search_depth: str = "basic",
    max_results: int = 5,
    include_answer: bool = False,
    include_raw_content: bool = False,
    include_images: bool = False,
    include_domains: Optional[list[str]] = None,
    exclude_domains: Optional[list[str]] = None,
) -> dict:
    """Search the web with Tavily and return ranked results.

    Parameters
    ----------
    query:
        Search query string.
    search_depth:
        ``"basic"`` (default, fast) or ``"advanced"`` (deeper research).
    max_results:
        Number of results to return, 1..20 (default 5).
    include_answer:
        Include Tavily's generated answer summary (default False).
    include_raw_content:
        Include raw article text for each result when available; capped per
        result to keep the payload bounded (default False).
    include_images:
        Include image URLs discovered on result pages (default False).
    include_domains / exclude_domains:
        Optional allow/deny lists of domains, e.g. ``["wikipedia.org"]``.
    """
    if not query or not query.strip():
        return {"success": False, "error": "query is empty"}
    depth = (search_depth or "basic").lower()
    if depth not in ("basic", "advanced"):
        return {"success": False, "error": f"invalid search_depth: {search_depth}"}
    n = max(1, min(20, int(max_results or 5)))

    try:
        api_key = _get_api_key()
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    body: dict[str, Any] = {
        "api_key": api_key,
        "query": query.strip(),
        "search_depth": depth,
        "max_results": n,
        "include_answer": bool(include_answer),
        "include_raw_content": bool(include_raw_content),
        "include_images": bool(include_images),
        "include_domains": list(include_domains or []),
        "exclude_domains": list(exclude_domains or []),
    }

    endpoint = _get_endpoint()
    status, text, via, attempts = _try_endpoints(endpoint, body)

    if not (200 <= status < 300):
        payload = _parse(text)
        logger.warning(
            f"tavily_search failed status={status} via={via} attempts={attempts}"
        )
        return {
            "success": False,
            "status": status,
            "via": via,
            "attempts": attempts,
            "error": payload,
        }

    data = _parse(text)
    results: list[dict[str, Any]] = []
    for r in data.get("results", []) or []:
        item: dict[str, Any] = {
            "title": r.get("title") or "Untitled",
            "url": r.get("url"),
            "score": r.get("score"),
            "content": r.get("content"),
        }
        if include_raw_content and r.get("raw_content"):
            item["raw_content"] = str(r["raw_content"])[:RAW_CONTENT_CAP]
        results.append(item)

    out: dict[str, Any] = {
        "success": True,
        "query": data.get("query") or query,
        "via": via,
        "results": results,
    }
    if include_answer and data.get("answer"):
        out["answer"] = data["answer"]
    if include_images and data.get("images"):
        out["images"] = data["images"]
    logger.info(
        f"tavily_search q={query!r} depth={depth} n={n} via={via} results={len(results)}"
    )
    return out


if __name__ == "__main__":
    mcp.run(transport="stdio")
