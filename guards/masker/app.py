"""Data Masker: A2A message/send. Regex only, so case and whitespace stay put."""

from uuid import uuid4

from fastapi import FastAPI

from guards.masker.mask import mask_text

MASKER_URL = "http://127.0.0.1:10003/"
app = FastAPI()


def agent_card() -> dict:
    return {
        "name": "Data Masker",
        "description": "Masks another person's email, phone numbers, and card numbers in a reply.",
        "url": MASKER_URL,
        "version": "0.1.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "mask-reply",
                "name": "Mask a reply",
                "description": "Returns the masked text and a count of what changed.",
                "tags": ["privacy"],
            }
        ],
    }


def message_text(params: dict) -> str:
    message = params.get("message") or {}
    parts = message.get("parts") or []
    chunks = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return "".join(chunks)


@app.get("/.well-known/agent.json")
def well_known_agent() -> dict:
    return agent_card()


@app.post("/")
async def message_send(body: dict) -> dict:
    request_id = body.get("id", str(uuid4()))
    if body.get("method") != "message/send":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method must be message/send"},
        }
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": "params must be an object"},
        }
    text = message_text(params)
    metadata = params.get("metadata") or {}
    user_email = metadata.get("user_email", "") if isinstance(metadata, dict) else ""
    masked, detail, counts = mask_text(text, str(user_email))
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"text": masked, "detail": detail, "counts": counts},
    }
