# DESIGN

> Copy this file to `DESIGN.md` and answer it **before you write code**. Revise it as you
> learn, but keep the first version in git history. A stranger grades it (`G-DESIGN`), so
> write for someone who has never seen your code. The five headings are fixed; keep them.

## Components

The moving parts, their external services, and their designated ports are:

* **PostgreSQL (`:5432`)**: Local relational database storing tables for `users`, `customer_orders`, and `actions_log`.
* **MCP Toolbox for Databases (`:5000`)**: Standalone server process exposing parameter-bound database tools (`get-order-status`, `find-customer-orders`, `action-log`) over the Model Context Protocol.
* **Security Judge Service (`:10002`)**: Standalone A2A service hosting an agent card at `/.well-known/agent.json` and exposing a JSON-RPC endpoint to screen for prompt injections, shell escapes, and adversarial attacks.
* **Data Masker Service (`:10003`)**: Standalone A2A service exposing an agent card and JSON-RPC endpoint to sanitize PII from agent outputs.
* **Arize Phoenix (`:6006`)**: Out-of-process observability service collecting OpenInference traces with persistent disk storage across restarts.
* **Mem0 Cloud Service (External API)**: Cloud-hosted vector and memory platform accessed over HTTPS for customer preference retrieval and persistence.
* **Web UI Server (`:8000`) & CLI Client**: Two distinct presentation frontends that contain zero pipeline logic. Both drive the shared `pipeline` module.
* **Sanitizer (In-process)**: A Python function running inside the pipeline to enforce length limits and character allow-lists before any external calls.
* **Guardrail (In-process)**: An ADK agent running in a fresh, ephemeral session to verify the message is on-topic for this specific support desk.
* **Support Agent (In-process)**: The core ADK agent responsible for reasoning, invoking the database tools via MCP, and generating the final response.

### Why Judge & Masker are separate services while the Guardrail runs in-process

If our security officer walks into the room, she wants to know that no prompt injection can hit any internal system and no customer PII leaks out. Her team writes and updates the Judge and Masker so that every agent in our company runs through the same audited gates.

The Guardrail belongs to the support team alone: it asks whether a message is about our shop's orders, returns, and delivery instructions. When our shop adds a new product line or updates our return window, our support engineers update that prompt directly inside the app repo without waiting on the security team.

### Why the two front ends share one pipeline module, and why memory is a code step

The CLI and the streaming web UI are two windows into the exact same engine. If pipeline logic lived inside the web server or the CLI loop, the implementations would drift: a bug fixed on the CLI would break the web demo. With one pipeline module yielding events, both front ends remain thin consumers.

Memory recall and save are hardcoded pipeline steps because anything that must happen on every turn cannot be left to an LLM's mood. If memory were exposed as a tool, the model would skip recall on simple queries or forget to save standing delivery notes.

---

## Responsibilities

* **Order Isolation**: Decided strictly at the database query layer via MCP Toolbox tool declarations (`tools.yaml`). Parameter binding via `toolbox-core` enforces that `customer_email` is populated from the authenticated session context rather than model output.
  * Shipped `get-order-status`: `SELECT order_id, customer_email, delivery_address, status, items, order_date, total_amount FROM customer_orders WHERE order_id = $1 AND customer_email = $2`. `$2` is `customer_email`, bound from the logged-in session, not chosen by the model.
  * Shipped `find-customer-orders`: `SELECT order_id, customer_email, delivery_address, status, items, order_date, total_amount FROM customer_orders WHERE customer_email = $1 ORDER BY order_date DESC`. `$1` is `customer_email`, bound from the session.
* **API Key Custody**: `GOOGLE_API_KEY` and `MEM0_API_KEY` are held exclusively by local backend runtimes: the web server process, the CLI process (which imports and runs the pipeline directly), and the two A2A microservices (Judge and Masker). The browser frontend never sees or receives API keys.
* **Turn Budgets**: Decided and enforced in code by the central `pipeline` orchestration loop by tracking elapsed wall-clock time (`T-BUD-WALL`), tool execution iterations (`T-BUD-TOOLS`), and token counters (`T-BUD-TOKENS`) across steps.

### Rule Enforcement Matrix (SPEC §11 + Course Rules)

