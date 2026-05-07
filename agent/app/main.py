"""Starlette ASGI entrypoint for the Zava sales agent.

Routes:
    GET  /                 — chat UI
    GET  /api/health       — readiness probe
    POST /api/chat         — NDJSON streaming chat with per-msg-id dedupe and
                             nano-utility tag filtering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env.local before importing modules that read env at import time.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env.local")

# Wire Azure Monitor OpenTelemetry (only when APPLICATIONINSIGHTS_CONNECTION_STRING
# is set — i.e. in deployed environments). No-op locally.
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(logger_name="app")
    except Exception as exc:  # pragma: no cover — best-effort
        logging.getLogger(__name__).warning("App Insights setup failed: %s", exc)

from langchain_mcp_adapters.client import MultiServerMCPClient
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from app.agent import build_agent, build_models
from app.streaming import event, iter_message_events
from app.tools import LOCAL_TOOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000").rstrip("/")
if not MCP_SERVER_URL.endswith("/mcp"):
    MCP_SERVER_URL = f"{MCP_SERVER_URL}/mcp"


# Funnel step names that map to the UI's frosted pill.
KNOWN_STEPS = {"greet", "qualify", "educate", "objection", "book", "handoff_to_ae"}


# ---- Lifespan -------------------------------------------------------------
async def _connect_mcp_with_retry(client: MultiServerMCPClient, attempts: int = 5) -> list:
    """Fetch MCP tools, retrying with exponential backoff on transient errors.

    Container Apps may start the agent before the MCP service is reachable;
    we want a few retries before crash-looping the container.
    """
    delay = 1.0
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            tools = await client.get_tools()
            logger.info("📦 Loaded %d MCP tool(s) from %s", len(tools), MCP_SERVER_URL)
            return tools
        except Exception as exc:
            last_exc = exc
            logger.warning("MCP get_tools attempt %d/%d failed: %s", i, attempts, exc)
            if i < attempts:
                await asyncio.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Could not reach MCP server at {MCP_SERVER_URL}") from last_exc


@asynccontextmanager
async def lifespan(app: Starlette):
    logger.info("Initialising sales agent (env=%s, mcp=%s)…", ENVIRONMENT, MCP_SERVER_URL)

    main_model, nano_model, credential = build_models()

    mcp_client = MultiServerMCPClient(
        {"zava-sales": {"url": MCP_SERVER_URL, "transport": "streamable_http"}}
    )
    mcp_tools = await _connect_mcp_with_retry(mcp_client)

    agent = build_agent(main_model, nano_model, mcp_tools)

    app.state.agent = agent
    app.state.mcp_tool_count = len(mcp_tools)
    app.state.local_tool_count = len(LOCAL_TOOLS)
    app.state.ready = True
    logger.info("✅ Agent ready (%d local + %d MCP tools)", len(LOCAL_TOOLS), len(mcp_tools))

    try:
        yield
    finally:
        try:
            await credential.close()
        except Exception:
            pass


# ---- Routes ---------------------------------------------------------------
async def index(request):
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


async def health(request):
    state = request.app.state
    ready = getattr(state, "ready", False)
    return JSONResponse(
        {
            "status": "healthy" if ready else "starting",
            "ready": ready,
            "environment": ENVIRONMENT,
            "mcp_server": MCP_SERVER_URL,
            "local_tool_count": getattr(state, "local_tool_count", 0),
            "mcp_tool_count": getattr(state, "mcp_tool_count", 0),
        },
        status_code=200 if ready else 503,
    )


async def chat(request):
    state = request.app.state
    if not getattr(state, "ready", False):
        return JSONResponse({"error": "Agent is not ready yet."}, status_code=503)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    message = body.get("message")
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    history = body.get("history") or []
    thread_id = body.get("thread_id") or str(uuid.uuid4())

    history_msgs = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role")
    ]
    history_msgs.append({"role": "user", "content": message})

    initial_state: dict[str, Any] = {"messages": history_msgs}

    config = {"configurable": {"thread_id": thread_id}}

    async def generate():
        # Always emit the thread id on the first event so the UI can
        # persist it and reuse it on the next turn.
        yield event({"thread_id": thread_id})

        full_text: list[str] = []
        last_step: str | None = None
        # Per-message dedupe: LangGraph can emit both streaming token deltas
        # AND a final aggregated chunk with the full cumulative text for the
        # same message id. Track per-msg-id text we've already streamed.
        text_by_msg: dict[str, str] = {}

        try:
            async for chunk in state.agent.astream(
                initial_state, config, stream_mode="messages"
            ):
                # stream_mode="messages" yields (AIMessageChunk, metadata) tuples.
                if isinstance(chunk, tuple) and len(chunk) >= 1:
                    msg = chunk[0]
                    metadata = chunk[1] if len(chunk) > 1 else {}
                else:
                    msg = chunk
                    metadata = {}

                # Drop chunks from internal nano-utility LLM calls (refine /
                # validate). They show up in the stream because LangGraph
                # streams every model call in the graph; we don't want them
                # in the user's chat bubble.
                tags = metadata.get("tags", []) if isinstance(metadata, dict) else []
                if "nano-utility" in tags:
                    continue

                # Surface step transitions from the per-chunk metadata.
                cur_step = None
                if isinstance(metadata, dict):
                    cur_step = metadata.get("current_step") or metadata.get("langgraph_node")
                if cur_step and cur_step != last_step and cur_step in KNOWN_STEPS:
                    last_step = cur_step
                    yield event({"step": cur_step})

                for ev in iter_message_events(msg):
                    if ev["kind"] == "text":
                        msg_id = getattr(msg, "id", None) or "_anon_"
                        chunk_text = ev["text"]
                        already = text_by_msg.get(msg_id, "")
                        new_emit = chunk_text
                        # Cumulative chunk that starts with what we've emitted.
                        if already and chunk_text.startswith(already):
                            new_emit = chunk_text[len(already):]
                        # Final aggregated chunk that repeats prior text exactly.
                        elif already and chunk_text == already:
                            continue
                        # Edge case: model produced an exact duplicate suffix.
                        elif already and already.endswith(chunk_text):
                            continue
                        if not new_emit:
                            continue
                        text_by_msg[msg_id] = already + new_emit
                        full_text.append(new_emit)
                        yield event({"chunk": new_emit})
                    elif ev["kind"] == "tool":
                        yield event({"tool": ev["tool"]})
                    elif ev["kind"] == "image":
                        yield event({"image": ev["image"]})
                    elif ev["kind"] == "citation":
                        yield event({"citation": ev["citation"]})
                    elif ev["kind"] == "citations":  # legacy
                        for doc_id in ev["doc_ids"]:
                            yield event({"citation": {"doc_id": doc_id}})
        except Exception as exc:
            logger.exception("Error during agent stream")
            yield event({"error": f"agent stream failed: {exc}"})

        yield event(
            {
                "message": "".join(full_text),
                "role": "assistant",
                "step": last_step,
                "done": True,
            }
        )

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ---- App ------------------------------------------------------------------
routes = [
    Route("/", index, methods=["GET"]),
    Route("/api/chat", chat, methods=["POST"]),
    Route("/api/health", health, methods=["GET"]),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
