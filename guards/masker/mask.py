"""Replace another person's email, a phone number, or a card number. Nothing else."""

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"(?<!\w)(?:\d{4}[ \-]){3}\d{4}(?!\w)|(?<!\w)\d{13,19}(?!\w)")
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}(?!\w)"
    r"|(?<!\w)\d{3}-\d{4}(?!\w)"
)


def _phrase(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def detail_for(counts: dict) -> str:
    parts = []
    if counts["email"]:
        parts.append(_phrase(counts["email"], "email", "emails"))
    if counts["phone"]:
        parts.append(_phrase(counts["phone"], "phone number", "phone numbers"))
    if counts["card"]:
        parts.append(_phrase(counts["card"], "card number", "card numbers"))
    if not parts:
        return "nothing to mask"
    return "masked " + ", ".join(parts)


def mask_text(text: str, user_email: str) -> tuple[str, str, dict]:
    """Return masked text, the step detail, and counts. The user's own email stays."""
    own = (user_email or "").casefold()
    counts = {"email": 0, "phone": 0, "card": 0}

    def hide_email(match: re.Match) -> str:
        if match.group(0).casefold() == own:
            return match.group(0)
        counts["email"] += 1
        return "[EMAIL]"

    def hide(kind: str, token: str):
        def replace(match: re.Match) -> str:
            counts[kind] += 1
            return token

        return replace

    masked = CARD_RE.sub(hide("card", "[CARD]"), text)
    masked = PHONE_RE.sub(hide("phone", "[PHONE]"), masked)
    masked = EMAIL_RE.sub(hide_email, masked)
    return masked, detail_for(counts), counts
