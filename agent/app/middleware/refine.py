"""Query refinement (Fin phase 1) — uses the cheap utility model.

Skipped for short, unambiguous messages or when the user is just confirming.
"""

from __future__ import annotations

import logging
import os

from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

REFINE_SKIP_THRESHOLD = int(os.getenv("REFINE_SKIP_THRESHOLD", "40"))

_REFINE_SYSTEM = (
    "You rewrite a single sales-conversation message to be unambiguous and self-contained. "
    "Resolve pronouns ('it', 'they', 'that plan') using the prior conversation. "
    "If the message is already clear, return it unchanged. "
    "Refuse only if the request is unsafe or off-topic; otherwise reply with just the rewritten message."
)


def make_refine_query(nano_model):
    """Factory: closes over the nano model so the middleware can reach it."""

    @wrap_model_call
    async def refine_query(request: ModelRequest, handler):
        msgs = request.messages
        last_user = next((m for m in reversed(msgs) if isinstance(m, HumanMessage)), None)
        if last_user is None:
            return await handler(request)
        text = (last_user.content or "").strip() if isinstance(last_user.content, str) else ""
        if len(text) < REFINE_SKIP_THRESHOLD or text.lower() in {"yes", "no", "yeah", "nope", "ok", "thanks", "sure"}:
            return await handler(request)

        try:
            refined = await nano_model.ainvoke(
                [
                    {"role": "system", "content": _REFINE_SYSTEM},
                    {"role": "user", "content": text},
                ]
            )
            new_text = (refined.content or "").strip() if isinstance(refined.content, str) else ""
            if new_text and new_text != text:
                logger.info("Refined query: %r -> %r", text[:60], new_text[:60])
                last_user.content = new_text
        except Exception:
            logger.exception("refine_query failed; using original message")
        return await handler(request)

    return refine_query
