# server.py — Game24 MCP server.
#
# Solves the 24-point game. Returns *all* distinct solutions (deduplicated by
# canonical string form) so the caller can pick. Historically this returned
# only the first hit, and the expression style (deeply parenthesized) confused
# TTS layers like Xiaozhi that dropped the leading "((". The new format uses
# minimal parentheses and separates solutions with " ; " so downstream
# renderers keep the whole payload intact.

import logging
import sys
from fractions import Fraction

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("Game24")

# Fix UTF-8 encoding for Windows console.
if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")


GOAL = Fraction(24)

# Card-face aliases → integer value.
_ALIAS = {"a": 1, "0": 10, "j": 11, "q": 12, "k": 13}


def _parse_numbers(raw: str) -> list[int]:
    """Return the four integers, or raise ValueError with a Chinese message."""
    if " " in raw:
        tokens = raw.split()
    else:
        # No spaces → each character (or two-char "10") is one card. We keep
        # the legacy shorthand: "5577" = [5,5,7,7], "aakk" = [1,1,13,13].
        tokens = list(raw)
    if len(tokens) != 4:
        raise ValueError(f"需要 4 个数字，收到 {len(tokens)} 个：{raw!r}")

    out: list[int] = []
    for tok in tokens:
        key = tok.lower()
        if key in _ALIAS:
            out.append(_ALIAS[key])
            continue
        try:
            n = int(key)
        except ValueError as e:
            raise ValueError(f"无法识别 {tok!r}") from e
        if not 1 <= n <= 13:
            raise ValueError(f"{tok!r} 超出 1-13 范围")
        out.append(n)
    return out


# Each node in the expression tree is either a leaf (a Fraction) or an inner
# node (op, left, right). We track the *precedence* of the top-level operator
# so the pretty-printer can drop redundant parentheses.
_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}


def _combine(a, b):
    """Yield (value, expr_string, top_op) for every non-trivial combination of
    the two subexpressions. Skips division-by-zero. `a`/`b` are the same
    triple shape produced by this generator (or leaves via `_leaf`)."""
    av, atxt, aop = a
    bv, btxt, bop = b

    def par(txt, top, outer_op, side):
        # side is "L" or "R" — dictates whether equal precedence still needs
        # brackets (right side of - and /).
        if top is None:
            return txt
        if _PREC[top] > _PREC[outer_op]:
            return txt
        if _PREC[top] == _PREC[outer_op]:
            if side == "L":
                return txt
            if outer_op in ("+", "*"):
                return txt
        return f"({txt})"

    yield av + bv, f"{par(atxt, aop, '+', 'L')}+{par(btxt, bop, '+', 'R')}", "+"
    yield av - bv, f"{par(atxt, aop, '-', 'L')}-{par(btxt, bop, '-', 'R')}", "-"
    yield av * bv, f"{par(atxt, aop, '*', 'L')}*{par(btxt, bop, '*', 'R')}", "*"
    if bv != 0:
        yield av / bv, f"{par(atxt, aop, '/', 'L')}/{par(btxt, bop, '/', 'R')}", "/"


def _leaf(n: int):
    return (Fraction(n), str(n), None)


def _solve(nums: list[int]) -> list[str]:
    """Return the sorted, deduplicated set of expression strings that hit 24."""
    # Work over the list of subexpressions; each round merges two into one.
    solutions: set[str] = set()
    initial = [_leaf(n) for n in nums]

    def recurse(items):
        if len(items) == 1:
            v, txt, _ = items[0]
            if v == GOAL:
                solutions.add(txt)
            return
        for i in range(len(items)):
            for j in range(len(items)):
                if i == j:
                    continue
                rest = [items[k] for k in range(len(items)) if k != i and k != j]
                for combo in _combine(items[i], items[j]):
                    recurse(rest + [combo])

    recurse(initial)
    # A given expression may appear from multiple orderings — the set already
    # deduplicates. Sort for stable output.
    return sorted(solutions)


mcp = FastMCP("Game24")


@mcp.tool(
    description=(
        "Solve the classic 24-point card game. Input is four cards; output "
        "*, /, and 【 】 (fullwidth CJK brackets used instead of ASCII "
        "parentheses so downstream renderers don't strip them)."
        "\n\n"
        "IMPORTANT — instruction to the calling assistant: after invoking "
        "this tool, the final assistant message to the user MUST be exactly "
        "the value of the `text` field, printed verbatim, character by "
        "character. Do NOT prepend any introduction (\"here is the "
        "result\", \"好的\", etc.), do NOT append any explanation or "
        "commentary, do NOT paraphrase, translate, drop backticks, drop "
        "parentheses, or replace `*` with '×'. Emit the `text` string as "
        "the entire reply and nothing else."
    )
)
def game24(numbers: str) -> dict:
    """
    24-point solver.

    Input formats:
      - Space-separated: "5 5 7 7"
      - Four chars glued: "5577", "aakk"
        Aliases: 'a'=1, '0'=10, 'j'=11, 'q'=12, 'k'=13.

    Returns:
      {
        "success": True,
        "count": <total number of distinct solutions>,
        "solutions": [<all solutions, ASCII>],
        "text": <human-facing string, up to 3 solutions, one per line>
      }
      When no solution exists: count=0, solutions=[], text="No solution.".
    """
    logger.info(f"game24: numbers={numbers!r}")
    try:
        parsed = _parse_numbers(numbers)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    sols = _solve(parsed)
    if not sols:
        return {"success": True, "count": 0, "solutions": [], "text": "No solution."}

    preview = sols[:3]
    # Xiaozhi's LLM/renderer strips ASCII (...) segments — the visible
    # payload arrives as "*2+8=24" instead of "(3+5)*2+8=24". Fullwidth
    # 【】 aren't markdown/programming syntax so they survive the pipe.
    def _cjk_brackets(expr: str) -> str:
        return expr.replace('(', '【').replace(')', '】')

    lines = [f"`{_cjk_brackets(expr)} = 24`" for expr in preview]
    if len(sols) > len(preview):
        lines.append(f"另有 {len(sols) - len(preview)} 组解")
    text = "\n".join(lines)

    logger.info(f"game24: {len(sols)} solution(s)")
    return {"success": True, "count": len(sols), "solutions": sols, "text": text}


if __name__ == "__main__":
    mcp.run(transport="stdio")
