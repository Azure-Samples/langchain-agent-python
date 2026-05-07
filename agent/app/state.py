"""SalesState: extends LangChain v1 AgentState with sales-funnel metadata.

The state machine in `app/middleware/steps.py` reads `current_step` to pick
the right system prompt + tool subset. State-mutating tools return
`Command(update={...})` to transition.

The funnel mirrors a Fin/SDR-style outbound + inbound flow:
    greet  →  qualify  →  educate  →  objection  →  book  →  handoff_to_ae
"""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired, TypeVar

from langchain.agents import AgentState

_T = TypeVar("_T")


def _take_last(left: _T | None, right: _T | None) -> _T | None:
    """Reducer: prefer the most recent non-None value.

    Without this, two tool calls in the same super-step that both update the
    same scalar key (e.g. two `set_intent` calls) raise
    INVALID_CONCURRENT_GRAPH_UPDATE. Most of our state keys are
    last-write-wins scalars, so this is the right semantic.
    """
    return right if right is not None else left


def _merge_list(left: list | None, right: list | None) -> list:
    """Reducer: append right onto left."""
    return [*(left or []), *(right or [])]


Step = Literal[
    "greet",
    "qualify",
    "educate",
    "objection",
    "book",
    "handoff_to_ae",
]

Intent = Literal[
    "evaluate_solution",   # exploratory: "what do you sell?", "tell me about Zava"
    "compare_pricing",     # "how much is X?", "what are your plans?"
    "see_case_study",      # "do you have customers like us?"
    "book_demo",            # "I want a demo", "let's chat"
    "objection",            # "X is too expensive", "your competitor does Y"
    "speak_to_human",       # "I want to talk to a real person"
    "other",
]


class SalesState(AgentState):
    """Conversation + sales-funnel state."""

    # Funnel position (last-write-wins to tolerate parallel tool calls).
    current_step: NotRequired[Annotated[Step, _take_last]]
    intent: NotRequired[Annotated[Intent, _take_last]]

    # Lead identification (set by greet/qualify)
    lead_id: NotRequired[Annotated[int | None, _take_last]]
    lead_email: NotRequired[Annotated[str | None, _take_last]]
    company_name: NotRequired[Annotated[str | None, _take_last]]

    # Qualification fields (BANT-ish; populated incrementally during qualify)
    industry: NotRequired[Annotated[str | None, _take_last]]
    team_size: NotRequired[Annotated[int | None, _take_last]]
    budget: NotRequired[Annotated[str | None, _take_last]]
    authority: NotRequired[Annotated[str | None, _take_last]]
    need: NotRequired[Annotated[str | None, _take_last]]
    timeline: NotRequired[Annotated[str | None, _take_last]]
    current_tools: NotRequired[Annotated[list[str], _merge_list]]

    # Objection tracking (append, never overwrite).
    objection_history: NotRequired[Annotated[list[str], _merge_list]]

    # Validation: list of doc_ids retrieved on this turn (for groundedness)
    last_retrieved_docs: NotRequired[Annotated[list[str], _merge_list]]

    # If validation rewrote a response asking the user to confirm escalation,
    # the next yes/no answer should be interpreted as confirming.
    awaiting_escalation_confirmation: NotRequired[Annotated[bool, _take_last]]


DEFAULT_STEP: Step = "greet"
