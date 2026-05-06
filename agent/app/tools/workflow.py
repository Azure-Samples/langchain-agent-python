"""Local workflow / state-transition tools for the sales funnel.

These tools mutate `SalesState` via `Command(update=...)` to advance the
agent through the funnel. Data lookups (case studies, pricing, product
search) live on the MCP server.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.state import Intent, SalesState, Step

logger = logging.getLogger(__name__)

# Map intent → next step. Used by `set_intent`.
_INTENT_TO_STEP: dict[str, Step] = {
    "evaluate_solution": "qualify",
    "compare_pricing": "educate",
    "see_case_study": "educate",
    "book_demo": "qualify",   # qualify first, then book
    "objection": "objection",
    "speak_to_human": "handoff_to_ae",
    "other": "greet",
}


@tool
def set_intent(
    intent: Intent,
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Record the visitor's high-level intent and advance to the matching funnel step."""
    next_step = _INTENT_TO_STEP.get(intent, "greet")
    return Command(
        update={
            "intent": intent,
            "current_step": next_step,
            "messages": [ToolMessage(f"Routed to {next_step}.", tool_call_id=tool_call_id)],
        }
    )


@tool
def lookup_lead_by_email(
    email: str,
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Try to find an existing lead by their work email so we can personalise the conversation.

    Returns a friendly tool message either way; only updates state if a match is found.
    Note: the MCP server owns the actual lead database. This local tool is a no-op
    placeholder until `lookup_lead_by_email` is exposed by the MCP service; once it is,
    move the call there. For now, we just record the email on state.
    """
    clean = (email or "").strip().lower()
    if "@" not in clean:
        return Command(
            update={"messages": [ToolMessage(f"'{email}' doesn't look like an email.", tool_call_id=tool_call_id)]}
        )
    return Command(
        update={
            "lead_email": clean,
            "messages": [
                ToolMessage(
                    f"Recorded lead email {clean}. (Lead lookup against the CRM is not yet wired.)",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def update_lead_profile(
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    company_name: str | None = None,
    industry: str | None = None,
    team_size: int | None = None,
    budget: str | None = None,
    authority: str | None = None,
    need: str | None = None,
    timeline: str | None = None,
    current_tools: list[str] | None = None,
) -> Command:
    """Record qualification fields the lead has shared.

    Pass only the fields that were *just* learned; leave the rest as None. The state
    keeps prior values for fields you don't pass.
    """
    update: dict = {}
    for key, value in [
        ("company_name", company_name),
        ("industry", industry),
        ("team_size", team_size),
        ("budget", budget),
        ("authority", authority),
        ("need", need),
        ("timeline", timeline),
        ("current_tools", current_tools),
    ]:
        if value is not None:
            update[key] = value
    if not update:
        return Command(
            update={"messages": [ToolMessage("No new lead fields to record.", tool_call_id=tool_call_id)]}
        )
    summary = ", ".join(f"{k}={v}" for k, v in update.items())
    update["messages"] = [ToolMessage(f"Updated lead profile: {summary}", tool_call_id=tool_call_id)]
    return Command(update=update)


@tool
def advance_to_step(
    step: Step,
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Advance to a specific funnel step. Use this when the visitor's mood or readiness
    changes mid-conversation (e.g. they're suddenly ready to book after qualify).
    """
    return Command(
        update={
            "current_step": step,
            "messages": [ToolMessage(f"Advanced to {step}.", tool_call_id=tool_call_id)],
        }
    )


@tool
def back_to_greet(
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Return to the greet step when the conversation has gone off-track or the visitor
    wants to start a different topic.
    """
    return Command(
        update={
            "current_step": "greet",
            "messages": [ToolMessage("Returned to greet.", tool_call_id=tool_call_id)],
        }
    )


@tool
def log_objection(
    objection_text: str,
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Append a raw objection to the conversation history so the AE can review it."""
    state = runtime.state
    history = list(state.get("objection_history") or [])
    history.append(objection_text)
    return Command(
        update={
            "objection_history": history,
            "messages": [ToolMessage(f"Logged objection: {objection_text[:80]}", tool_call_id=tool_call_id)],
        }
    )


@tool
def propose_meeting_times(
    times_iso: list[str],
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Propose 2–3 candidate meeting slots to the visitor.

    `times_iso` should be ISO-8601 datetimes you've already negotiated in chat.
    The actual calendar booking happens once the lead is handed off to an AE
    (or, in a later iteration, via the WorkIQ MCP server).
    """
    if not times_iso:
        return Command(
            update={"messages": [ToolMessage("No times to propose.", tool_call_id=tool_call_id)]}
        )
    rendered = "; ".join(times_iso[:3])
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"Proposed meeting times: {rendered}. Once the lead confirms, the AE will book on the real calendar.",
                    tool_call_id=tool_call_id,
                )
            ]
        }
    )


@tool
def create_handoff_summary(
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    next_steps: str = "Schedule a 30-min discovery call.",
) -> Command:
    """Build a structured summary the AE can read in 30 seconds before they take over.

    Pulls company/industry/budget/authority/need/timeline/objections from state.
    Always call this as the final step before handing off.
    """
    s = runtime.state
    summary_lines = [
        "## Handoff summary",
        f"- **Lead email**: {s.get('lead_email') or 'unknown'}",
        f"- **Company**: {s.get('company_name') or 'unknown'}",
        f"- **Industry**: {s.get('industry') or 'unknown'}",
        f"- **Team size**: {s.get('team_size') or 'unknown'}",
        f"- **Budget**: {s.get('budget') or 'unknown'}",
        f"- **Authority**: {s.get('authority') or 'unknown'}",
        f"- **Need**: {s.get('need') or 'unknown'}",
        f"- **Timeline**: {s.get('timeline') or 'unknown'}",
        f"- **Current tools**: {', '.join(s.get('current_tools') or []) or 'unknown'}",
    ]
    objections = s.get("objection_history") or []
    if objections:
        summary_lines.append("- **Objections raised**:")
        summary_lines.extend(f"  - {o}" for o in objections)
    summary_lines.append(f"- **Next step**: {next_steps}")
    body = "\n".join(summary_lines)
    return Command(
        update={
            "current_step": "handoff_to_ae",
            "messages": [ToolMessage(body, tool_call_id=tool_call_id)],
        }
    )


@tool
def log_activity(
    activity_type: Literal["chat", "demo_proposed", "objection", "handoff", "escalated"],
    note: str,
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Record a CRM-style activity entry. (Stored in conversation messages until the
    MCP server exposes a `log_activity` write tool.)
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"[activity] {ts} type={activity_type} note={note}",
                    tool_call_id=tool_call_id,
                )
            ]
        }
    )


@tool
def escalate_to_ae(
    reason: str,
    runtime: ToolRuntime[None, SalesState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Escalate to a human account executive. Only call after the visitor explicitly
    confirms (e.g. they answered 'yes' to 'shall I bring in an AE?').
    """
    return Command(
        update={
            "current_step": "handoff_to_ae",
            "awaiting_escalation_confirmation": False,
            "messages": [
                ToolMessage(
                    f"🙋 Escalated to an AE. Reason: {reason}. They'll follow up by email shortly.",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )
