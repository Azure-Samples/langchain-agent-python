"""
Agent API ASGI Application
LangChain agent with MCP tool support, using Azure OpenAI
Runs with uvicorn on Azure Container Apps
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool

# Load environment variables from .env.local (for local development)
env_path = Path(__file__).parent.parent / ".env.local"
load_dotenv(env_path)

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Determine environment
environment = os.getenv("ENVIRONMENT", "production")
is_local = environment == "local"

# Load system instructions once at module level
system_instructions_path = Path(__file__).parent / "instructions.txt"
with open(system_instructions_path, "r") as f:
    system_prompt = f.read().strip()

# Configuration from environment
openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
if openai_endpoint and not openai_endpoint.endswith("/openai/v1"):
    openai_endpoint = f"{openai_endpoint}/openai/v1"

openai_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

mcp_server_url = os.getenv("MCP_SERVER_URL", "http://localhost:8000").rstrip("/")
if not mcp_server_url.endswith("/mcp"):
    mcp_server_url = f"{mcp_server_url}/mcp"

# For local mode, we'll load MCP tools via langchain-mcp-adapters
mcp_tools = []
mcp_client = None

if is_local:
    logger.info("🔧 Running in LOCAL mode - using langchain-mcp-adapters for MCP tools")
    from langchain_mcp_adapters.client import MultiServerMCPClient

    # Create MCP client for local server
    mcp_client = MultiServerMCPClient(
        {
            "zava-sales": {
                "url": mcp_server_url,
                "transport": "streamable_http",
            }
        }
    )
else:
    logger.info(
        "🚀 Running in PRODUCTION mode - using Azure OpenAI Responses API for MCP"
    )


async def chat_ui_endpoint(request):
    """Serve the chat UI."""
    try:
        html_path = Path(__file__).parent / "static" / "index.html"
        return FileResponse(html_path, media_type="text/html")
    except Exception as e:
        logger.error(f"Error loading chat UI: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Error loading chat UI: {str(e)}"}, status_code=500
        )


async def chat_endpoint(request):
    """
    Chat endpoint for the agent with streaming support.

    Request body:
    {
        "message": "user message",
        "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    }
    """
    try:
        # Parse request body
        req_body = await request.json()
        message = req_body.get("message")
        history = req_body.get("history", [])

        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        # Initialize Azure credential and token provider
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )

        # Build tools list - differs between local and production
        if is_local:
            # LOCAL MODE: Use a separate image agent to avoid the partial_images
            # mutation bug when combining image_generation with custom tools.
            # See: https://github.com/langchain-ai/langchain/pull/34136

            # Create a dedicated image generation agent (no custom tools = no bug)
            image_model = ChatOpenAI(
                model=openai_deployment,
                base_url=openai_endpoint,
                api_key=token_provider,
                temperature=0.7,
                streaming=True,
                use_responses_api=True,
            )
            image_agent = create_agent(
                model=image_model,
                tools=[{"type": "image_generation", "quality": "low"}],
                system_prompt="You are an image generation assistant. Generate images based on the user's description. Be creative and descriptive.",
            )

            # Wrap the image agent as a tool for the main agent
            @tool
            async def generate_image(description: str) -> dict:
                """Generate an image based on a text description. Use this when the user asks you to create, draw, or generate an image. Returns a dictionary with image data."""
                result = await image_agent.ainvoke(
                    {"messages": [{"role": "user", "content": description}]}
                )
                # Extract image data from the response
                last_message = result["messages"][-1]
                content = last_message.content

                # Parse the content to find image blocks
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image":
                            return {
                                "type": "image",
                                "base64": block.get("base64", ""),
                                "format": block.get("format", "png"),
                            }
                        elif hasattr(block, "type") and block.type == "image":
                            return {
                                "type": "image",
                                "base64": getattr(block, "base64", ""),
                                "format": getattr(block, "format", "png"),
                            }

                # If no image block found, return the text content
                return {"type": "text", "content": str(content)}

            # Responses API tools (without image_generation - it's now a subagent)
            responses_api_tools = [
                {"type": "web_search_preview"},
                {"type": "code_interpreter", "container": {"type": "auto"}},
            ]
            # Get MCP tools via langchain-mcp-adapters
            mcp_tools = await mcp_client.get_tools()
            logger.info(f"📦 Loaded {len(mcp_tools)} MCP tools for local mode")
            # Combine: Responses API tools + image tool (subagent) + MCP tools
            all_tools = responses_api_tools + [generate_image] + mcp_tools
        else:
            # PRODUCTION MODE: All tools work via Azure OpenAI remote handling
            all_tools = [
                {
                    "type": "mcp",
                    "server_label": "zava-sales",
                    "server_url": mcp_server_url,
                    "require_approval": "never",
                },
                {"type": "web_search_preview"},
                {"type": "image_generation", "quality": "low"},
                {"type": "code_interpreter", "container": {"type": "auto"}},
            ]

        # Create model with all tools bound
        model = ChatOpenAI(
            model=openai_deployment,
            base_url=openai_endpoint,
            api_key=token_provider,
            temperature=0.7,
            streaming=True,
            use_responses_api=True,
            include=["code_interpreter_call.outputs"],
        )

        # Create agent with the same tools
        agent = create_agent(model=model, tools=all_tools, system_prompt=system_prompt)

        # Build messages for agent
        messages = []

        # Add history
        if history:
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})

        # Add current message
        messages.append({"role": "user", "content": message})

        # Helper function to get appropriate status message based on tool name
        def get_tool_status(tool_names: list) -> str:
            """Return appropriate status message based on the tool being called."""
            for name in tool_names:
                name_lower = name.lower() if name else ""
                # Check for our MCP tools first (more specific matches)
                if "semantic_search" in name_lower:
                    return "🔍 Searching products..."
                elif (
                    "execute_sales_query" in name_lower
                    or "get_table_schemas" in name_lower
                ):
                    return "🔍 Querying database..."
                elif "get_current_utc_date" in name_lower:
                    return "⏰ Getting current time..."
                # Then check for built-in tools
                elif "image" in name_lower or "generate_image" in name_lower:
                    return "🎨 Generating image..."
                elif "web_search" in name_lower:
                    return "🔎 Searching the web..."
                elif "code_interpreter" in name_lower or "code" in name_lower:
                    return "💻 Running code..."
                elif any(
                    db_term in name_lower
                    for db_term in [
                        "query",
                        "sql",
                        "database",
                        "db",
                        "sales",
                        "customer",
                        "order",
                        "product",
                    ]
                ):
                    return "🔍 Querying database..."
            # Default status for unknown tools
            return f"⚙️ Using {tool_names[0] if tool_names else 'tool'}..."

        # Async generator for true streaming
        async def generate_stream():
            """Stream chunks as they arrive from the agent."""
            full_response = ""
            images = []
            tool_in_progress = False

            # Stream with stream_mode="messages" to get token-by-token output
            async for chunk in agent.astream(
                {"messages": messages}, stream_mode="messages"
            ):
                # Handle different chunk formats
                if isinstance(chunk, tuple):
                    token, metadata = chunk
                else:
                    token = chunk

                # Skip tool calls and tool results - only show AI responses
                # Check message type
                msg_type = getattr(token, "type", None)
                if msg_type in ("tool", "function"):
                    # This is a tool result - check for images from code_interpreter
                    tool_content = getattr(token, "content", "")
                    if tool_content and isinstance(tool_content, str):
                        # Check for base64 image data in tool output
                        if "base64" in tool_content and (
                            "image" in tool_content or "png" in tool_content
                        ):
                            try:
                                tool_data = json.loads(tool_content)
                                if (
                                    isinstance(tool_data, dict)
                                    and tool_data.get("type") == "image"
                                ):
                                    image_data = {
                                        "base64": tool_data.get("base64", ""),
                                        "format": tool_data.get("format", "png"),
                                    }
                                    images.append(image_data)
                                    yield json.dumps({"image": image_data}) + "\n"
                            except json.JSONDecodeError:
                                pass
                    continue

                # Check if this is a tool call message
                if hasattr(token, "tool_calls") and token.tool_calls:
                    # AI is calling a tool - send appropriate status update
                    if not tool_in_progress:
                        tool_in_progress = True
                        tool_names = [
                            tc.get("name", "tool")
                            if isinstance(tc, dict)
                            else getattr(tc, "name", "tool")
                            for tc in token.tool_calls
                        ]
                        status_msg = get_tool_status(tool_names)
                        yield json.dumps({"status": status_msg}) + "\n"
                    continue

                # Check for additional_kwargs with tool_calls
                if hasattr(token, "additional_kwargs"):
                    if token.additional_kwargs.get("tool_calls"):
                        if not tool_in_progress:
                            tool_in_progress = True
                            # Extract tool names from additional_kwargs
                            tool_calls = token.additional_kwargs.get("tool_calls", [])
                            tool_names = [
                                tc.get("function", {}).get("name", "tool")
                                if isinstance(tc, dict)
                                else getattr(tc, "name", "tool")
                                for tc in tool_calls
                            ]
                            status_msg = get_tool_status(tool_names)
                            yield json.dumps({"status": status_msg}) + "\n"
                        continue

                # Reset tool status when we get actual content
                if tool_in_progress:
                    tool_in_progress = False
                    yield json.dumps({"status": ""}) + "\n"  # Clear status

                # Only process AI messages with actual content
                if hasattr(token, "content"):
                    content = token.content

                    # Skip empty content
                    if not content:
                        continue

                    # Check response_metadata for code_interpreter outputs (contains images)
                    if hasattr(token, "response_metadata"):
                        resp_meta = token.response_metadata
                        if isinstance(resp_meta, dict):
                            # Check for code_interpreter output in response
                            outputs = resp_meta.get("code_interpreter_call", {}).get(
                                "outputs", []
                            )
                            if not outputs:
                                outputs = resp_meta.get("outputs", [])
                            for output in outputs:
                                if isinstance(output, dict):
                                    # Handle file outputs (like PNG images)
                                    if output.get("type") == "files":
                                        for file_info in output.get("files", []):
                                            if "image" in file_info.get(
                                                "mime_type", ""
                                            ):
                                                # The file content is base64 encoded
                                                image_data = {
                                                    "base64": file_info.get(
                                                        "file_data", ""
                                                    ),
                                                    "format": file_info.get(
                                                        "mime_type", "image/png"
                                                    ).split("/")[-1],
                                                }
                                                images.append(image_data)
                                                yield (
                                                    json.dumps({"image": image_data})
                                                    + "\n"
                                                )
                                    # Handle image type outputs directly
                                    elif output.get("type") == "image":
                                        image_data = {
                                            "base64": output.get(
                                                "base64", output.get("data", "")
                                            ),
                                            "format": output.get("format", "png"),
                                        }
                                        images.append(image_data)
                                        yield json.dumps({"image": image_data}) + "\n"

                    # Handle content_blocks (LangChain Responses API format)
                    # See: https://docs.langchain.com/oss/python/langchain/messages#message-content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                block_type = block.get("type")

                                # Text content block
                                if block_type == "text":
                                    text = block.get("text", "")
                                    if text:
                                        full_response += text
                                        yield json.dumps({"chunk": text}) + "\n"

                                # Reasoning block (from reasoning models)
                                elif block_type == "reasoning":
                                    # Skip reasoning blocks - don't show to user
                                    pass

                                # Server tool call (web_search, file_search, etc.)
                                elif block_type == "server_tool_call":
                                    tool_name = block.get("name", "tool")
                                    if not tool_in_progress:
                                        tool_in_progress = True
                                        status_msg = get_tool_status([tool_name])
                                        yield json.dumps({"status": status_msg}) + "\n"

                                # Server tool result
                                elif block_type == "server_tool_result":
                                    # Tool completed, reset status
                                    if tool_in_progress:
                                        tool_in_progress = False
                                        yield json.dumps({"status": ""}) + "\n"

                                # Code interpreter call with outputs
                                elif block_type == "code_interpreter_call":
                                    outputs = block.get("outputs", [])

                                    # Show status when code interpreter is seen (before outputs)
                                    # Only show if we haven't shown status yet AND no outputs yet
                                    if not tool_in_progress and not outputs:
                                        tool_in_progress = True
                                        yield (
                                            json.dumps({"status": "💻 Running code..."})
                                            + "\n"
                                        )

                                    # If we have outputs, clear status first then process
                                    if outputs:
                                        if tool_in_progress:
                                            tool_in_progress = False
                                            yield json.dumps({"status": ""}) + "\n"
                                    for output in outputs:
                                        if isinstance(output, dict):
                                            if output.get("type") == "image":
                                                # Image can be in 'url' as data URI or direct 'base64'
                                                url = output.get("url", "")
                                                if url.startswith("data:image/"):
                                                    # Parse data URI: data:image/png;base64,XXXX
                                                    parts = url.split(",", 1)
                                                    if len(parts) == 2:
                                                        # Extract format from mime type
                                                        mime_part = parts[0]
                                                        b64_data = parts[1]
                                                        img_format = "png"
                                                        if "image/" in mime_part:
                                                            img_format = (
                                                                mime_part.split(
                                                                    "image/"
                                                                )[1].split(";")[0]
                                                            )
                                                        image_data = {
                                                            "base64": b64_data,
                                                            "format": img_format,
                                                        }
                                                        images.append(image_data)
                                                        yield (
                                                            json.dumps(
                                                                {"image": image_data}
                                                            )
                                                            + "\n"
                                                        )
                                                else:
                                                    # Direct base64
                                                    b64 = output.get(
                                                        "base64", output.get("data", "")
                                                    )
                                                    if b64:
                                                        image_data = {
                                                            "base64": b64,
                                                            "format": output.get(
                                                                "format", "png"
                                                            ),
                                                        }
                                                        images.append(image_data)
                                                        yield (
                                                            json.dumps(
                                                                {"image": image_data}
                                                            )
                                                            + "\n"
                                                        )

                                # Direct image block (from image_generation)
                                elif block_type == "image":
                                    url = block.get("url", "")
                                    if url.startswith("data:image/"):
                                        parts = url.split(",", 1)
                                        if len(parts) == 2:
                                            mime_part = parts[0]
                                            b64_data = parts[1]
                                            img_format = "png"
                                            if "image/" in mime_part:
                                                img_format = mime_part.split("image/")[
                                                    1
                                                ].split(";")[0]
                                            image_data = {
                                                "base64": b64_data,
                                                "format": img_format,
                                            }
                                            images.append(image_data)
                                            yield (
                                                json.dumps({"image": image_data}) + "\n"
                                            )
                                    else:
                                        b64 = block.get("base64", "")
                                        if b64:
                                            image_data = {
                                                "base64": b64,
                                                "format": block.get("format", "png"),
                                            }
                                            images.append(image_data)
                                            yield (
                                                json.dumps({"image": image_data}) + "\n"
                                            )

                            # Handle object-style blocks (older format)
                            elif hasattr(block, "text"):
                                text = block.text
                                if text:
                                    full_response += text
                                    yield json.dumps({"chunk": text}) + "\n"
                            elif (
                                hasattr(block, "type")
                                and getattr(block, "type", None) == "image"
                            ):
                                image_data = {
                                    "base64": getattr(
                                        block, "base64", getattr(block, "data", "")
                                    ),
                                    "format": getattr(block, "format", "png"),
                                }
                                if image_data["base64"]:
                                    images.append(image_data)
                                    yield json.dumps({"image": image_data}) + "\n"
                    elif isinstance(content, str) and content:
                        # Check if content contains image data from tool result
                        if content.startswith("{") and '"type": "image"' in content:
                            try:
                                img_data = json.loads(content)
                                if img_data.get("type") == "image":
                                    image_data = {
                                        "base64": img_data.get("base64", ""),
                                        "format": img_data.get("format", "png"),
                                    }
                                    images.append(image_data)
                                    yield json.dumps({"image": image_data}) + "\n"
                                    continue
                            except json.JSONDecodeError:
                                pass
                        full_response += content
                        yield json.dumps({"chunk": content}) + "\n"

            # Send final complete message
            yield (
                json.dumps(
                    {
                        "message": full_response,
                        "role": "assistant",
                        "images": images,
                        "done": True,
                    }
                )
                + "\n"
            )

        return StreamingResponse(generate_stream(), media_type="application/json")

    except ValueError as e:
        logger.error(f"ValueError in chat endpoint: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Internal server error: {str(e)}"}, status_code=500
        )


async def health_endpoint(request):
    """Health check endpoint."""
    try:
        return JSONResponse(
            {
                "status": "healthy",
                "environment": environment,
                "openai_endpoint": openai_endpoint,
                "mcp_server": mcp_server_url,
            }
        )
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=503)


# Define routes
routes = [
    Route("/", chat_ui_endpoint, methods=["GET"]),
    Route("/api/chat", chat_endpoint, methods=["POST"]),
    Route("/api/health", health_endpoint, methods=["GET"]),
]

# Create Starlette app
app = Starlette(debug=False, routes=routes)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
