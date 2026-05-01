"""Agent API ASGI application.

A LangChain v1 agent backed by Azure OpenAI (Responses API) and an MCP
server reached over `langchain-mcp-adapters`. One unified code path serves
both local development and Azure Container Apps deployments.

Heavy initialisation (Azure credential, model, MCP client + tools, agent)
happens once in the Starlette `lifespan` and is reused across requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env.local before importing anything that reads env at import time.
load_dotenv(Path(__file__).parent.parent / ".env.local")

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from streaming import iter_message_events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- Configuration ---------------------------------------------------------
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
if OPENAI_ENDPOINT and not OPENAI_ENDPOINT.endswith("/openai/v1"):
    OPENAI_ENDPOINT = f"{OPENAI_ENDPOINT}/openai/v1"

OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8000").rstrip("/")
if not MCP_SERVER_URL.endswith("/mcp"):
    MCP_SERVER_URL = f"{MCP_SERVER_URL}/mcp"

# Image generation pairs `image_generation` (server-side) with custom MCP
# tools, which historically tripped a partial_images mutation bug
# (langchain-ai/langchain#34136). We keep it behind an env flag so the
# default deploy is conservative and operators can opt in once they've
# verified their langchain stack version.
ENABLE_IMAGE_GENERATION = os.getenv("ENABLE_IMAGE_GENERATION", "false").lower() in ("1", "true", "yes")

with open(Path(__file__).parent / "instructions.txt", "r") as fh:
    SYSTEM_PROMPT = fh.read().strip()


# ---- Lifespan: build the agent once at startup -----------------------------
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
    logger.info("Initialising agent (env=%s, mcp=%s)…", ENVIRONMENT, MCP_SERVER_URL)

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")

    mcp_client = MultiServerMCPClient(
        {"zava-sales": {"url": MCP_SERVER_URL, "transport": "streamable_http"}}
    )
    mcp_tools = await _connect_mcp_with_retry(mcp_client)

    server_tools: list[dict[str, Any]] = [
        {"type": "web_search_preview"},
        {"type": "code_interpreter", "container": {"type": "auto"}},
    ]
    if ENABLE_IMAGE_GENERATION:
        server_tools.append({"type": "image_generation", "quality": "low"})
        logger.info("🎨 image_generation enabled")

    model = ChatOpenAI(
        model=OPENAI_DEPLOYMENT,
        base_url=OPENAI_ENDPOINT,
        api_key=token_provider,
        streaming=True,
        use_responses_api=True,
        include=["code_interpreter_call.outputs"],
    )

    agent = create_agent(
        model=model,
        tools=server_tools + mcp_tools,
        system_prompt=SYSTEM_PROMPT,
    )

    app.state.agent = agent
    app.state.mcp_tool_count = len(mcp_tools)
    app.state.image_generation_enabled = ENABLE_IMAGE_GENERATION
    logger.info("✅ Agent ready")

    try:
        yield
    finally:
        try:
            credential.close()
        except Exception:
            pass


# ---- Endpoints -------------------------------------------------------------
async def chat_ui_endpoint(request):
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html")


async def chat_endpoint(request):
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        return JSONResponse({"error": "Agent is not ready yet. Try again in a few seconds."}, status_code=503)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    message = body.get("message")
    history = body.get("history", []) or []
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    messages = [{"role": m["role"], "content": m["content"]} for m in history if m.get("role")]
    messages.append({"role": "user", "content": message})

    async def generate_stream():
        full_text: list[str] = []
        images: list[dict] = []
        status_active = False

        async def encode(obj: dict) -> str:
            return json.dumps(obj) + "\n"

        try:
            async for chunk in agent.astream({"messages": messages}, stream_mode="messages"):
                msg = chunk[0] if isinstance(chunk, tuple) else chunk

                for ev in iter_message_events(msg):
                    kind = ev["kind"]
                    if kind == "text":
                        if status_active:
                            yield await encode({"status": ""})
                            status_active = False
                        full_text.append(ev["text"])
                        yield await encode({"chunk": ev["text"]})
                    elif kind == "image":
                        if status_active:
                            yield await encode({"status": ""})
                            status_active = False
                        images.append(ev["image"])
                        yield await encode({"image": ev["image"]})
                    elif kind == "status_start":
                        if not status_active:
                            yield await encode({"status": ev["status"]})
                            status_active = True
                    elif kind == "status_end":
                        if status_active:
                            yield await encode({"status": ""})
                            status_active = False
        except Exception as exc:
            logger.exception("Error during agent stream")
            yield await encode({"error": f"agent stream failed: {exc}"})

        if status_active:
            yield await encode({"status": ""})
        yield await encode(
            {
                "message": "".join(full_text),
                "role": "assistant",
                "images": images,
                "done": True,
            }
        )

    return StreamingResponse(generate_stream(), media_type="application/json")


async def health_endpoint(request):
    state = request.app.state
    ready = hasattr(state, "agent") and state.agent is not None
    return JSONResponse(
        {
            "status": "healthy" if ready else "starting",
            "ready": ready,
            "environment": ENVIRONMENT,
            "openai_endpoint": OPENAI_ENDPOINT,
            "mcp_server": MCP_SERVER_URL,
            "mcp_tool_count": getattr(state, "mcp_tool_count", 0),
            "image_generation_enabled": getattr(state, "image_generation_enabled", False),
        },
        status_code=200 if ready else 503,
    )


# ---- App -------------------------------------------------------------------
routes = [
    Route("/", chat_ui_endpoint, methods=["GET"]),
    Route("/api/chat", chat_endpoint, methods=["POST"]),
    Route("/api/health", health_endpoint, methods=["GET"]),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
