"""One turn of the support pipeline, as SPEC §7.1 events."""

import json
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import AsyncIterator

import aiohttp

from openinference.semconv.trace import OpenInferenceSpanKindValues
from openinference.semconv.trace import SpanAttributes
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

from guards.guardrail import CUSTOMER_REPLY
from guards.guardrail import GuardrailError
from guards.guardrail import check as check_guardrail
from guards.sanitizer import check_message
from support.memory import MemoryError
from support.memory import agent_message
from support.memory import recall as recall_memories
from support.memory import save as save_memory
from support.telemetry import trace_url
from support.telemetry import tracer

JUDGE_URL = "http://127.0.0.1:10002/"
MASKER_URL = "http://127.0.0.1:10003/"
BLOCKED_REPLY = "I can't help with that request."

TOOLS = {
    "get-order-status": {
        "access": "READ",
        "statement": (
            "SELECT order_id, customer_email, delivery_address, status, items, "
            "order_date, total_amount FROM customer_orders "
            "WHERE order_id = $1 AND customer_email = $2"
        ),
        "params": ["order_id", "customer_email"],
    },
    "find-customer-orders": {
        "access": "READ",
        "statement": (
            "SELECT order_id, customer_email, delivery_address, status, items, "
            "order_date, total_amount FROM customer_orders "
            "WHERE customer_email = $1 ORDER BY order_date DESC"
        ),
        "params": ["customer_email"],
    },
    "action-log": {
        "access": "WRITE",
        "statement": (
            "INSERT INTO actions_log (user_email, action_type, parameters) "
            "SELECT $1, $2, $3::jsonb WHERE ($3::jsonb ->> 'order_id') IS NULL "
            "OR EXISTS (SELECT 1 FROM customer_orders WHERE customer_email = $1 "
            "AND order_id = ($3::jsonb ->> 'order_id')::integer) "
            "RETURNING id, user_email, action_type, parameters"
        ),
        "params": ["user_email", "action_type", "parameters"],
    },
}


def decode_args(args) -> dict:
    if not args:
        return {}
    decoded = {}
    for key, value in dict(args).items():
        if isinstance(value, str) and value.lstrip()[:1] in "{[":
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                decoded[key] = value
        else:
            decoded[key] = value
    return decoded


def unwrap_result(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, dict) and set(value) == {"result"}:
        inner = value["result"]
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return inner
        return inner
    if isinstance(value, dict) and "output" in value and "error" not in value:
        return unwrap_result(value["output"])
    return value


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _tokens(usage) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    return usage.prompt_token_count or 0, usage.candidates_token_count or 0


class JudgeError(Exception):
    """The Judge was unreachable or its verdict could not be parsed."""


class MaskerError(Exception):
    """The Masker was unreachable or its result could not be parsed."""


def _write_run(record: dict) -> None:
    folder = Path("runs/failing" if record.get("terminated") in {"error", "cap"} else "runs")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{record['turn_id']}.json").write_text(json.dumps(record, indent=2) + "\n")


async def call_judge(message: str) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
            }
        },
    }
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(JUDGE_URL, json=payload) as response:
                body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise JudgeError(f"Security Judge unreachable: {exc}") from exc
    if not isinstance(body, dict) or "error" in body:
        detail = "unparseable verdict"
        if isinstance(body, dict):
            detail = (body.get("error") or {}).get("message", detail)
        raise JudgeError(f"Security Judge: {detail}")
    result = body.get("result")
    if (
        not isinstance(result, dict)
        or result.get("verdict") not in {"allow", "block"}
        or not result.get("reason")
    ):
        raise JudgeError("Security Judge returned an unparseable verdict")
    return result


async def call_masker(reply: str, user_email: str) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": reply}],
            },
            "metadata": {"user_email": user_email},
        },
    }
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(MASKER_URL, json=payload) as response:
                body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise MaskerError(f"Data Masker unreachable: {exc}") from exc
    if not isinstance(body, dict) or "error" in body:
        detail = "unparseable mask result"
        if isinstance(body, dict):
            detail = (body.get("error") or {}).get("message", detail)
        raise MaskerError(f"Data Masker: {detail}")
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result.get("detail"):
        raise MaskerError("Data Masker returned an unparseable result")
    return result


async def run_turn(
    *,
    runner,
    user_id: str,
    session_id: str,
    message: str,
) -> AsyncIterator[dict]:
    """Yield the events for one user message and write runs/<turn_id>.json first."""
    from google.genai import types

    with tracer().start_as_current_span("agent.turn") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.CHAIN.value,
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, message)
        span.set_attribute(SpanAttributes.USER_ID, user_id)
        async for event in _turn_events(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            message=message,
            trace_id=f"{span.get_span_context().trace_id:032x}",
            url=trace_url(span.get_span_context().trace_id),
            content_type=types,
        ):
            if event["type"] == "final":
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, event["response"])
            elif event["type"] == "error":
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, event["error"])
                span.set_status(Status(StatusCode.ERROR, event["error"]))
            yield event


