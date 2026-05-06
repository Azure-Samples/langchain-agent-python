"""SalesState: extends LangChain v1 AgentState with sales-funnel metadata.

The state machine in `app/middleware/steps.py` reads `current_step` to pick
the right system prompt + tool subset. State-mutating tools return
`Command(update={...})` to transition.

The funnel mirrors a Fin/SDR-style outbound + inbound flow:
    greet  →  qualify  →  educate  →  objection  →  book  →  handoff_to_ae
"""

from __future__ import annotations

from typing import Literal, NotRequired

from langchain.agents import AgentState

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

    # Funnel position
    current_step: NotRequired[Step]
    intent: NotRequired[Intent]

    # Lead identification (set by greet/qualify)
    lead_id: NotRequired[int | None]
    lead_email: NotRequired[str | None]
    company_name: NotRequired[str | None]

    # Qualification fields (BANT-ish; populated incrementally during qualify)
    industry: NotRequired[str | None]
    team_size: NotRequired[int | None]
    budget: NotRequired[str | None]            # "<$10k" | "$10-50k" | ">$50k" | "unknown"
    authority: NotRequired[str | None]         # "decision_maker" | "influencer" | "evaluator"
    need: NotRequired[str | None]              # short text summary of the buyer's pain
    timeline: NotRequired[str | None]          # "this_quarter" | "next_quarter" | "exploring"
    current_tools: NotRequired[list[str]]      # tools they use today

    # Objection tracking
    objection_history: NotRequired[list[str]]  # raw objection text

    # Validation: list of doc_ids retrieved on this turn (for groundedness)
    last_retrieved_docs: NotRequired[list[str]]

    # If validation rewrote a response asking the user to confirm escalation,
    # the next yes/no answer should be interpreted as confirming.
    awaiting_escalation_confirmation: NotRequired[bool]


DEFAULT_STEP: Step = "greet"
