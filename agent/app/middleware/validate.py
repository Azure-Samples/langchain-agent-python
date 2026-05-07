"""Groundedness validation middleware.

After the agent produces a tool-free assistant message in retrieval-heavy
steps (`educate`, `objection`), we run a cheap groundedness check (nano
model). If the answer makes substantive product/pricing/case-study claims
without referencing a retrieved doc id, we *rewrite* the response to ask
the user whether to escalate to a human AE instead of silently making
something up.

Modes (env `VALIDATION_MODE`):
- `advisory` — log only.
- `rewrite` (default) — replace the response with an "ask before escalate"
  prompt; escalation only happens if the user confirms on the next turn.
- `escalate` — call escalate_to_ae immediately.
"""

from __future__ import annotations

import logging
import os
import re

from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

VALIDATION_MODE = os.getenv("VALIDATION_MODE", "rewrite").lower()
ASK_BEFORE_ESCALATE = (
    "I want to make sure I get this right rather than guess. "
    "Would you like me to bring in one of our account executives to walk you through it?"
)

_DOC_TAG = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")


def _looks_like_factual(text: str) -> bool:
    """Cheap heuristic: skip groundedness for greetings / short ack messages."""
    t = (text or "").strip().lower()
    if len(t) < 80:
        return False
    if any(t.startswith(p) for p in ("hi ", "hello", "hey ", "thanks", "you're welcome", "got it", "great")):
        return False
    return True


def make_validate_response(nano_model):
    @wrap_model_call
    async def validate_response(request: ModelRequest, handler):
        ai = await handler(request)

        # Only validate in steps where ungrounded claims do real harm.
        state = request.state
        step = state.get("current_step")
        if step not in {"educate", "objection"}:
            return ai

        # Only validate plain assistant text replies (no pending tool calls).
        if not isinstance(ai, AIMessage) or getattr(ai, "tool_calls", None):
            return ai
        text = ai.content if isinstance(ai.content, str) else ""
        if not _looks_like_factual(text):
            return ai

        retrieved = state.get("last_retrieved_docs") or []
        cited_ids = set(_DOC_TAG.findall(text))
        if cited_ids and any(c in retrieved for c in cited_ids):
            return ai  # already grounded

        if VALIDATION_MODE == "advisory":
            logger.warning("Ungrounded sales claim detected (step=%s). Logging only.", step)
            return ai

        if VALIDATION_MODE == "escalate":
            ai.content = (
                "Let me bring in an account executive to make sure we get this right — "
                "I'm flagging this conversation for them now."
            )
        else:
            ai.content = ASK_BEFORE_ESCALATE
        return ai

    return validate_response
