"""In-process scope check. One fresh session per message, then deleted."""

import json

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

PROMPT = (
    "You are a Guardrail agent for an online shop's customer support desk. "
    "Your sole job is to determine if a customer's message is safe and on-topic. "
    "The only allowed topics are: orders, deliveries, returns, account management, "
    "and customer preferences. Important: Personal details shared by the customer "
    "to get better service (such as delivery instructions, working from home, or "
    "back door drop-offs) are explicitly ON-TOPIC and must be allowed. Output a "
    "structured JSON response with 'decision' ('safe' or 'unsafe') and 'reasoning'."
)
CUSTOMER_REPLY = (
    "I can only help with orders, deliveries, returns, and your account."
)
APP_NAME = "guardrail"
MODEL = "gemini-3.8-flash"

_runner: Runner | None = None
_sessions: InMemorySessionService | None = None


class GuardrailError(Exception):
    """The Guardrail returned a decision that could not be parsed."""


def _runner_once() -> tuple[Runner, InMemorySessionService]:
    global _runner, _sessions
    if _runner is None or _sessions is None:
        _sessions = InMemorySessionService()
        _runner = Runner(
            app_name=APP_NAME,
            agent=LlmAgent(
                name="guardrail_agent",
                model=MODEL,
                instruction=PROMPT,
            ),
            session_service=_sessions,
        )
    return _runner, _sessions


def parse_decision(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if payload.get("decision") not in {"safe", "unsafe"} or not payload.get("reasoning"):
        return None
    return {"decision": payload["decision"], "reasoning": payload["reasoning"]}


async def check(message: str) -> dict:
    runner, sessions = _runner_once()
    session = await sessions.create_session(app_name=APP_NAME, user_id="guardrail")
    text = ""
    try:
        async for event in runner.run_async(
            user_id="guardrail",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(part.text or "" for part in event.content.parts)
    finally:
        await sessions.delete_session(
            app_name=APP_NAME,
            user_id="guardrail",
            session_id=session.id,
        )
    decision = parse_decision(text)
    if decision is None:
        raise GuardrailError("Guardrail returned an unparseable decision")
    return decision
