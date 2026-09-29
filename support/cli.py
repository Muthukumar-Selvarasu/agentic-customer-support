"""CLI for the support agent. Renders pipeline events; it does not run the turn."""

import argparse
import asyncio
import getpass
import hmac
import json
import sys

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from toolbox_core import ToolboxClient

from support.pipeline import run_turn
from support.telemetry import setup_telemetry

APP_NAME = "support"
TOOLBOX_URL = "http://127.0.0.1:5000"
MODEL = "gemini-3.8-flash"

def parse_rows(raw: str) -> list[dict]:
    payload = json.loads(raw)
    if isinstance(payload, dict) and "result" in payload:
        inner = payload["result"]
        payload = json.loads(inner) if isinstance(inner, str) else inner
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


INSTRUCTION = """
You are the support agent for this shop. Customers ask about their own orders.

Tools:
- get-order-status looks up one order by id, and only if it belongs to the logged-in customer.
- find-customer-orders lists this customer's orders, newest first.
- action-log records a requested change. It does not change the order itself.

Order facts come only from tool results. If the customer names an order id, call
get-order-status before you answer. If they name a product or ask what they ordered,
call find-customer-orders. If a tool returns no rows, say that order is not on this
account. Do not describe another customer's order, items, or status.

A cancel, return, address change, or profile change is one action-log call. If you
need the order id first, call find-customer-orders, then action-log once. parameters
is a JSON string and includes order_id when the request is about an order.
When the action-log result starts with SUCCESS, that call worked. Reply to the
customer. Do not call action-log or any other tool again on this turn.

Standing preferences may appear above the customer's message under the header
"Relevant memories about this customer (from Mem0):". Trust tool results over
those memories for order facts.
""".strip()

ACTION_SUCCESS = (
    "SUCCESS. This action is recorded. Do not call action-log again. "
    "Do not call any other tool. Reply to the customer now."
)


class _ActionLogResult:
    """Same tool the model already has. A saved row comes back marked SUCCESS."""

    def __init__(self, inner):
        self._inner = inner
        self.__name__ = inner.__name__
        self.__doc__ = (
            (inner.__doc__ or "")
            + "\n\nCall this at most once. A result that starts with SUCCESS is final. "
            "Reply to the customer and do not call this tool again."
        )
        self.__signature__ = inner.__signature__
        self.__annotations__ = dict(getattr(inner, "__annotations__", {}))

    async def __call__(self, *args, **kwargs):
        result = await self._inner(*args, **kwargs)
        text = "" if result is None else str(result)
        if text.strip() in {"", "[]", "null", "None"}:
            return text
        return f"{ACTION_SUCCESS}\n{text}"


def prepare_agent_tools(tools):
    """Mark a saved action-log row so the model stops after the first success."""
    prepared = []
    for tool in tools:
        if getattr(tool, "__name__", "") == "action-log":
            prepared.append(_ActionLogResult(tool))
        else:
            prepared.append(tool)
    return prepared


def render(event: dict) -> str | None:
    kind = event["type"]
    if kind == "trace":
        return f"[trace] {event['url']}"
    if kind == "stage":
        return None
    if kind == "step":
        memories = event.get("memories") or []
        if memories:
            bits = []
            for item in memories:
                flag = "inserted" if item.get("inserted") else "skipped"
                bits.append(f"\"{item.get('memory', '')}\" {item.get('score')} {flag}")
            tail = " · " + ", ".join(bits)
        elif event.get("detail"):
            tail = f" · {event['detail']}"
        else:
            tail = ""
        return (
            f"[{event['key']}] {event['status']} · {event.get('ms', 0)} ms · "
            f"{event.get('span', event['key'])}{tail}"
        )
    if kind == "llm":
        return f"[llm] {event['decision']} · {event['ms']} ms · {event['span']}"
    if kind == "tool_call":
        args = json.dumps(event["args"])
        return f"[tool_call] {event['name']}({args})"
    if kind == "tool_result":
        state = "ok" if event["ok"] else "error"
        return f"[tool_result] {event['name']} {state} · {event['ms']} ms"
    if kind == "final":
        return f"Agent: {event['response']}"
    if kind == "error":
        return f"[error] {event['step']} · {event['error']}"
    return None


async def lookup_user(client: ToolboxClient, email: str) -> str:
    tools = await client.load_toolset("login")
    get_user = next(tool for tool in tools if tool.__name__ == "get-user")
    rows = parse_rows(await get_user(email))
    if len(rows) != 1:
        raise SystemExit("Login failed.")
    return rows[0]["full_name"]


async def login(client: ToolboxClient) -> tuple[str, str]:
    tools = await client.load_toolset("login")
    get_user = next(tool for tool in tools if tool.__name__ == "get-user")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    rows = parse_rows(await get_user(email))
    if len(rows) != 1 or not hmac.compare_digest(rows[0].get("password", ""), password):
        raise SystemExit("Login failed.")
    return email, rows[0]["full_name"]


def show(event: dict, events_only: bool) -> None:
    if events_only:
        print(json.dumps(event), flush=True)
        return
    line = render(event)
    if line:
        print(line, flush=True)


async def one_turn(runner, email: str, session_id: str, message: str, events_only: bool) -> None:
    async for event in run_turn(
        runner=runner,
        user_id=email,
        session_id=session_id,
        message=message,
    ):
        show(event, events_only)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", action="store_true")
    parser.add_argument("--user", default=None)
    args = parser.parse_args()
    load_dotenv()
    setup_telemetry()
    client = ToolboxClient(TOOLBOX_URL)
    try:
        if args.user:
            email = args.user.strip()
            await lookup_user(client, email)
        else:
            email, name = await login(client)
            if not args.events:
                print(f"Logged in as {name}. Empty line quits.")
        tools = await client.load_toolset(
            "support-agent",
            bound_params={"customer_email": email, "user_email": email},
        )
        agent = LlmAgent(
            name="support_agent",
            model=MODEL,
            instruction=INSTRUCTION,
            tools=prepare_agent_tools(tools),
        )
        session_service = InMemorySessionService()
        session = await session_service.create_session(app_name=APP_NAME, user_id=email)
        runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)
        if args.user:
            message = sys.stdin.read().strip()
            if not message:
                raise SystemExit("message is empty")
            await one_turn(runner, email, session.id, message, args.events)
            return
        while True:
            line = input("You: ").strip()
            if line in {"", "quit", "exit", "q"}:
                break
            await one_turn(runner, email, session.id, line, args.events)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
