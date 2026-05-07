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
# Captures: **[doc-id]** title-line  (followed by an indented summary line)
_DOC_BLOCK = re.compile(
    r"\*\*\[([a-zA-Z0-9_\-]+)\]\*\*\s*([^\n]*)(?:\n\s{2,}([^\n]+))?",
    re.MULTILINE,
)


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
        # Content may be a string or a list of content blocks (Azure/Anthropic
        # tool-result format). Normalise to a single string before regexing.
        raw = getattr(msg, "content", "") or ""
        if isinstance(raw, list):
            parts = []
            for block in raw:
                if isinstance(block, dict):
                    parts.append(
                        block.get("text")
                        or block.get("content")
                        or (block.get("output") if isinstance(block.get("output"), str) else "")
                        or ""
                    )
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(p for p in parts if p)
        else:
            content = raw if isinstance(raw, str) else str(raw)
        if content:
            # First, try the rich block pattern (search_* tools format their
            # results as `- **[doc-id]** title\n  snippet`). For each match,
            # emit a structured citation with title + snippet so the UI can
            # render a hover card.
            seen: set[str] = set()
            for m in _DOC_BLOCK.finditer(content):
                doc_id = m.group(1)
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                title = (m.group(2) or "").strip().strip("—").strip()
                snippet = (m.group(3) or "").strip()
                yield {
                    "kind": "citation",
                    "citation": {
                        "doc_id": doc_id,
                        "title": title,
                        "snippet": snippet,
                    },
                }
            # Fallback for tool results that don't use the bold-bracket
            # convention (e.g. get_pricing returns JSON with `doc_id`).
            for doc_id in _DOC_TAG.findall(content):
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                yield {
                    "kind": "citation",
                    "citation": {"doc_id": doc_id, "title": "", "snippet": ""},
                }
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
