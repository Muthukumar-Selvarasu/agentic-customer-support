"""Recall and save are pipeline steps. The agent has no memory tool."""

import os

from mem0 import AsyncMemoryClient

T_MEM_TOPK = 5
T_MEM_MINSCORE = 0.15
T_MEM_MAXCHARS = 500
MEMORY_HEADER = "Relevant memories about this customer (from Mem0):"

_client: AsyncMemoryClient | None = None


class MemoryError(Exception):
    """Mem0 was unreachable or returned something this step cannot use."""


def client() -> AsyncMemoryClient:
    global _client
    if _client is None:
        if not os.environ.get("MEM0_API_KEY"):
            raise MemoryError("MEM0_API_KEY is not set")
        _client = AsyncMemoryClient()
    return _client


def select_memories(candidates: list[dict]) -> list[dict]:
    """Keep a memory only at or above the cutoff and within the length cap."""
    chosen = []
    for item in candidates:
        text = str(item.get("memory") or "")
        score = float(item.get("score") or 0)
        if len(text) > T_MEM_MAXCHARS:
            reason = "too long"
            inserted = False
        elif score < T_MEM_MINSCORE:
            reason = "below cutoff"
            inserted = False
        else:
            reason = None
            inserted = True
        chosen.append(
            {
                "memory": text,
                "score": score,
                "inserted": inserted,
                "reason": reason,
            }
        )
    return chosen


def agent_message(message: str, memories: list[dict]) -> str:
    """Put inserted memories above the customer's words, under a fixed header."""
    lines = [item["memory"] for item in memories if item.get("inserted")]
    if not lines:
        return message
    body = "\n".join(f"- {line}" for line in lines)
    return f"{MEMORY_HEADER}\n{body}\n\n{message}"


def _rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        raw = payload.get("results", [])
    elif isinstance(payload, list):
        raw = payload
    else:
        raise MemoryError("Mem0 search returned an unparseable result")
    if not isinstance(raw, list):
        raise MemoryError("Mem0 search returned an unparseable result")
    rows = []
    for item in raw[:T_MEM_TOPK]:
        if not isinstance(item, dict):
            continue
        text = item.get("memory") or item.get("text") or ""
        if not str(text).strip():
            continue
        score = item.get("score", item.get("similarity", 0))
        try:
            score = float(score)
        except (TypeError, ValueError) as exc:
            raise MemoryError("Mem0 search returned an unparseable score") from exc
        rows.append({"memory": str(text), "score": score})
    return rows


async def recall(user_id: str, message: str) -> list[dict]:
    """Search this user's memories. A failure is an error, not an empty list."""
    try:
        payload = await client().search(
            message,
            filters={"user_id": user_id},
            top_k=T_MEM_TOPK,
        )
    except MemoryError:
        raise
    except Exception as exc:
        raise MemoryError(f"Mem0 recall failed: {exc}") from exc
    return select_memories(_rows(payload))


async def save(user_id: str, message: str) -> str:
    """Store the user's message only. Mem0 extracts the fact after this returns."""
    try:
        payload = await client().add(message, user_id=user_id)
    except Exception as exc:
        raise MemoryError(f"Mem0 save failed: {exc}") from exc
    status = payload.get("status") if isinstance(payload, dict) else None
    if status:
        return f"saved user message ({status})"
    return "saved user message"
