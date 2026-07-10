# server.py
"""StudyEnglish MCP server for Xiaozhi（读新闻学英语）。

把 pi 的 study-english skill 封装成小智可调用的 MCP 工具。设计分工：

- 本 MCP 只做“确定性数据操作”：抓 newsforkids.net 首页头条、抓文章并逐句拆分、
  一次给一句、读写学习进度文件。**不做**判断翻译对错、答疑等需要大模型的事。
- “逐句教学纪律”（先给原文、给难词提示、等孩子翻译、判分、答疑不推进、
  对了说“过”、每句存进度）写在**小智后台的角色提示词**里，由小智大模型执行。

之所以把“拆句子”放在 MCP 里，是为了保证每次给小智的英文原句都来自同一份
拆好的数组，满足 skill 的铁律：英文原句必须逐字（verbatim）来自原文。

进度文件默认 study-english-progress.md，可用环境变量 STUDY_ENGLISH_FILE
覆盖，以便多个孩子各用一份（多用户隔离）。

Proxy 通过标准 http_proxy / https_proxy 环境变量生效（xiaozhi.service 已导出）。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("StudyEnglish")

# Fix UTF-8 encoding for Windows console (parity with sibling servers)
if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

mcp = FastMCP("StudyEnglish")

HOME_URL = "https://newsforkids.net"
PROGRESS_FILE = os.environ.get("STUDY_ENGLISH_FILE", "study-english-progress.md")

DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 XiaozhiStudyEnglish/1.0"
)

# 常驻进程内缓存：url -> {"title": str, "sentences": [str, ...]}
_ARTICLE_CACHE: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# 网络抓取
# --------------------------------------------------------------------------- #
def _fetch(url: str, timeout: float) -> httpx.Response:
    # trust_env=True picks up http_proxy / https_proxy from the environment.
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        trust_env=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
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
    try:
        return resp.text
    except Exception:
        return resp.content.decode("utf-8", errors="replace")


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("url is empty")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    if not parsed.netloc:
        raise ValueError("url is missing host")
    return url


# --------------------------------------------------------------------------- #
# 首页头条解析
# --------------------------------------------------------------------------- #
def _extract_headlines(html: str, base_url: str) -> list[dict]:
    """从 newsforkids.net 首页 HTML 里抽取头条（标题 + 绝对 URL）。

    newsforkids.net 的文章链接形如 /YYYY/MM/DD/slug/。按此模式匹配，
    去重并保持出现顺序。
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # pragma: no cover
        logger.warning(f"bs4 parse failed: {exc}")
        return []

    seen: set[str] = set()
    headlines: list[dict] = []
    article_re = re.compile(r"/\d{4}/\d{2}/\d{2}/")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if not text or len(text) < 12:
            continue
        abs_url = urljoin(base_url, href)
        path = urlparse(abs_url).path
        if not article_re.search(path):
            continue
        # 去掉锚点差异后的规范 URL 去重
        norm = abs_url.split("#")[0].rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        headlines.append({"title": text, "url": abs_url})

    return headlines


# --------------------------------------------------------------------------- ## 文章正文提取与逐句拆分
# --------------------------------------------------------------------------- #
def _extract_article(html: str, url: str) -> tuple[Optional[str], str]:
    """返回 (标题, 正文纯文本)。优先用 trafilatura，回退到 bs4。"""
    title: Optional[str] = None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip() or None
        # newsforkids 标题常带站点后缀，去掉第一个分隔符后的部分
        if title:
            title = re.split(r"\s*[|\-–—]\s*", title)[0].strip() or title

    # 1) trafilatura —— 只要正文，输出纯文本
    try:
        import trafilatura

        txt = trafilatura.extract(
            html,
            url=url,
            output_format="txt",
            include_links=False,
            include_images=False,
            include_tables=False,
            favor_recall=True,
        )
        if txt and txt.strip():
            return title, txt.strip()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"trafilatura extract failed: {exc}")

    # 2) 回退：bs4 取 <p> 段落
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(
            ["script", "style", "noscript", "template", "svg", "iframe", "nav", "header", "footer"]
        ):
            tag.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        body = "\n".join(p for p in paras if p)
        return title, body.strip()
    except Exception as exc:
        logger.warning(f"bs4 fallback failed: {exc}")
        return title, re.sub(r"<[^>]+>", "", html).strip()


# 需要跳过的噪声行（图片说明、cookie 提示、来源链接等）
_NOISE_PATTERNS = [
    re.compile(r"this image has not been loaded", re.IGNORECASE),
    re.compile(r"^\s*(photo|image|picture|illustration|credit|source|sources)\s*[:：]", re.IGNORECASE),
    re.compile(r"^\s*\(?\s*(reuters|ap|afp|getty)\b", re.IGNORECASE),
    re.compile(r"cookie", re.IGNORECASE),
    re.compile(r"^\s*(read more|related|share this|advertisement)\b", re.IGNORECASE),
]


