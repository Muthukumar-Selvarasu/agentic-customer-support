"""Length, character allow-list, and the cheapest obvious patterns. No model call."""

import re

MAX_CHARS = 2000
PUNCTUATION = set(" '\"#$:;@.,?!()[]{}/*+-=_&%\\|~^<>`")
OBVIOUS = (
    (re.compile(r"<\s*script\b", re.I), "script tag"),
    (re.compile(r"\$\("), "shell substitution"),
    (re.compile(r"\.\./"), "path traversal"),
)


def check_message(message: str) -> tuple[str, str]:
    """Return ('passed', detail) or ('blocked', detail)."""
    if len(message) > MAX_CHARS:
        return "blocked", f"message is longer than {MAX_CHARS} characters"
    for char in message:
        if char.isalnum() or char.isspace() or char in PUNCTUATION:
            continue
        return "blocked", f"character not allowed: {char!r}"
    for pattern, reason in OBVIOUS:
        if pattern.search(message):
            return "blocked", reason
    return "passed", "length, characters, and obvious patterns ok"