| Rule | Description | Enforcement Point | Architectural Justification |
|---|---|---|---|
| **R-1** | Ownership checked in SQL via bound session email | **In code** (SQL & toolbox-core binding) | Prompt instructions ("only access this user's records") fail under adversarial prompt injection. Binding the session email to a parameterized SQL query physically prevents cross-tenant access. |
| **R-2** | Mandatory per-turn tasks run as pipeline steps | **In code** (Pipeline DAG) | Prompts telling an LLM to "always recall memory" are routinely skipped on short user turns. Fixed pipeline code guarantees execution every single turn. |
| **R-3** | Exclusion of mutating tools from agent toolset | **In code** (Tool registration) | Negative prompt constraints ("do not mutate orders") are easily bypassed. Omitting update/delete tools from the MCP registration eliminates the write capability at the protocol level. |
| **R-4** | Guard errors fail the turn loudly | **In code** (Pipeline error emission) | A timeout or a verdict you cannot parse ends the turn with an error event, and this is not turned into allow. Catching an exception and falling open causes silent security bypasses. |
| **R-5** | Save only user text to memory, never agent output | **In code** (Pipeline `save` stage) | Models hallucinate disclaimers or false policies. Saving model replies contaminates the vector store; filtering input arguments in code ensures only genuine user preferences persist. |
| **R-6** | Guardrail defines product scope with shop-specific examples | **In code (fast path) / In prompt** | Ordinary shop questions return `safe` in code before the Guardrail model is even called. For edge cases, categorizing semantic intent requires context-aware model evaluation. |
| **R-7** | Reject over-long memories before prompt insertion | **In code** (Recall length filter) | Mem0 merge loops can produce pathological text blobs. Hard length checks against `T-MEM-MAXCHARS` protect the context window deterministically. |
| **R-8** | Data Masker changes only targeted PII | **In code** (Regex replacement) | Because the Masker code uses targeted regex replacements, it fundamentally cannot alter case or whitespace. |
| **R-9** | Explicit verdict schema (`allow`/`block`), never echo | **Both** (Prompt schema + code validator) | Prompt instructs structured JSON output; pipeline code parses and validates the explicit verdict field. |
| **R-10** | Traces persist outside the application process | **In code** (OTel gRPC/HTTP exporter) | Tracing data must stream out-of-process to the persistent Phoenix collector so crashes or restarts do not destroy telemetry history. |
| **R-11** | Enforce action enum types at schema level | **In code** (PostgreSQL DDL `CHECK` constraint) | Relying on the LLM to pick valid enum strings leads to database drift. Enforcing the constraint in SQL prevents bad writes. |
| **Grounded or nothing** | Order facts must come from a tool call in that turn | **In prompt** (Agent instruction) | The active control during the turn is the agent prompt, which strictly directs the model to trust tools. The eval suite only scores the turn afterward. |
| **Fail loud** | Downstream dependencies that fail end the turn | **In code** (Step-level status validation) | Unreachable microservices or unparseable JSON immediately emit terminal `error` events with HTTP 502 semantics rather than failing open. |
| **Bounded and honest** | Wall clock, token, and tool bounds are strictly enforced | **In code** (Pipeline budget counters) | The orchestrator loop checks counters against thresholds on every iteration, terminating with `terminated: "cap"` when exceeded. |
| **Evidence over vibes** | All quality assertions must come from the eval runner | **In code** (Eval test suite & run log validator) | Metrics are asserted against machine-readable JSON logs in `runs/<turn_id>.json`, never subjective impressions. |

* **False block**: The passing eval measured `T-LEGIT-FALSE-BLOCK` at 0.0 (n=30). There is no trace id, because no legitimate customer message was blocked.
* **False pass**: The passing eval measured `T-ATTACK-BLOCK` at 1.0 (n=30). There is no false-pass trace id. An earlier run allowed X30, the premium-discount attack; a Judge pattern now blocks that message before the model runs, and the final run has no remaining false pass to open.
---

## Communication

How does a message travel from the CLI or browser to the database and back? Name each hop's protocol (function call, MCP, A2A JSON-RPC, HTTP, NDJSON). Which A2A method did you implement, and what does a verdict look like on the wire?

### Request/Response Hop Trajectory (11 Hops)

1. **Client to Pipeline (HTTP / Function Call)**: User input originates from the Browser (`HTTP POST /api/chat` over HTTP/1.1 streaming `application/x-ndjson`) or CLI (direct Python async function call to `pipeline.run_turn()`).
2. **Pipeline to Sanitizer (Function Call)**: The pipeline calls `guards/sanitizer.py` in-process to verify character allow-lists and length bounds.
3. **Pipeline to Security Judge (A2A JSON-RPC over HTTP)**: The pipeline sends an HTTP `POST` request to `http://localhost:10002/` executing the `message/send` method with the user's message.
4. **Security Judge to Pipeline (A2A JSON-RPC Response)**: The Judge returns the JSON-RPC response containing the verdict and reasoning.
5. **Pipeline to Guardrail (Function Call / ADK)**: The pipeline invokes the in-process ADK Guardrail agent in an ephemeral session to evaluate store topic relevance.
6. **Pipeline to Mem0 Cloud (HTTPS / REST)**: The pipeline issues an HTTPS REST query to Mem0 Cloud to recall standing user preferences filtered by `customer_email`.
7. **Pipeline to Support Agent & MCP Toolbox (MCP over JSON-RPC / SSE)**: The ADK agent runs; on tool execution, it communicates with MCP Toolbox (`http://localhost:5000`) over MCP.
8. **MCP Toolbox to PostgreSQL (PostgreSQL Wire Protocol over TCP)**: MCP Toolbox runs the parameterized query against PostgreSQL on port 5432 and receives raw rows.
9. **Pipeline to Data Masker (A2A JSON-RPC over HTTP)**: The pipeline sends an HTTP `POST` to `http://localhost:10003/` executing `message/send` with the agent's text output to redact PII.
10. **Pipeline to Mem0 Cloud (HTTPS / REST)**: On non-blocked, successful turns, the pipeline sends an HTTPS REST request to Mem0 Cloud persisting only the user's message.
11. **Pipeline to Client (NDJSON stream)**: The pipeline streams NDJSON events (`application/x-ndjson`) back to the client interface, concluding with a terminal `final` or `error` object.

