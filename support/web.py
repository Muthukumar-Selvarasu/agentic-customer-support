"""Web UI and the SPEC §7.2 routes. The page only renders pipeline events."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import hmac
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from toolbox_core import ToolboxClient

from guards.sanitizer import MAX_CHARS
from support.cli import APP_NAME
from support.cli import INSTRUCTION
from support.cli import prepare_agent_tools
from support.cli import MODEL
from support.cli import TOOLBOX_URL
from support.cli import parse_rows
from support.pipeline import run_turn
from support.telemetry import setup_telemetry

PAGE = (Path(__file__).parent / "page.html").read_text()
JUDGE_CARD = "http://127.0.0.1:10002/.well-known/agent.json"
MASKER_CARD = "http://127.0.0.1:10003/.well-known/agent.json"
PHOENIX_PROJECTS = "http://127.0.0.1:6006/v1/projects"
MEM0_PING = "https://api.mem0.ai/v1/ping/"


def _body(payload) -> dict:
    return payload if isinstance(payload, dict) else {}


async def _get(url: str, headers: dict | None = None) -> str:
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status >= 400:
                    return f"HTTP {response.status}"
                return "ok"
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        return str(exc)


async def _dependency_report(toolbox: ToolboxClient) -> dict:
    judge, masker, phoenix, toolbox_status = await _gather(
        _get(JUDGE_CARD),
        _get(MASKER_CARD),
        _get(PHOENIX_PROJECTS),
        _get(TOOLBOX_URL),
    )
    report = {
        "status": "ok",
        "model": MODEL,
        "db": await _db_status(toolbox, toolbox_status),
        "toolbox": toolbox_status,
        "judge": judge,
        "masker": masker,
        "mem0": await _mem0_status(),
        "phoenix": phoenix,
    }
    if any(value != "ok" for key, value in report.items() if key not in {"status", "model"}):
        report["status"] = "degraded"
    return report


async def _gather(*aws):
    return await asyncio.gather(*aws)


async def _db_status(toolbox: ToolboxClient, toolbox_status: str) -> str:
    if toolbox_status != "ok":
        return toolbox_status
    try:
        tools = await toolbox.load_toolset("login")
        get_user = next(tool for tool in tools if tool.__name__ == "get-user")
        rows = parse_rows(await get_user("alice.jones@example.com"))
    except Exception as exc:
        return str(exc)
    if len(rows) != 1:
        return "get-user did not return Alice"
    return "ok"


async def _mem0_status() -> str:
    key = os.environ.get("MEM0_API_KEY")
    if not key:
        return "MEM0_API_KEY is not set"
    return await _get(MEM0_PING, {"Authorization": f"Token {key}"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    setup_telemetry()
    app.state.toolbox = ToolboxClient(TOOLBOX_URL)
    app.state.adk = InMemorySessionService()
    app.state.users = {}
    try:
        yield
    finally:
        await app.state.toolbox.close()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/health")
async def health(request: Request):
    report = await _dependency_report(request.app.state.toolbox)
    code = 200 if report["status"] == "ok" else 503
    return JSONResponse(report, status_code=code)


@app.post("/api/login")
async def login(request: Request):
    body = _body(await request.json())
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    try:
        tools = await request.app.state.toolbox.load_toolset("login")
        get_user = next(tool for tool in tools if tool.__name__ == "get-user")
        rows = parse_rows(await get_user(email))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    if len(rows) != 1 or not hmac.compare_digest(str(rows[0].get("password", "")), password):
        return JSONResponse({"error": "Invalid email or password."}, status_code=401)
    row = rows[0]
    agent_tools = await request.app.state.toolbox.load_toolset(
        "support-agent",
        bound_params={"customer_email": email, "user_email": email},
    )
    runner = Runner(
        app_name=APP_NAME,
        agent=LlmAgent(
            name="support_agent",
            model=MODEL,
            instruction=INSTRUCTION,
            tools=prepare_agent_tools(agent_tools),
        ),
        session_service=request.app.state.adk,
    )
    session = await request.app.state.adk.create_session(app_name=APP_NAME, user_id=email)
    request.app.state.users[email] = {"runner": runner, "session_id": session.id}
    return {
        "user_id": row["email"],
        "full_name": row["full_name"],
        "is_premium": bool(row.get("is_premium_customer")),
    }


@app.post("/api/logout")
async def logout(request: Request):
    body = _body(await request.json())
    user_id = str(body.get("user_id") or "")
    request.app.state.users.pop(user_id, None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(request: Request):
    body = _body(await request.json())
    message = body.get("message")
    user_id = str(body.get("user_id") or "")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse({"error": "message is empty"}, status_code=400)
    logged_in = request.app.state.users.get(user_id)
    if logged_in is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)
    if len(message) > MAX_CHARS:
        return JSONResponse({"error": "message too long"}, status_code=413)

    async def stream():
        async for event in run_turn(
            runner=logged_in["runner"],
            user_id=user_id,
            session_id=logged_in["session_id"],
            message=message,
        ):
            yield json.dumps(event) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
