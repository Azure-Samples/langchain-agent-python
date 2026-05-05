"""
Helpers for converting a streamed LangChain agent message into the NDJSON
event stream used by the chat UI.

The chat API emits one JSON object per line. Event kinds:

    {"chunk": "text"}                       # each token of assistant text
    {"status": "..."}                       # tool announcement (empty = clear)
    {"image": {"base64": "...", "format": "png"}}
    {"message": "...", "role": "assistant", "images": [...], "done": true}

`iter_message_events` is a pure function over a single AIMessageChunk-like
object. It yields zero or more event dicts. The caller is responsible for
serialising them and managing the running `full_text` and `images` lists.

Keeping this module free of network I/O makes it cheap to unit-test against
hand-built chunks that mimic real LangChain v1 streaming shapes.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


def tool_status_for(tool_names: list[str]) -> str:
    """Map a list of tool names to a friendly status string."""
    for name in tool_names:
        n = (name or "").lower()
        if "semantic_search" in n:
            return "🔍 Searching products..."
        if "execute_sales_query" in n or "get_table_schemas" in n:
            return "🔍 Querying database..."
        if "get_current_utc_date" in n:
            return "⏰ Getting current time..."
        if "image" in n or "generate_image" in n:
            return "🎨 Generating image..."
        if "web_search" in n:
            return "🔎 Searching the web..."
        if "code_interpreter" in n or "code" in n:
            return "💻 Running code..."
        if any(t in n for t in ("query", "sql", "database", "db", "sales", "customer", "order", "product")):
            return "🔍 Querying database..."
    return f"⚙️ Using {tool_names[0] if tool_names else 'tool'}..."


def _extract_image(block: dict) -> dict | None:
    """Pull a {base64, format} image dict out of a content block in any of the
    shapes produced by LangChain / OpenAI Responses API."""
    url = block.get("url", "") or ""
    if isinstance(url, str) and url.startswith("data:image/"):
        try:
            mime, data = url.split(",", 1)
        except ValueError:
            return None
        fmt = "png"
        if "image/" in mime:
            fmt = mime.split("image/", 1)[1].split(";", 1)[0] or "png"
        return {"base64": data, "format": fmt}

    b64 = block.get("base64") or block.get("data") or block.get("file_data")
    if b64:
        fmt = block.get("format") or block.get("mime_type", "image/png").split("/")[-1]
        return {"base64": b64, "format": fmt or "png"}
    return None


def _iter_blocks(blocks: Any) -> Iterable[dict]:
    """Yield events for an iterable of content blocks (dict or object)."""
    if not isinstance(blocks, list):
        return
    for b in blocks:
        if isinstance(b, dict):
            t = b.get("type")
            if t == "text":
                text = b.get("text") or ""
                if text:
                    yield {"kind": "text", "text": text}
            elif t == "image":
                img = _extract_image(b)
                if img:
                    yield {"kind": "image", "image": img}
            elif t == "server_tool_call":
                yield {"kind": "status_start", "status": tool_status_for([b.get("name", "")])}
            elif t == "server_tool_result":
                yield {"kind": "status_end"}
            elif t == "code_interpreter_call":
                outputs = b.get("outputs") or []
                if not outputs:
                    yield {"kind": "status_start", "status": "💻 Running code..."}
                    continue
                yield {"kind": "status_end"}
                for o in outputs:
                    if isinstance(o, dict) and o.get("type") == "image":
                        img = _extract_image(o)
                        if img:
                            yield {"kind": "image", "image": img}
                    elif isinstance(o, dict) and o.get("type") == "files":
                        for f in o.get("files", []):
                            if isinstance(f, dict) and "image" in (f.get("mime_type") or ""):
                                img = _extract_image(f)
                                if img:
                                    yield {"kind": "image", "image": img}
            # reasoning/tool_use/etc → ignored on purpose
        else:
            text = getattr(b, "text", None)
            if text:
                yield {"kind": "text", "text": text}
            elif getattr(b, "type", None) == "image":
                img = {
                    "base64": getattr(b, "base64", "") or getattr(b, "data", ""),
                    "format": getattr(b, "format", "png"),
                }
                if img["base64"]:
                    yield {"kind": "image", "image": img}


def _tool_names_from_chunk(msg: Any) -> list[str]:
    """Extract tool names from `tool_calls` (langchain) or `additional_kwargs` (raw OpenAI)."""
    names: list[str] = []
    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        if isinstance(tc, dict):
            n = tc.get("name") or tc.get("function", {}).get("name")
        else:
            n = getattr(tc, "name", None)
        if n:
            names.append(n)
    if names:
        return names
    extras = getattr(msg, "additional_kwargs", None) or {}
    for tc in extras.get("tool_calls", []) or []:
        if isinstance(tc, dict):
            n = tc.get("name") or tc.get("function", {}).get("name")
            if n:
                names.append(n)
    return names


def _maybe_image_from_tool_string(content: str) -> dict | None:
    """Some custom MCP tools return a JSON-stringified image dict."""
    if not isinstance(content, str) or '"type"' not in content[:100] or "image" not in content[:100]:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("type") == "image":
        b64 = data.get("base64") or data.get("data")
        if b64:
            return {"base64": b64, "format": data.get("format", "png")}
    return None


def iter_message_events(msg: Any) -> Iterable[dict]:
    """Yield event dicts for one streamed message chunk.

    Event kinds:
      {"kind": "text",         "text": str}
      {"kind": "image",        "image": {...}}
      {"kind": "status_start", "status": str}
      {"kind": "status_end"}            # caller emits {"status": ""}
    """
    msg_type = getattr(msg, "type", None)

    # Tool/function results: only interesting when a custom MCP tool returns
    # a JSON-encoded image dict. Otherwise we ignore them - the AI message's
    # content_blocks already reflect what the user should see.
    if msg_type in ("tool", "function"):
        content = getattr(msg, "content", "") or ""
        img = _maybe_image_from_tool_string(content) if isinstance(content, str) else None
        if img:
            yield {"kind": "image", "image": img}
        return

    # AI requesting tools - announce status (layered: tool_calls then additional_kwargs).
    tool_names = _tool_names_from_chunk(msg)
    if tool_names:
        yield {"kind": "status_start", "status": tool_status_for(tool_names)}
        return

    # response_metadata may carry code_interpreter outputs out-of-band.
    rmeta = getattr(msg, "response_metadata", None)
    if isinstance(rmeta, dict):
        outputs = rmeta.get("code_interpreter_call", {}).get("outputs") or rmeta.get("outputs") or []
        for o in outputs:
            if isinstance(o, dict):
                if o.get("type") == "files":
                    for f in o.get("files", []):
                        if isinstance(f, dict) and "image" in (f.get("mime_type") or ""):
                            img = _extract_image(f)
                            if img:
                                yield {"kind": "image", "image": img}
                elif o.get("type") == "image":
                    img = _extract_image(o)
                    if img:
                        yield {"kind": "image", "image": img}

    # Standard LC v1 normalized blocks (preferred path).
    blocks = getattr(msg, "content_blocks", None)
    if blocks:
        yield from _iter_blocks(blocks)
        return

    # Fallback: raw content (may be a list of provider blocks or a plain string).
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        yield from _iter_blocks(content)
    elif isinstance(content, str) and content:
        img = _maybe_image_from_tool_string(content)
        if img:
            yield {"kind": "image", "image": img}
        else:
            yield {"kind": "text", "text": content}
