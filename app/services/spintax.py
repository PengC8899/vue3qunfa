"""Spintax 文案变体引擎。

语法 `{你好|您好|hi}`，支持嵌套 `{你好{呀|哇}|hi}`，反斜杠转义 `\\{` `\\|` `\\}` `\\\\`。

为什么需要它：Telegram 的反垃圾对「短时间内大量完全相同的文本」加权。
一批几百个群发同一句话等于自己举手，每条消息走一次 expand() 之后字面各不相同，
重复度权重就散了。

和 `send_scheduler.fingerprint_message` 里那套（随机增删 emoji、空格抖动）互补：
那套只是在同一句话上加噪音，字面仍然高度相似；spintax 换的是词和句式，才是真的换文案。
两者叠加使用。
"""
from __future__ import annotations

import random

_SPECIAL = "{}|\\"


class SpintaxError(ValueError):
    """花括号不配对等语法错误。"""


# 语法树节点：str 是字面量，list 是顺序拼接（Seq），tuple 是多选一（Choice）
Node = object


def _parse(text: str, pos: int, depth: int) -> tuple[list, int]:
    """解析一段顺序节点；遇到 `|` 或 `}`（depth>0 时）就交还给上层。"""
    seq: list = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            seq.append("".join(buf))
            buf.clear()

    n = len(text)
    while pos < n:
        ch = text[pos]
        if ch == "\\" and pos + 1 < n and text[pos + 1] in _SPECIAL:
            buf.append(text[pos + 1])
            pos += 2
            continue
        if ch == "{":
            flush()
            choice, pos = _parse_choice(text, pos + 1, depth + 1)
            seq.append(choice)
            continue
        # depth=0 时孤立的 } 当普通字符，消息里常带 JSON / 颜文字
        if depth > 0 and ch in "|}":
            break
        buf.append(ch)
        pos += 1
    flush()
    return seq, pos


def _parse_choice(text: str, pos: int, depth: int) -> tuple[tuple, int]:
    """解析 `{a|b|c}`，pos 指向 `{` 之后的第一个字符。"""
    options: list[list] = []
    while True:
        seq, pos = _parse(text, pos, depth)
        options.append(seq)
        if pos >= len(text):
            raise SpintaxError("花括号没有闭合：缺少 }")
        if text[pos] == "|":
            pos += 1
            continue
        if text[pos] == "}":
            return tuple(options), pos + 1
        raise SpintaxError(f"位置 {pos} 处出现意外字符 {text[pos]!r}")


def _compile(text: str) -> list:
    seq, pos = _parse(text, 0, 0)
    if pos != len(text):
        raise SpintaxError(f"位置 {pos} 处有多余的 }}，如需字面量请写 \\}}")
    return seq


def _render(seq: list, rng: random.Random) -> str:
    out: list[str] = []
    for node in seq:
        if isinstance(node, str):
            out.append(node)
        else:
            out.append(_render(rng.choice(node), rng))
    return "".join(out)


def _count(seq: list) -> int:
    total = 1
    for node in seq:
        if not isinstance(node, str):
            total *= sum(_count(opt) for opt in node)
    return total


def has_spintax(text: str) -> bool:
    """有没有可展开的东西。没有就走快路，避免白解析一遍。"""
    return "{" in text or "\\" in text


def expand(text: str, rng: random.Random | None = None) -> str:
    """随机展开一条变体。没有 spintax 标记也没有转义时原样返回。"""
    if not has_spintax(text):
        return text
    try:
        return _render(_compile(text), rng or random)
    except SpintaxError:
        # 文案写错了不该让整条发送任务挂掉，退化成原文
        return text


def count(text: str) -> int:
    """可生成的变体总数。1 表示这条文案没有任何变化。

    语法有问题时也按 1 返回而不是抛错（发消息的路径不能因为文案写错就崩）；
    要区分「真的只有 1 种」和「写错了」，用 validate。
    """
    if not has_spintax(text):
        return 1
    try:
        return _count(_compile(text))
    except SpintaxError:
        return 1


def validate(text: str) -> dict:
    """给接口/UI 用的一次性校验 + 预览。"""
    if not has_spintax(text):
        return {"ok": True, "variants": 1, "preview": text, "warning": "这条文案没有 {a|b} 变体标记，每次发出去都一样"}
    try:
        total = _count(_compile(text))
    except SpintaxError as exc:
        return {"ok": False, "variants": 0, "preview": "", "error": str(exc)}
    return {
        "ok": True,
        "variants": total,
        "preview": expand(text),
        "warning": "变体数过少，群发时仍可能被判定为重复内容" if total < 5 else None,
    }
