# server.py
"""Refrigerator MCP server for Xiaozhi (智能冰箱).

refrigerator.md 记录冰箱里的东西。文件分两部分：

1. 手动清点的“基准在存”（自由文本，无时间记录）——今天大山亲自清点的结果，
   保持原样，不由本工具改动。
2. 由本 MCP 维护的“小智冰箱管理记录”结构化区域（用 HTML 注释标记包裹），
   凡是通过小智放进 / 取出冰箱的东西都记在这里，并自动记录放入时间，
   可选记录存放位置、数量、过期时间。

小智可以：
- 通过 get_refrigerator 读取全部内容，了解冰箱情况、推荐菜谱；
- 通过 add_item 放入东西（自动记时间）；
- 通过 set_expiry 补录刚放进去的东西的过期时间；
- 通过 remove_item 取出东西；
- 通过 check_expiring 查快过期 / 已过期的东西。
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, date
from typing import Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("Refrigerator")

# Fix UTF-8 encoding for Windows console (parity with sibling servers)
if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

mcp = FastMCP("Refrigerator")

FRIDGE_FILE = "refrigerator.md"
START_MARKER = "<!-- MCP_MANAGED_START -->"
END_MARKER = "<!-- MCP_MANAGED_END -->"
MANAGED_HEADING = "# 小智冰箱管理记录（本区域由小智自动维护，勿手改）"


# --------------------------------------------------------------------------- #
# 文件读写与结构化区域解析
# --------------------------------------------------------------------------- #
def _read_file() -> str:
    try:
        with open(FRIDGE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _write_file(content: str) -> None:
    with open(FRIDGE_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def _split_content(content: str) -> tuple[str, str]:
    """把文件拆成 (基准在存部分, 管理区域内部文本)。

    管理区域内部文本不含标记行本身。若文件里还没有管理区域，返回空串。
    """
    if START_MARKER in content and END_MARKER in content:
        head, rest = content.split(START_MARKER, 1)
        managed, _tail = rest.split(END_MARKER, 1)
        # 去掉管理区域的标题行，避免每次保存时重复累积
        head = head.replace(MANAGED_HEADING, "")
        return head.rstrip("\n"), managed.strip("\n")
    return content.rstrip("\n"), ""


def _parse_items(managed_text: str) -> list[dict]:
    """把管理区域每一行解析成 dict。

    每行格式：
    - 名称：牛奶 | 数量：5 | 单位：袋 | 位置：中间夹层 | 放入时间：2026-07-10 15:40 | 过期时间：2026-07-20
    """
    items: list[dict] = []
    for line in managed_text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        body = line.lstrip("-").strip()
        fields: dict[str, str] = {}
        for part in body.split("|"):
            part = part.strip()
            if "：" in part:
                k, v = part.split("：", 1)
            elif ":" in part:
                k, v = part.split(":", 1)
            else:
                continue
            fields[k.strip()] = v.strip()
        qty_raw = fields.get("数量", "").strip()
        try:
            qty = int(qty_raw) if qty_raw else 1
        except ValueError:
            qty = 1
        item = {
            "name": fields.get("名称", ""),
            "quantity": qty,
            "unit": fields.get("单位", ""),
            "location": fields.get("位置", ""),
            "added": fields.get("放入时间", ""),
            "expiry": fields.get("过期时间", ""),
        }
        if item["name"]:
            items.append(item)
    return items


def _format_item(item: dict) -> str:
    qty = item.get("quantity", 1)
    parts = [
        f"名称：{item['name']}",
        f"数量：{qty}",
        f"单位：{item.get('unit', '')}",
    ]
    if item.get("location"):
        parts.append(f"位置：{item['location']}")
    if item.get("added"):
        parts.append(f"放入时间：{item['added']}")
    if item.get("expiry"):
        parts.append(f"过期时间：{item['expiry']}")
    return "- " + " | ".join(parts)


def _save_items(head: str, items: list[dict]) -> None:
    lines = [_format_item(it) for it in items]
    managed_block = (
        f"{MANAGED_HEADING}\n\n{START_MARKER}\n"
        + ("\n".join(lines) + "\n" if lines else "")
        + f"{END_MARKER}\n"
    )
    content = head.rstrip("\n") + "\n\n" + managed_block
    _write_file(content)


def _remove_from_baseline(head: str, name: str) -> tuple[str, list[str]]:
    """从基准库存自由文本里删除包含 name 的物品词条。

    基准库存每行常见格式为“分类标题：物品1、物品2、物品3。”，物品之间用
    顿号/逗号分隔。此函数按分隔符切词，删掉命中 name 的词条：
    - 若某行删空了该分类的所有物品，则整行删除；
    - 返回 (新的 head 文本, 被删掉的词条原文列表)。
    """
    lines = head.split("\n")
    new_lines: list[str] = []
    removed: list[str] = []
    for line in lines:
        if not line.strip() or name not in line:
            new_lines.append(line)
            continue
        # 分离分类标题前缀（冒号前的部分，且 name 不在标题里）
        prefix = ""
        body = line
        m = re.match(r"^([^：:]*[：:])(.*)$", line)
        if m and name not in m.group(1):
            prefix = m.group(1)
            body = m.group(2)
        # 记录并暂时去掉行末句号
        stripped = body.rstrip()
        end_punct = ""
        if stripped.endswith("。") or stripped.endswith("."):
            end_punct = stripped[-1]
            stripped = stripped[:-1]
        tokens = re.split(r"[、,，]", stripped)
        kept: list[str] = []
        for t in tokens:
            if name in t:
                removed.append(t.strip())
            else:
                kept.append(t)
        if not removed:
            # 本行虽含 name，但切词后没命中（例如出现在标题里），原样保留
            new_lines.append(line)
        elif kept:
            new_lines.append(prefix + "、".join(kept) + end_punct)
        # kept 为空则整行删除（不加入 new_lines）
    return "\n".join(new_lines), removed


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _parse_date(s: str) -> Optional[date]:
    """尽量宽松地把日期字符串解析成 date。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# 数量（含中文量词）解析与加减