async def _turn_events(
    *,
    runner,
    user_id: str,
    session_id: str,
    message: str,
    trace_id: str,
    url: str,
    content_type,
) -> AsyncIterator[dict]:
    turn_id = f"turn_{uuid.uuid4().hex}"
    started = time.perf_counter()
    model = runner.agent.model if isinstance(runner.agent.model, str) else ""
    tokens_in = 0
    tokens_out = 0
    llm_calls = 0
    tool_log: list[dict] = []
    reply = ""
    next_tool_id = 1
    open_tools: dict[str, deque[tuple[int, float]]] = defaultdict(deque)
    mark = started

    def event(payload: dict) -> dict:
        return payload

    steps: list[dict] = []

    def run_record(terminated: str, blocked_at: str | None, extra_steps: list[dict]) -> dict:
        return {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "user": user_id,
            "message": message,
            "terminated": terminated,
            "blocked_at": blocked_at,
            "wall_clock_ms": _ms(started),
            "tokens": {"in": tokens_in, "out": tokens_out},
            "steps": extra_steps,
            "tool_calls": tool_log,
            "llm_calls": llm_calls,
        }

    yield event(
        {
            "type": "trace",
            "turn_id": turn_id,
            "trace_id": trace_id,
            "url": url,
        }
    )

    yield event({"type": "stage", "key": "sanitize", "label": "Sanitizer"})
    sanitize_started = time.perf_counter()
    with tracer().start_as_current_span("security.sanitize") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.GUARDRAIL.value,
        )
        sanitize_status, sanitize_detail = check_message(message)
    sanitize_ms = _ms(sanitize_started)
    steps.append({"key": "sanitize", "status": sanitize_status, "ms": sanitize_ms})
    yield event(
        {
            "type": "step",
            "key": "sanitize",
            "status": sanitize_status,
            "detail": sanitize_detail,
            "ms": sanitize_ms,
            "span": "security.sanitize",
            "kind": "Python fn",
        }
    )
    if sanitize_status == "blocked":
        record = run_record("blocked", "sanitize", steps)
        _write_run(record)
        yield event(
            {
                "type": "final",
                "blocked": True,
                "blocked_at": "sanitize",
                "response": BLOCKED_REPLY,
                "terminated": "blocked",
                "wall_clock_ms": record["wall_clock_ms"],
                "tokens": record["tokens"],
            }
        )
        return

    yield event({"type": "stage", "key": "judge", "label": "A2A Security Judge"})
    judge_started = time.perf_counter()
    try:
        with tracer().start_as_current_span("security.a2a_judge") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.GUARDRAIL.value,
            )
            verdict = await call_judge(message)
    except JudgeError as exc:
        judge_ms = _ms(judge_started)
        steps.append({"key": "judge", "status": "error", "ms": judge_ms})
        record = run_record("error", None, steps)
        _write_run(record)
        yield event(
            {
                "type": "error",
                "step": "judge",
                "status": 502,
                "error": str(exc),
                "terminated": "error",
            }
        )
        return
    judge_ms = _ms(judge_started)
    judge_status = "blocked" if verdict["verdict"] == "block" else "passed"
    steps.append({"key": "judge", "status": judge_status, "ms": judge_ms})
    yield event(
        {
            "type": "step",
            "key": "judge",
            "status": judge_status,
            "detail": f"{verdict['verdict']}: {verdict['reason']}",
            "ms": judge_ms,
            "span": "security.a2a_judge",
            "kind": "A2A",
        }
    )
    if judge_status == "blocked":
        record = run_record("blocked", "judge", steps)
        _write_run(record)
        yield event(
            {
                "type": "final",
                "blocked": True,
                "blocked_at": "judge",
                "response": BLOCKED_REPLY,
                "terminated": "blocked",
                "wall_clock_ms": record["wall_clock_ms"],
                "tokens": record["tokens"],
            }
        )
        return

    yield event({"type": "stage", "key": "guardrail", "label": "Guardrail"})
    guardrail_started = time.perf_counter()
    try:
        with tracer().start_as_current_span("guardrail.check") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.GUARDRAIL.value,
            )
            decision = await check_guardrail(message)
    except GuardrailError as exc:
        guardrail_ms = _ms(guardrail_started)
        steps.append({"key": "guardrail", "status": "error", "ms": guardrail_ms})
        record = run_record("error", None, steps)
        _write_run(record)
        yield event(
            {
                "type": "error",
                "step": "guardrail",
                "status": 502,
                "error": str(exc),
                "terminated": "error",
            }
        )
        return
    guardrail_ms = _ms(guardrail_started)
    guardrail_status = "blocked" if decision["decision"] == "unsafe" else "passed"
    steps.append({"key": "guardrail", "status": guardrail_status, "ms": guardrail_ms})
    yield event(
        {
            "type": "step",
            "key": "guardrail",
            "status": guardrail_status,
            "detail": f"{decision['decision']}: {decision['reasoning']}",
            "ms": guardrail_ms,
            "span": "guardrail.check",
            "kind": "in-process",
        }
    )
    if guardrail_status == "blocked":
        record = run_record("blocked", "guardrail", steps)
        _write_run(record)
        yield event(
            {
                "type": "final",
                "blocked": True,
                "blocked_at": "guardrail",
                "response": CUSTOMER_REPLY,
                "terminated": "blocked",
                "wall_clock_ms": record["wall_clock_ms"],
                "tokens": record["tokens"],
            }
        )
        return

    yield event({"type": "stage", "key": "recall", "label": "Memory recall"})
    recall_started = time.perf_counter()
    try:
        with tracer().start_as_current_span("memory.recall") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.RETRIEVER.value,
            )
            memories = await recall_memories(user_id, message)
            for index, item in enumerate(memories):
                span.set_attribute(
                    f"retrieval.documents.{index}.document.content",
                    item["memory"],
                )
                span.set_attribute(
                    f"retrieval.documents.{index}.document.score",
                    item["score"],
                )
    except MemoryError as exc:
        recall_ms = _ms(recall_started)
        steps.append({"key": "recall", "status": "error", "ms": recall_ms})
        record = run_record("error", None, steps)
        _write_run(record)
        yield event(
            {
                "type": "error",
                "step": "recall",
                "status": 502,
                "error": str(exc),
                "terminated": "error",
            }
        )
        return
    recall_ms = _ms(recall_started)
    steps.append({"key": "recall", "status": "passed", "ms": recall_ms})
    inserted = sum(1 for item in memories if item["inserted"])
    yield event(
        {
            "type": "step",
            "key": "recall",
            "status": "passed",
            "detail": "nothing recalled" if not memories else f"{inserted} inserted",
            "ms": recall_ms,
            "span": "memory.recall",
            "kind": "Mem0",
            "memories": memories,
        }
    )

    yield event({"type": "stage", "key": "agent", "label": "Support agent"})
    agent_started = time.perf_counter()
    prompt = agent_message(message, memories)

    try:
        async for adk_event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content_type.Content(
                role="user",
                parts=[content_type.Part(text=prompt)],
            ),
        ):
            if adk_event.partial:
                continue
            calls = adk_event.get_function_calls()
            responses = adk_event.get_function_responses()
            got_in, got_out = _tokens(adk_event.usage_metadata)
            tokens_in += got_in
            tokens_out += got_out

            if calls:
                llm_calls += 1
                names = ", ".join(call.name or "tool" for call in calls)
                yield event(
                    {
                        "type": "llm",
                        "model": model,
                        "decision": f"call {names}",
                        "tokens_in": got_in,
                        "tokens_out": got_out,
                        "ms": _ms(mark),
                        "span": "call_llm",
                    }
                )
                mark = time.perf_counter()
                for call in calls:
                    tool_id = next_tool_id
                    next_tool_id += 1
                    name = call.name or "tool"
                    open_tools[name].append((tool_id, time.perf_counter()))
                    info = TOOLS.get(name, {})
                    yield event(
                        {
                            "type": "tool_call",
                            "id": tool_id,
                            "name": name,
                            "args": decode_args(call.args),
                            "info": {
                                "kind": "MCP",
                                "access": info.get("access", "READ"),
                                "statement": info.get("statement", ""),
                                "params": info.get("params", []),
                            },
                            "span": f"execute_tool {name}",
                        }
                    )

            for response in responses:
                name = response.name or "tool"
                if not open_tools[name]:
                    raise RuntimeError(f"tool result for {name} without a tool call")
                tool_id, tool_started = open_tools[name].popleft()
                elapsed = _ms(tool_started)
                body = response.response or {}
                failed = isinstance(body, dict) and bool(body.get("error"))
                result = body.get("error") if failed else unwrap_result(body)
                tool_log.append(
                    {
                        "name": name,
                        "ok": not failed,
                        "ms": elapsed,
                        **({"error": str(result)} if failed else {}),
                    }
                )
                yield event(
                    {
                        "type": "tool_result",
                        "id": tool_id,
                        "name": name,
                        "ok": not failed,
                        "ms": elapsed,
                        "result": result,
                    }
                )

            if adk_event.is_final_response() and not calls:
                llm_calls += 1
                text = ""
                if adk_event.content and adk_event.content.parts:
                    text = "".join(part.text or "" for part in adk_event.content.parts)
                reply = text or reply
                yield event(
                    {
                        "type": "llm",
                        "model": model,
                        "decision": "final answer",
                        "tokens_in": got_in,
                        "tokens_out": got_out,
                        "ms": _ms(mark),
                        "span": "call_llm",
                    }
                )
                mark = time.perf_counter()
    except Exception as exc:
        wall = _ms(started)
        record = {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "user": user_id,
            "message": message,
            "terminated": "error",
            "blocked_at": None,
            "wall_clock_ms": wall,
            "tokens": {"in": tokens_in, "out": tokens_out},
            "steps": steps + [
                {"key": "agent", "status": "error", "ms": _ms(agent_started)}
            ],
            "tool_calls": tool_log,
            "llm_calls": llm_calls,
        }
        _write_run(record)
        yield event(
            {
                "type": "error",
                "step": "agent",
                "status": 502,
                "error": str(exc),
                "terminated": "error",
            }
        )
        return

    agent_ms = _ms(agent_started)
    steps.append({"key": "agent", "status": "passed", "ms": agent_ms})
    yield event(
        {
            "type": "step",
            "key": "agent",
            "status": "passed",
            "detail": "final answer",
            "ms": agent_ms,
            "span": "invoke_agent support_agent",
            "kind": "in-process",
        }
    )

    yield event({"type": "stage", "key": "mask", "label": "A2A Data Masker"})
    mask_started = time.perf_counter()
    try:
        with tracer().start_as_current_span("security.a2a_mask") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.GUARDRAIL.value,
            )
            masked = await call_masker(reply, user_id)
    except MaskerError as exc:
        mask_ms = _ms(mask_started)
        steps.append({"key": "mask", "status": "error", "ms": mask_ms})
        record = run_record("error", None, steps)
        _write_run(record)
        yield event(
            {
                "type": "error",
                "step": "mask",
                "status": 502,
                "error": str(exc),
                "terminated": "error",
            }
        )
        return
    reply = masked["text"]
    mask_ms = _ms(mask_started)
    steps.append({"key": "mask", "status": "passed", "ms": mask_ms})
    yield event(
        {
            "type": "step",
            "key": "mask",
            "status": "passed",
            "detail": masked["detail"],
            "ms": mask_ms,
            "span": "security.a2a_mask",
            "kind": "A2A",
        }
    )

    yield event({"type": "stage", "key": "save", "label": "Memory save"})
    save_started = time.perf_counter()
    try:
        with tracer().start_as_current_span("memory.save") as span:
            span.set_attribute(
                SpanAttributes.OPENINFERENCE_SPAN_KIND,
                OpenInferenceSpanKindValues.TOOL.value,
            )
            span.set_attribute(SpanAttributes.INPUT_VALUE, message)
            save_detail = await save_memory(user_id, message)
    except MemoryError as exc:
        save_ms = _ms(save_started)
        steps.append({"key": "save", "status": "error", "ms": save_ms})
        record = run_record("error", None, steps)
        _write_run(record)
        yield event(
            {
                "type": "error",
                "step": "save",
                "status": 502,
                "error": str(exc),
                "terminated": "error",
            }
        )
        return
    save_ms = _ms(save_started)
    steps.append({"key": "save", "status": "passed", "ms": save_ms})
    yield event(
        {
            "type": "step",
            "key": "save",
            "status": "passed",
            "detail": save_detail,
            "ms": save_ms,
            "span": "memory.save",
            "kind": "Mem0",
        }
    )

    wall = _ms(started)
    over_tools = len(tool_log) > 6
    over_tokens = (tokens_in + tokens_out) > 30000
    over_wall = wall > 30000
    if over_tools or over_tokens or over_wall:
        limit = "tool" if over_tools else "token" if over_tokens else "time"
        record = run_record("cap", None, steps)
        record["wall_clock_ms"] = wall
        _write_run(record)
        yield event(
            {
                "type": "final",
                "blocked": False,
                "blocked_at": None,
                "response": f"This request hit the {limit} limit, so I stopped before finishing.",
                "terminated": "cap",
                "wall_clock_ms": wall,
                "tokens": {"in": tokens_in, "out": tokens_out},
            }
        )
        return
    record = run_record("done", None, steps)
    record["wall_clock_ms"] = wall
    _write_run(record)
    yield event(
        {
            "type": "final",
            "blocked": False,
            "blocked_at": None,
            "response": reply,
            "terminated": "done",
            "wall_clock_ms": wall,
            "tokens": {"in": tokens_in, "out": tokens_out},
        }
    )
