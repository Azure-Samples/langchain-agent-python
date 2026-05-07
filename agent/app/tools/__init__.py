"""Local @tool definitions for the sales agent.

These run in-process inside the agent. Data tools (Postgres queries,
semantic product search, case-study lookup, pricing) come from the MCP
server at runtime and are concatenated with `LOCAL_TOOLS` in `agent.py`.
"""

from .workflow import (
    advance_to_step,
    back_to_greet,
    create_handoff_summary,
    escalate_to_ae,
    log_activity,
    log_objection,
    lookup_lead_by_email,
    propose_meeting_times,
    set_intent,
    update_lead_profile,
)

LOCAL_TOOLS = [
    set_intent,
    lookup_lead_by_email,
    update_lead_profile,
    advance_to_step,
    back_to_greet,
    log_objection,
    propose_meeting_times,
    create_handoff_summary,
    log_activity,
    escalate_to_ae,
]

__all__ = ["LOCAL_TOOLS"]
