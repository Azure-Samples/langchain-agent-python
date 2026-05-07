"""Step configuration middleware (Handoffs pattern).

For each `current_step` we declare:
  - `prompt`: which prompt file's contents to inject as the system message.
  - `tools`: which tool names the model is allowed to see in this step.

Implemented as a `@wrap_model_call` middleware that intercepts each model
invocation and rewrites the request based on `state["current_step"]`.

Reference: https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs-customer-support
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.messages import SystemMessage

from app.state import DEFAULT_STEP, Step, SalesState

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text().strip()


# Step → which tool names the model sees.
#
# Tool names cover BOTH local @tool definitions in `app.tools` AND tools
# served by the MCP server (loaded at runtime). Tools whose names aren't in
# the allowed set for a step are filtered out before the LLM call.
STEP_CONFIG: dict[Step, dict] = {
    "greet": {
        "prompt": _load_prompt("greet"),
        "tools": {
            # Local
            "set_intent",
            "lookup_lead_by_email",
            "escalate_to_ae",
            # MCP
            "search_case_studies",
        },
    },
    "qualify": {
        "prompt": _load_prompt("qualify"),
        "tools": {
            # Local
            "update_lead_profile",
            "back_to_greet",
            "escalate_to_ae",
            "advance_to_step",
        },
    },
    "educate": {
        "prompt": _load_prompt("educate"),
        "tools": {
            # Local
            "back_to_greet",
            "advance_to_step",
            # MCP
            "semantic_search_products",
            "search_case_studies",
            "search_kb_articles",
            "get_pricing",
        },
    },
    "objection": {
        "prompt": _load_prompt("objection"),
        "tools": {
            # Local
            "log_objection",
            "back_to_greet",
            "escalate_to_ae",
            "advance_to_step",
            # MCP
            "search_case_studies",
            "search_kb_articles",
            "get_pricing",
            "compare_plans",
        },
    },
    "book": {
        "prompt": _load_prompt("book"),
        "tools": {
            # Local
            "propose_meeting_times",
            "back_to_greet",
            "advance_to_step",
            "escalate_to_ae",
        },
    },
    "handoff_to_ae": {
        "prompt": _load_prompt("handoff_to_ae"),
        "tools": {
            # Local
            "create_handoff_summary",
            "log_activity",
        },
    },
}


# Style rules appended to every step prompt so the assistant's voice is
# consistent regardless of which step is active.
GLOBAL_STYLE_RULES = """

## Style rules (apply to every reply)
- **Never use em dashes (—) or en dashes (–).** Use a comma, full stop, parentheses, or a colon instead. This applies to every sentence you write, including casual asides.
- Prefer short sentences over long ones joined by dashes.
"""


@wrap_model_call
async def apply_step_config(request: ModelRequest, handler):
    """Inject the step-specific system prompt and filter the tool list."""
    state: SalesState = request.state  # type: ignore[assignment]
    step: Step = state.get("current_step") or DEFAULT_STEP  # type: ignore[assignment]
    config = STEP_CONFIG.get(step, STEP_CONFIG["greet"])

    # Inject system prompt at the head of the messages list (replace any prior).
    system_msg = SystemMessage(content=config["prompt"] + GLOBAL_STYLE_RULES)
    msgs = [m for m in request.messages if not (hasattr(m, "type") and m.type == "system")]
    request.messages = [system_msg, *msgs]

    # Filter tools: keep only the ones allowed for this step.
    allowed = config["tools"]
    request.tools = [t for t in request.tools if getattr(t, "name", None) in allowed]
    return await handler(request)
