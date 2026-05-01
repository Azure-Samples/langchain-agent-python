"""Contract tests for `streaming.iter_message_events`.

These tests build hand-crafted message chunks that mimic the shapes a
LangChain v1 / Azure OpenAI Responses API agent produces during streaming,
then assert the public NDJSON event contract is preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from streaming import iter_message_events, tool_status_for


def chunk(**kwargs):
    """Build a SimpleNamespace pretending to be an AIMessageChunk."""
    defaults = {"type": "ai", "content": "", "tool_calls": []}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------- text streaming ---------------------------------------------------
def test_text_via_content_blocks():
    msg = chunk(content_blocks=[{"type": "text", "text": "Hel"}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "text", "text": "Hel"}]


def test_text_via_raw_string_content():
    msg = chunk(content="Hello")
    events = list(iter_message_events(msg))
    assert events == [{"kind": "text", "text": "Hello"}]


def test_text_via_raw_list_content_when_no_content_blocks():
    msg = chunk(content=[{"type": "text", "text": "world"}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "text", "text": "world"}]


def test_empty_text_block_skipped():
    msg = chunk(content_blocks=[{"type": "text", "text": ""}])
    assert list(iter_message_events(msg)) == []


def test_reasoning_block_is_ignored():
    msg = chunk(content_blocks=[{"type": "reasoning", "text": "thinking..."}])
    assert list(iter_message_events(msg)) == []


# ---------- tool calls -------------------------------------------------------
def test_tool_call_emits_status_start():
    msg = chunk(tool_calls=[{"name": "semantic_search_products", "args": {}}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "status_start", "status": "🔍 Searching products..."}]


def test_tool_call_via_additional_kwargs():
    msg = chunk(additional_kwargs={"tool_calls": [{"function": {"name": "execute_sales_query"}}]})
    events = list(iter_message_events(msg))
    assert events == [{"kind": "status_start", "status": "🔍 Querying database..."}]


def test_server_tool_call_block():
    msg = chunk(content_blocks=[{"type": "server_tool_call", "name": "web_search"}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "status_start", "status": "🔎 Searching the web..."}]


def test_server_tool_result_block():
    msg = chunk(content_blocks=[{"type": "server_tool_result"}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "status_end"}]


# ---------- code interpreter -------------------------------------------------
def test_code_interpreter_call_no_outputs_announces_status():
    msg = chunk(content_blocks=[{"type": "code_interpreter_call", "outputs": []}])
    assert list(iter_message_events(msg)) == [
        {"kind": "status_start", "status": "💻 Running code..."},
    ]


def test_code_interpreter_with_image_data_url():
    msg = chunk(
        content_blocks=[
            {
                "type": "code_interpreter_call",
                "outputs": [{"type": "image", "url": "data:image/png;base64,AAA"}],
            }
        ]
    )
    events = list(iter_message_events(msg))
    assert events == [
        {"kind": "status_end"},
        {"kind": "image", "image": {"base64": "AAA", "format": "png"}},
    ]


def test_response_metadata_code_interpreter_files():
    msg = chunk(
        response_metadata={
            "code_interpreter_call": {
                "outputs": [
                    {"type": "files", "files": [{"mime_type": "image/png", "file_data": "ZZZ"}]}
                ]
            }
        }
    )
    events = list(iter_message_events(msg))
    assert events == [{"kind": "image", "image": {"base64": "ZZZ", "format": "png"}}]


# ---------- direct image blocks ---------------------------------------------
def test_direct_image_block_data_url():
    msg = chunk(content_blocks=[{"type": "image", "url": "data:image/jpeg;base64,JJJ"}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "image", "image": {"base64": "JJJ", "format": "jpeg"}}]


def test_direct_image_block_raw_base64():
    msg = chunk(content_blocks=[{"type": "image", "base64": "BBB", "format": "png"}])
    events = list(iter_message_events(msg))
    assert events == [{"kind": "image", "image": {"base64": "BBB", "format": "png"}}]


# ---------- tool messages with image payload --------------------------------
def test_tool_message_with_json_image_string():
    msg = chunk(
        type="tool",
        content='{"type": "image", "base64": "TTT", "format": "png"}',
    )
    events = list(iter_message_events(msg))
    assert events == [{"kind": "image", "image": {"base64": "TTT", "format": "png"}}]


def test_tool_message_text_is_ignored():
    msg = chunk(type="tool", content="some plain text result")
    assert list(iter_message_events(msg)) == []


# ---------- tool_status_for --------------------------------------------------
def test_tool_status_known_names():
    assert tool_status_for(["semantic_search_products"]).startswith("🔍")
    assert tool_status_for(["execute_sales_query"]).startswith("🔍")
    assert tool_status_for(["web_search_preview"]).startswith("🔎")
    assert tool_status_for(["code_interpreter"]).startswith("💻")
    assert tool_status_for(["image_generation"]).startswith("🎨")
    assert tool_status_for(["mystery_tool"]).startswith("⚙️")
    assert tool_status_for([]).startswith("⚙️")
