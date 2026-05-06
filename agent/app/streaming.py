"""NDJSON event emitter and per-message-id stream dedupe helpers.

Event kinds (one JSON object per line emitted by `app.main.chat`):
    {"thread_id":  "..."}              # always first
    {"chunk":      "text"}             # token of assistant text
    {"step":       "qualify"}          # current funnel step (frosted pill in UI)
    {"tool":       {"name": "...",
                    "args_preview": "..."}}
    {"citation":   {"doc_id": "...",
                    "title":  "..."}}
    {"image":      {...}}
    {"done": true, "message": "...", "step": "..."}
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from langchain_core.messages import AIMessage, AIMessageChunk

_DOC_TAG = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")


def event(obj: dict) -> str:
    """Serialise a single event to an NDJSON line."""
    return json.dumps(obj, default=str) + "\n"


def _tool_names_from_chunk(msg: Any) -> list[str]:
    names: list[str] = []
    for tc in (getattr(msg, "tool_calls", None) or []):
        n = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        if n:
            names.append(n)
    return names


def iter_message_events(msg: Any) -> Iterable[dict]:
    """Yield UI events for one streamed message chunk.

    Kinds returned (caller serialises them):
      {"kind": "text",      "text": str}
      {"kind": "tool",      "tool": {"name": ..., "args_preview": ...}}
      {"kind": "citations", "doc_ids": [str, ...]}
      {"kind": "image",     "image": {...}}
    """
    msg_type = getattr(msg, "type", None)
    if msg_type in ("tool", "function"):
        # Tool result — surface citations if present in the content text.
        content = getattr(msg, "content", "") or ""
        if isinstance(content, str):
            doc_ids = _DOC_TAG.findall(content)
            if doc_ids:
                yield {"kind": "citations", "doc_ids": doc_ids}
        return

    tool_names = _tool_names_from_chunk(msg)
    if tool_names:
        first_call = (getattr(msg, "tool_calls", None) or [{}])[0]
        args = first_call.get("args") if isinstance(first_call, dict) else None
        args_preview = ""
        if args:
            try:
                args_preview = json.dumps(args)[:120]
            except Exception:
                args_preview = str(args)[:120]
        yield {"kind": "tool", "tool": {"name": tool_names[0], "args_preview": args_preview}}
        return

    # Only stream text from streaming chunks (AIMessageChunk). The final
    # aggregated AIMessage that LangGraph emits at the end has the same
    # content as the concatenated chunks; emitting both doubles the reply.
    if isinstance(msg, AIMessage) and not isinstance(msg, AIMessageChunk):
        return

    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                # image_generation_call output (Azure OpenAI Responses API)
                if btype == "image_generation_call":
                    image_url = block.get("result") or block.get("url")
                    if image_url:
                        yield {"kind": "image", "image": {"url": image_url}}
                    continue
                text = block.get("text") or block.get("delta") or ""
                if text and ("text" in btype or btype == "" or btype == "output_text"):
                    yield {"kind": "text", "text": text}
            elif isinstance(block, str) and block:
                yield {"kind": "text", "text": block}
    elif isinstance(content, str) and content:
        yield {"kind": "text", "text": content}
