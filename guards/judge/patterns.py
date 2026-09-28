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
)


def find_pattern(message: str) -> str | None:
    for pattern, reason in PATTERNS:
        if pattern.search(message):
            return reason
    return None


def scan_message(message: str) -> str:
    """ADK tool. Returns an explicit block or allow, never the message itself."""
    reason = find_pattern(message)
    if reason:
        return f"block: {reason}"
    return "allow: no injection patterns"