# --------------------------------------------------------------------------- #
_CN_DIGIT = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
# 常见量词
UNITS = "盒袋包个块条瓶罐只斤克升颗粒根片张份桶提排枚棵头尾朵串瓣盘碗杯"


def _cn_to_int(s: str) -> Optional[int]:
    """把阿拉伯数字或简单中文数字（支持到 99）转成 int。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    return None


def _parse_quantity(q: str) -> tuple[Optional[int], str]:
    """把数量描述拆成 (数值, 量词)。

    例：“五袋” -> (5, “袋”)；“10个” -> (10, “个”)；
    无法解析则返回 (None, 原串)。
    """
    q = (q or "").strip()
    if not q:
        return None, ""
    m = re.match(r"^\s*(\d+|[零一两二三四五六七八九十]+)\s*(.*)$", q)
    if not m:
        return None, q
    num = _cn_to_int(m.group(1))
    unit = m.group(2).strip()
    return num, unit


# --------------------------------------------------------------------------- #
# MCP 工具
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_refrigerator() -> dict:
    """读取冰箱里的全部内容（refrigerator.md）。

    小智应先调用此工具了解冰箱现状，再据此推荐菜谱、回答“冰箱里有什么”、
    “快过期的东西”等问题。返回内容包含手动清点的基准在存，以及由小智管理、
    带放入时间和过期时间的记录。
    """
    content = _read_file()
    if not content:
        return {"success": False, "error": f"{FRIDGE_FILE} 不存在或为空。"}
    head, managed = _split_content(content)
    items = _parse_items(managed)
    logger.info(f"get_refrigerator: managed_items={len(items)}")
    return {
        "success": True,
        "content": content,
        "baseline": head,
        "managed_items": items,
        "managed_count": len(items),
    }


@mcp.tool()
def add_item(
    name: str,
    quantity: int = 1,
    unit: str = "",
    location: str = "",
    expiry: str = "",
) -> dict:
    """往冰箱里放入一样东西，自动记录放入时间。

    :param name: 东西的名称，例如“牛奶”“三文鱼”。必填。
    :param quantity: 数量，纯数字，例如 5。1。默认 1。
    :param unit: 单位，例如“袋”“盒”“个”。可选。
    :param location: 存放位置，例如“中间夹层”。可选。
    :param expiry: 过期时间，例如“2026-07-20”。可选，可稍后用 set_expiry 补录。

    如果同名同位置的东西已经在冰箱里，则在原数量上累加。
    放入时间由服务器按当前时间自动填写，无需传入。
    """
    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "name 不能为空。"}
    try:
        quantity = int(quantity)
    except (ValueError, TypeError):
        quantity = 1
    if quantity < 1:
        quantity = 1
    unit = (unit or "").strip()

    content = _read_file()
    head, managed = _split_content(content)
    items = _parse_items(managed)

    now = _now_str()
    # 同名同位置（且单位一致）则累加数量
    merged = None
    for it in items:
        if it["name"] == name and it.get("unit", "") == unit and (
            not location.strip() or it.get("location", "") == location.strip()
        ):
            merged = it
            break
    if merged is not None:
        merged["quantity"] = merged.get("quantity", 0) + quantity
        merged["added"] = now
        if expiry.strip():
            merged["expiry"] = expiry.strip()
        _save_items(head, items)
        logger.info(f"add_item(merge): {name} +{quantity} -> {merged['quantity']}")
        return {
            "success": True,
            "message": f"已增加“{name}”{quantity}{unit}，现有 {merged['quantity']}{unit}。",
            "item": merged,
        }

    new_item = {
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "location": location.strip(),
        "added": now,
        "expiry": expiry.strip(),
    }
    items.append(new_item)
    _save_items(head, items)

    logger.info(f"add_item: {name} x{quantity}{unit} added at {now}")
    tail = "" if expiry.strip() else " 尚未记录过期时间，可以告诉我它什么时候过期。"
    return {
        "success": True,
        "message": f"已放入“{name}”{quantity}{unit}，放入时间 {now}。" + tail,
        "item": new_item,
    }


@mcp.tool()
def set_expiry(name: str, expiry: str) -> dict:
    """为刚放进冰箱（或已在管理记录中）的东西补录 / 修改过期时间。

    :param name: 东西的名称，需与之前放入时的名称一致（支持部分匹配）。
    :param expiry: 过期时间，例如“2026-07-20”。

    若有多件同名，默认更新最近放入的一件。
    """
    name = (name or "").strip()
    expiry = (expiry or "").strip()
    if not name or not expiry:
        return {"success": False, "error": "name 和 expiry 都不能为空。"}

    content = _read_file()
    head, managed = _split_content(content)
    items = _parse_items(managed)

    matches = [i for i, it in enumerate(items) if name in it["name"]]
    if not matches:
        return {
            "success": False,
            "error": f"管理记录里没有找到“{name}”。它可能属于手动清点的基准在存，"
            "或者还没通过我放进去。可以先用 add_item 放入。",
        }

    idx = matches[-1]  # 最近放入的一件
    items[idx]["expiry"] = expiry
    _save_items(head, items)

    logger.info(f"set_expiry: {items[idx]['name']} -> {expiry}")
    return {
        "success": True,
        "message": f"已记录“{items[idx]['name']}”的过期时间为 {expiry}。",
        "item": items[idx],
    }


@mcp.tool()
def remove_item(name: str, quantity: int = 0) -> dict:
    """从冰箱里取出一样东西。

    :param name: 东西的名称（支持部分匹配）。必填。
    :param quantity: 取出的数量，纯数字。留空或 0 表示全部取出（删除整条）；
        若数量小于库存，则扣减后保留剩余；若大于等于库存，则整条删除。

    若有多件同名，默认取出最近放入的一件。若管理记录里没有该物品，
    会尝试从手动清点的基准库存（若存在）中删除对应词条。
    """
    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "name 不能为空。"}
    try:
        quantity = int(quantity)
    except (ValueError, TypeError):
        quantity = 0
    if quantity < 0:
        quantity = 0

    content = _read_file()
    head, managed = _split_content(content)
    items = _parse_items(managed)

    matches = [i for i, it in enumerate(items) if name in it["name"]]
    if not matches:
        # 管理记录里没有，尝试从基准库存删除
        new_head, removed_tokens = _remove_from_baseline(head, name)
        if removed_tokens:
            _save_items(new_head, items)
            joined = "、".join(removed_tokens)
            logger.info(f"remove_item(baseline): {joined}")
            return {
                "success": True,
                "message": f"已从冰箱取出（删除）“{joined}”。",
                "removed_from": "baseline",
                "removed": removed_tokens,
            }
        return {"success": False, "error": f"冰箱里没有找到“{name}”。"}

    idx = matches[-1]
    item = items[idx]
    unit = item.get("unit", "")
    have = item.get("quantity", 1)

    if quantity == 0 or quantity >= have:
        # 全部取出，删除整条
        items.pop(idx)
        _save_items(head, items)
        if quantity > have:
            msg = f"“{item['name']}”只有 {have}{unit}，已全部取出。"
        else:
            msg = f"已取出“{item['name']}”{have}{unit}（已取完）。"
        logger.info(f"remove_item: {item['name']} all ({have})")
        return {"success": True, "message": msg, "item": item, "remaining": 0}

    # 部分取出，真正做减法
    item["quantity"] = have - quantity
    _save_items(head, items)
    msg = f"已取出“{item['name']}”{quantity}{unit}，剩余 {item['quantity']}{unit}。"
    logger.info(f"remove_item: {item['name']} -{quantity} -> {item['quantity']}")
    return {"success": True, "message": msg, "item": item, "remaining": item["quantity"]}


@mcp.tool()
def check_expiring(days: int = 3) -> dict:
    """检查管理记录里快过期 / 已过期的东西。

    :param days: 未来多少天内过期算“快过期”，默认 3 天。

    只检查有过期时间记录的条目（即通过小智放入并记了过期时间的东西）。
    手动清点的基准在存没有过期时间，不在检查范围内。
    """
    content = _read_file()
    head, managed = _split_content(content)
    items = _parse_items(managed)

    today = date.today()
    expired: list[dict] = []
    soon: list[dict] = []
    no_expiry: list[str] = []

    for it in items:
        d = _parse_date(it.get("expiry", ""))
        if d is None:
            if it.get("expiry"):
                # 有值但解析不了，也提醒一下
                no_expiry.append(it["name"])
            else:
                no_expiry.append(it["name"])
            continue
        delta = (d - today).days
        entry = dict(it)
        entry["days_left"] = delta
        if delta < 0:
            expired.append(entry)
        elif delta <= days:
            soon.append(entry)

    expired.sort(key=lambda x: x["days_left"])
    soon.sort(key=lambda x: x["days_left"])

    parts = []
    if expired:
        parts.append(
            "已过期：" + "、".join(f"{e['name']}（过期{-e['days_left']}天）" for e in expired)
        )
    if soon:
        parts.append(
            f"{days}天内到期：" + "、".join(f"{e['name']}（还剩{e['days_left']}天）" for e in soon)
        )
    if not expired and not soon:
        parts.append(f"暂时没有已过期或{days}天内到期的东西。")
    if no_expiry:
        parts.append("（尚未记录过期时间：" + "、".join(no_expiry) + "）")

    logger.info(f"check_expiring days={days} expired={len(expired)} soon={len(soon)}")
    return {
        "success": True,
        "message": " ".join(parts),
        "expired": expired,
        "expiring_soon": soon,
        "no_expiry_recorded": no_expiry,
    }


# Start the server
if __name__ == "__main__":
    mcp.run(transport="stdio")