def _is_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith("http://") or s.startswith("https://"):
        return True
    for pat in _NOISE_PATTERNS:
        if pat.search(s):
            return True
    return False


# 句子拆分：在 . ! ? 及其后续引号/括号后断句，但避开常见缩写。
_ABBREV = {
    "mr", "mrs", "ms", "dr", "st", "mt", "jr", "sr", "vs", "etc", "no",
    "u.s", "u.k", "u.n", "a.m", "p.m", "e.g", "i.e",
}
_SENT_END_RE = re.compile(r'([.!?]+["”’)\]]*)\s+')


def _split_sentences(text: str) -> list[str]:
    """把正文拆成句子列表，跳过噪声行，尽量避免在缩写处误断。"""
    good_lines = [ln.strip() for ln in text.splitlines() if not _is_noise(ln)]
    joined = " ".join(good_lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    if not joined:
        return []

    sentences: list[str] = []
    start = 0
    for m in _SENT_END_RE.finditer(joined):
        end = m.end()
        # 检查断点前的“词”是否是缩写，是则不在此断
        prefix = joined[: m.start() + 1]
        last_word = re.split(r"[\s(]", prefix.strip())[-1].rstrip(".!?\"”’)]").lower()
        if last_word in _ABBREV:
            continue
        candidate = joined[start:end].strip()
        if candidate:
            sentences.append(candidate)
            start = end
    tail = joined[start:].strip()
    if tail:
        sentences.append(tail)

    return [s for s in sentences if len(s) >= 2]


def _load_article(url: str, timeout: float = DEFAULT_TIMEOUT, refresh: bool = False) -> dict:
    """抓文章、拆句并写入进程缓存，返回缓存条目 {title, sentences}。"""
    if not refresh and url in _ARTICLE_CACHE:
        return _ARTICLE_CACHE[url]
    resp = _fetch(url, timeout=timeout)
    html = _decode(resp)
    title, body = _extract_article(html, url)
    sentences = _split_sentences(body)
    entry = {"title": title or url, "sentences": sentences}
    _ARTICLE_CACHE[url] = entry
    return entry


# --------------------------------------------------------------------------- #
# 进度文件读写（进度文件是唯一状态源：当前活跃文章 + 每篇的游标）
# --------------------------------------------------------------------------- #
# 进度文件结构：
#
#   # 读新闻学英语 · 学习进度
#
#   - current: <当前活跃文章 url>        # 全局：正在学哪篇
#
#   ## <文章标题>
#   - url: ...
#   - status: in-progress | finished
#   - cursor: <当前停在第几句，0 起始>
#   - last_sentence_text: "<该句原文，用于重启后校验定位>"
#   - updated: <YYYY-MM-DD>
def _read_progress_raw() -> str:
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _write_progress_raw(content: str) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def _parse_progress(content: str) -> tuple[str, list[dict]]:
    """解析进度文件，返回 (current_url, [文章条目...])。"""
    current = ""
    entries: list[dict] = []
    cur: Optional[dict] = None
    for line in content.splitlines():
        cm = re.match(r"^\s*-\s*current\s*[:：]\s*(.*)$", line)
        if cm and cur is None:
            current = cm.group(1).strip()
            continue
        h = re.match(r"^##\s+(.*)$", line)
        if h:
            if cur is not None:
                entries.append(cur)
            cur = {"title": h.group(1).strip()}
            continue
        if cur is None:
            continue
        fm = re.match(r"^\s*-\s*([^:：]+)[:：]\s*(.*)$", line)
        if fm:
            cur[fm.group(1).strip()] = fm.group(2).strip()
    if cur is not None:
        entries.append(cur)
    return current, entries


def _dump_progress(current: str, entries: list[dict]) -> None:
    lines = ["# 读新闻学英语 · 学习进度", ""]
    if current:
        lines.append(f"- current: {current}")
        lines.append("")
    for e in entries:
        lines.append(f"## {e.get('title', '(未命名)')}")
        for key in ("url", "status", "cursor", "last_sentence_text", "updated"):
            if key in e and str(e[key]) != "":
                lines.append(f"- {key}: {e[key]}")
        lines.append("")
    _write_progress_raw("\n".join(lines).rstrip("\n") + "\n")


def _find_entry(entries: list[dict], url: str) -> Optional[int]:
    u = url.rstrip("/")
    for i, e in enumerate(entries):
        if e.get("url", "").rstrip("/") == u:
            return i
    return None


def _entry_cursor(entry: dict) -> int:
    try:
        return max(0, int(entry.get("cursor", 0)))
    except (ValueError, TypeError):
        return 0


def _resolve_cursor(entry: dict, sentences: list[str]) -> int:
    """用 last_sentence_text 校验游标；重启/拆句差异时重新定位。"""
    cursor = _entry_cursor(entry)
    saved = entry.get("last_sentence_text", "").strip().strip('"')
    if not saved:
        return min(cursor, max(0, len(sentences) - 1))
    # 若当前游标处文本与存档一致，直接用
    if 0 <= cursor < len(sentences) and sentences[cursor].strip() == saved:
        return cursor
    # 否则按存档文本重新定位
    for i, s in enumerate(sentences):
        if s.strip() == saved:
            return i
    # 找不到（文章更新了）就退回原游标（截断到范围内）
    return min(cursor, max(0, len(sentences) - 1))


# --------------------------------------------------------------------------- #
# 内部：把“当前活跃文章 + 游标”落到进度文件并生成返回句
# --------------------------------------------------------------------------- #
def _save_cursor(url: str, title: str, cursor: int, sentence_text: str, finished: bool) -> None:
    content = _read_progress_raw()
    current, entries = _parse_progress(content)
    idx = _find_entry(entries, url)
    entry = {
        "title": title,
        "url": url,
        "status": "finished" if finished else "in-progress",
        "cursor": str(cursor),
        "last_sentence_text": f'"{sentence_text}"' if sentence_text else "",
        "updated": date.today().isoformat(),
    }
    if idx is None:
        entries.append(entry)
    else:
        entries[idx] = entry
    _dump_progress(url, entries)  # 设为当前活跃文章


def _sentence_payload(url: str, entry: dict, cursor: int) -> dict:
    sentences = entry["sentences"]
    total = len(sentences)
    is_last = cursor >= total - 1
    return {
        "success": True,
        "url": url,
        "title": entry["title"],
        "index": cursor,
        "total_sentences": total,
        "sentence": sentences[cursor],
        "is_last": is_last,
        "progress": f"{cursor + 1}/{total}",
    }


# --------------------------------------------------------------------------- #
# MCP 工具
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_headlines(timeout: float = DEFAULT_TIMEOUT) -> dict:
    """列出 newsforkids.net 首页当前的新闻头条，供孩子挑选一篇来学。

    返回带编号的标题列表和对应文章 URL。小智应把标题读给孩子听
    （可附一句中文大意），让孩子选一篇，再用 start_article 打开。
    """
    try:
        resp = _fetch(HOME_URL, timeout=timeout)
    except Exception as exc:
        return {"success": False, "error": f"抓取首页失败：{exc}"}

    html = _decode(resp)
    headlines = _extract_headlines(html, HOME_URL)
    if not headlines:
        return {"success": False, "error": "没有解析到头条，网站结构可能变了。"}

    logger.info(f"list_headlines: {len(headlines)} items")
    return {
        "success": True,
        "count": len(headlines),
        "headlines": [
            {"index": i, "title": h["title"], "url": h["url"]}
            for i, h in enumerate(headlines)
        ],
    }


@mcp.tool()
def start_article(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """选定一篇文章开始学习，设为“当前活跃文章”，返回要学的那一句。

    :param url: 文章 URL（来自 list_headlines）。

    行为：抓取正文并逐句拆分（缓存到内存），把这篇设为当前活跃文章。
    - 若进度文件里这篇之前学过且未完成，则从上次停下的那一句继续；
    - 否则从第一句（第 0 句）开始。
    之后孩子翻译完就调 next_sentence()，提问后复述就调 current_sentence()，
    全程无需再传 url 或句子序号——状态由本 MCP 维护。
    """
    try:
        url = _normalize_url(url)
    except Exception as exc:
        return {"success": False, "error": f"无效 url：{exc}"}
    try:
        entry = _load_article(url, timeout=timeout, refresh=True)
    except Exception as exc:
        return {"success": False, "error": f"抓取文章失败：{exc}"}

    sentences = entry["sentences"]
    if not sentences:
        return {"success": False, "error": "没有解析到正文句子，可能是页面结构问题。"}

    # 若之前学过这篇且未完成，从存档游标续读；否则从 0 开始
    _current, entries = _parse_progress(_read_progress_raw())
    idx = _find_entry(entries, url)
    if idx is not None and entries[idx].get("status") != "finished":
        cursor = _resolve_cursor(entries[idx], sentences)
        resumed = cursor > 0
    else:
        cursor = 0
        resumed = False

    _save_cursor(url, entry["title"], cursor, sentences[cursor], finished=len(sentences) == 1)
    logger.info(f"start_article: {url} title={entry['title']!r} cursor={cursor} resumed={resumed}")

    payload = _sentence_payload(url, entry, cursor)
    payload["resumed"] = resumed
    return payload


def _active_article() -> tuple[Optional[str], list[dict], Optional[int]]:
    """读进度文件，返回 (current_url, entries, current 在 entries 里的下标)。"""
    _current, entries = _parse_progress(_read_progress_raw())
    if not _current:
        return None, entries, None
    return _current, entries, _find_entry(entries, _current)


@mcp.tool()
def current_sentence(timeout: float = DEFAULT_TIMEOUT) -> dict:
    """返回当前正在学的那一句，**不推进**。

    用于：孩子没有翻译、而是提了一个问题（问单词、语法等）。小智答疑后
    调用本工具，把同一句再念一遍，请孩子翻译。绝不因为答疑而跳到下一句。
    """
    url, entries, idx = _active_article()
    if not url or idx is None:
        return {"success": False, "error": "还没有正在学的文章。请先用 list_headlines 选一篇，再用 start_article 开始。"}
    try:
        entry = _load_article(url, timeout=timeout)
    except Exception as exc:
        return {"success": False, "error": f"抓取文章失败：{exc}"}
    sentences = entry["sentences"]
    if not sentences:
        return {"success": False, "error": "文章正文为空。"}
    cursor = _resolve_cursor(entries[idx], sentences)
    return _sentence_payload(url, entry, cursor)


@mcp.tool()
def next_sentence(timeout: float = DEFAULT_TIMEOUT) -> dict:
    """推进到当前文章的下一句，并保存进度。

    用于：孩子已经给出了当前句的翻译（无论对错，小智判分/讲解之后）。
    本工具把游标 +1、写进度文件、返回新的一句。若已经是最后一句，
    则标记这篇为已学完（finished=True）。
    """
    url, entries, idx = _active_article()
    if not url or idx is None:
        return {"success": False, "error": "还没有正在学的文章。请先用 list_headlines 选一篇，再用 start_article 开始。"}
    try:
        entry = _load_article(url, timeout=timeout)
    except Exception as exc:
        return {"success": False, "error": f"抓取文章失败：{exc}"}
    sentences = entry["sentences"]
    total = len(sentences)
    if not sentences:
        return {"success": False, "error": "文章正文为空。"}

    cursor = _resolve_cursor(entries[idx], sentences)
    if cursor >= total - 1:
        # 已经在最后一句，标记完成
        _save_cursor(url, entry["title"], cursor, sentences[cursor], finished=True)
        logger.info(f"next_sentence: {url} finished at {cursor}")
        return {
            "success": True,
            "url": url,
            "title": entry["title"],
            "finished": True,
            "total_sentences": total,
            "message": f"这篇《{entry['title']}》已经学完啦，共 {total} 句。可以用 list_headlines 再选一篇。",
        }

    cursor += 1
    _save_cursor(url, entry["title"], cursor, sentences[cursor], finished=False)
    logger.info(f"next_sentence: {url} -> cursor={cursor}/{total}")
    return _sentence_payload(url, entry, cursor)


@mcp.tool()
def resume() -> dict:
    """查看上次学到哪篇文章、停在第几句，用于会话开始时询问是否继续。

    不抓网络、不推进，只读进度文件。小智在孩子说“学英语”时可先调用本工具：
    - 若有未完成的文章，把标题和进度告诉孩子，问要“继续”还是“换一篇”；
      要继续就调用 start_article(那篇的 url)（会自动从断点续读）。
    - 若没有未完成的，直接用 list_headlines 让孩子选新的一篇。
    """
    content = _read_progress_raw()
    if not content.strip():
        return {"success": True, "has_unfinished": False, "message": "还没有任何学习记录。"}
    current, entries = _parse_progress(content)
    unfinished = [e for e in entries if e.get("status") != "finished"]

    active = None
    if current:
        i = _find_entry(entries, current)
        if i is not None and entries[i].get("status") != "finished":
            active = entries[i]
    if active is None and unfinished:
        active = unfinished[-1]

    if active is None:
        return {"success": True, "has_unfinished": False, "message": "之前的文章都学完了，可以选新的一篇。"}

    cursor = _entry_cursor(active)
    return {
        "success": True,
        "has_unfinished": True,
        "title": active.get("title", ""),
        "url": active.get("url", ""),
        "cursor": cursor,
        "next_sentence_number": cursor + 1,
        "last_sentence_text": active.get("last_sentence_text", "").strip().strip('"'),
        "updated": active.get("updated", ""),
        "message": f"上次在学《{active.get('title', '')}》，学到第 {cursor + 1} 句。要继续吗？",
    }


# Start the server
if __name__ == "__main__":
    mcp.run(transport="stdio")
