"""Deterministic attack patterns. A hit blocks without calling the model."""

import re

PATTERNS = (
    (re.compile(r"drop\s+table", re.I), "SQL injection: DROP TABLE"),
    (re.compile(r"union\s+select", re.I), "SQL injection: UNION SELECT"),
    (re.compile(r"\bor\s+1\s*=\s*1\b", re.I), "SQL injection: OR 1=1"),
    (re.compile(r"or\s+'[^']*'\s*=\s*'", re.I), "SQL injection: tautology"),
    (re.compile(r"delete\s+from\b", re.I), "SQL injection: DELETE FROM"),
    (re.compile(r"update\s+[a-z0-9_]+\s+set\b", re.I), "SQL injection: UPDATE SET"),
    (re.compile(r"select\s+\*\s+from\b", re.I), "SQL injection: SELECT * FROM"),
    (re.compile(r"like\s+'%", re.I), "SQL injection: LIKE wildcard"),
    (re.compile(r"admin'\s*--", re.I), "SQL injection: comment"),
    (re.compile(r";\s*--", re.I), "SQL injection: statement comment"),
    (re.compile(r"<\s*script\b", re.I), "markup: script tag"),
    (re.compile(r"onerror\s*=", re.I), "markup: event handler"),
    (re.compile(r"javascript\s*:", re.I), "markup: javascript URL"),
    (re.compile(r"<\s*iframe\b", re.I), "markup: iframe"),
    (re.compile(r"\{\{"), "template injection"),
    (re.compile(r"\{%"), "template injection"),
    (re.compile(r"/etc/passwd"), "shell: passwd file"),
    (re.compile(r"\$\("), "shell: command substitution"),
    (re.compile(r";\s*cat\b", re.I), "shell: cat"),
    (re.compile(r"\.\./"), "path traversal"),
    (re.compile(r"ignore all previous instructions", re.I), "prompt extraction"),
    (re.compile(r"ignore the above", re.I), "prompt extraction"),
    (re.compile(r"system prompt", re.I), "prompt extraction"),
    (re.compile(r"print your instructions", re.I), "prompt extraction"),
    (re.compile(r"you are now dan", re.I), "prompt override"),
    (re.compile(r"forget your rules", re.I), "prompt override"),
    (re.compile(r"new instructions", re.I), "prompt override"),
    (re.compile(r"\bSYSTEM\s*:", re.I), "prompt override: fake system turn"),
    (re.compile(r"users table", re.I), "data theft: users table"),
    (re.compile(r"dump\s+customer_orders", re.I), "data theft: dump orders"),
    (re.compile(r"security judge approved", re.I), "prompt override: fake approval"),
    (re.compile(r"repeat everything above", re.I), "prompt extraction"),
    (re.compile(r"passwords of all users", re.I), "data theft: passwords"),
    (re.compile(r"export all customer", re.I), "data theft: export customers"),
    (re.compile(r"every customer", re.I), "data theft: every customer"),
    (re.compile(r"100\s*%\s*discount", re.I), "policy abuse: discount"),
    (re.compile(r"set my account to premium", re.I), "policy abuse: premium"),
)


SHOP = re.compile(
    r"\b(orders?|deliver(?:y|ies|ed)?|returns?|cancel(?:led)?|shipping|shipped|"
    r"address|packages?|account|refund|status)\b|where is my|how much did i pay|list my",
    re.I,
)


def find_pattern(message: str) -> str | None:
    for pattern, reason in PATTERNS:
        if pattern.search(message):
            return reason
    return None


def shop_question(message: str) -> bool:
    """An ordinary shop request that already missed every attack pattern."""
    return find_pattern(message) is None and SHOP.search(message) is not None


def scan_message(message: str) -> str:
    """ADK tool. Returns an explicit block or allow, never the message itself."""
    reason = find_pattern(message)
    if reason:
        return f"block: {reason}"
    return "allow: no injection patterns"