### A2A Method & Verdict Payloads on the Wire

We implement the current A2A specification method **`message/send`**.

#### Allow Verdict Payload (Wire Format)
```json
{
  "jsonrpc": "2.0",
  "id": "turn_01HZX8B5C0QW8Z1K9P3E7R2M0A",
  "result": {
    "verdict": "allow",
    "reason": "Input contains standard order status query; no SQL injection, template injection, or prompt extraction patterns detected."
  }
}
```

### Block Verdict Payload (Wire Format)
```json
{
  "jsonrpc": "2.0",
  "id": "turn_01HZX8B5C0QW8Z1K9P3E7R2M0A",
  "result": {
    "verdict": "block",
    "reason": "Blocked: SQL injection signature detected ('; DROP TABLE users; --)."
  }
}
```
---

## State

| State Category | Storage Location | Retention / TTL | Customer Data Included? | Access Controls |
|---|---|---|---|---|
| **Sessions** | Application memory dictionary / cache | Lifespan of active CLI process or until `POST /api/logout` / session expiry | Yes (User email, full name, auth state) | Internal backend pipeline only |
| **Memories** | Mem0 Cloud vector store | Persistent indefinitely across sessions until deleted | Yes (Customer delivery notes, standing preferences) | Scoped strictly by user email filter (`user_id = email`) |
| **Traces** | Arize Phoenix persistent disk storage | Retained across application process restarts | Yes (Sanitized inputs, tool SQL parameters, masked answers) | Operations engineers & security auditors with Phoenix access |
| **Run Logs** | Local disk (`runs/<turn_id>.json`) | Immutable local audit records for grading and evaluation | Yes (Complete per-turn event logs, token usage, latencies) | Local process filesystem / test runner |
| **Actions Log** | PostgreSQL `actions_log` table | Permanent append-only audit trail | Yes (User email, action enum, payload parameters) | Customer support staff, fulfillment automation systems |

### Handling Order Changes Relative to Existing Memory

Mem0 is never used to cache or infer order facts. Order facts (status, tracking numbers, items) are strictly retrieved dynamically on demand via parameterized MCP database tools. The system prompt explicitly instructs the agent to trust active tool results over memory context for any order fact. When an order is updated in PostgreSQL (or recorded via `action-log`), subsequent turns execute fresh SQL reads against the database. Mem0 remains dedicated solely to standing user behavioral preferences (e.g., "leave packages on the back porch").


---

## Trade-offs

What did each guard cost you in latency (from your traces), and was it worth it? Did you
make the Guardrail fail closed, and what does that cost when Gemini is slow? If you changed
`T-MEM-MINSCORE` or argued against any threshold, give the evidence here, and keep the
original gate in your report.

* **Latency Cost of Guards (from Traces)**:
  * Based on 12 order turns (T-LAT-P50: 5353 ms, T-LAT-P95: 7376 ms), the median step times were:
    * Sanitizer: 3 ms
    * Judge: 5 ms
    * Guardrail: 2 ms
    * Recall: 428 ms
    * Agent: 4480 ms
    * Masker: 5 ms
    * Save: 827 ms
  * *Note*: Those Judge and Guardrail times are the shop fast path: ordinary shop questions return `allow` and `safe` in code, skipping the models. That cost is worth it. The agent median is 4480 ms, so the guards are not what makes a shop question slow. The pattern-only block on the passing eval is `T-LAT-BLOCK-P95` 11 ms, against a bar of `<= 5000`.
* **Security Judge Decision Policy (SPEC §17)**:
  * The pattern tool alone can block without consulting the model. The Judge model runs only when the message is not a pattern block and not an ordinary shop question. Ordinary shop questions return `allow` in code. The measured pattern-only block latency is `T-LAT-BLOCK-P95` 11 ms, which clears `<= 5000`.
* **Fail-Closed Guardrail Operational Impact**:
  * Configured strictly to fail closed per P-4 and R-4. If Gemini is slow, times out, or returns an unparseable verdict, the Guardrail treats this as an operational failure and terminates the turn with an explicit `error` event (`status: 502`) rather than passing an unchecked reply.
  * *Cost*: Under API latency spikes or degradation, paying customers encounter a hard failure rather than receiving an uninspected answer. This design deliberately prioritizes data security and containment over availability.
* **`T-MEM-MINSCORE` Cutoff Calibration**:
  * I lowered the Mem0 score cutoff to 0.15 because the eval run demonstrated that the default 0.25 threshold was too strict, dropping genuine customer memories that scored between 0.16 and 0.23. Memories scoring below 0.15 or exceeding `T-MEM-MAXCHARS` are reported as skipped.