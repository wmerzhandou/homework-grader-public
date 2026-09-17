"""会话码：给每个会话一个短、好念、好打的代号，用于跨会话引用。

设计要点：
- 4 位、字母数字，且排除易混字符（0/O、1/I/L），避免口头或手输时出错；
- 同一用户内唯一（不同用户可以有相同码，因为只能引用自己的会话）；
- 存储统一大写；解析时大小写不敏感。
"""

from __future__ import annotations

import re
import secrets

CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 4

REFERENCE_RE = re.compile(r"(?<![0-9A-Za-z])[#＃]([0-9A-Za-z]{4})(?![0-9A-Za-z])")


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str:
    """把用户输入规整成存储形式（大写、去空格）。"""
    return (raw or "").strip().upper()


def is_valid_code(raw: str) -> bool:
    code = normalize_code(raw)
    return len(code) == CODE_LENGTH and all(ch in CODE_ALPHABET for ch in code)


def extract_references(text: str) -> list[str]:
    """从用户消息里取出所有会话码（去重、保持出现顺序）。"""
    found: list[str] = []
    for match in REFERENCE_RE.finditer(text or ""):
        code = normalize_code(match.group(1))
        if code not in found:
            found.append(code)
    return found
