"""Corvane purchasing agent (LangGraph).

agent (LLM + tools) -> tools -> agent ...
                          \-> prepare_review -> ask_approver (interrupt) -> prepare_review ... -> END

The LLM only extracts the request and calls tools. Routing into the approval steps is
deterministic: once a request is submitted or opened for review, the graph reads the
workflow's state from Temporal and raises one interrupt per pending approval step.
The approver's answer is sent to the workflow as a signal. Temporal, not this graph,
owns the approval state.
"""

import asyncio
import os
import time
from typing import Optional

from langchain_core.messages import AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from temporalio.service import RPCError

from agent.tools import TOOLS
from approval.client import connect, get_status, send_decision, status_dict
from approval.models import TERMINAL_STATES, Decision, RequestStatus

PROMPT = """You are the purchasing assistant for Corvane Industrial, a pump and gearbox manufacturer.
You help employees raise purchase requests and check on them.
A request needs a cost center, a supplier, what is being bought, and the total amount in USD.
To raise one: call lookup_supplier and lookup_budget, then submit_purchase_request with the matched names.
If the supplier is blocked or unknown, or the budget is too low, explain that instead of submitting.
For status questions call get_request_status. If an approver wants to review a request, call review_request.
Ask for missing details. Never invent request IDs, amounts or approvals. Answer in two or three sentences."""


class AgentState(MessagesState):
    review_request_id: Optional[str]
    review_step: Optional[str]
    review_snapshot: Optional[dict]


llm = ChatOpenAI(
    model=os.environ.get("LLM_MODEL", "qwen2.5:7b"),
    base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
    api_key=os.environ.get("LLM_API_KEY", "ollama"),
    temperature=0,
    timeout=float(os.environ.get("LLM_TIMEOUT_S", "300")),
    max_retries=1,
).bind_tools(TOOLS)


async def agent(state: AgentState) -> dict:
    reply = await llm.ainvoke([SystemMessage(PROMPT), *state["messages"]])
    # Only a tool called in this turn (submit or review) may route into the approval steps.
    return {"messages": [reply], "review_request_id": None, "review_step": None, "review_snapshot": None}


def after_agent(state: AgentState) -> str:
    return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END


def after_tools(state: AgentState) -> str:
    return "prepare_review" if state.get("review_request_id") else "agent"


async def _settled_status(request_id: str, wait_s: float = 20.0) -> RequestStatus:
    """Poll until the workflow is waiting on a person or finished (not mid-activity)."""
    client = await connect()
    deadline = time.monotonic() + wait_s
    while True:
        s = await get_status(client, request_id)
        if s.pending_step or s.state in TERMINAL_STATES or time.monotonic() > deadline:
            return s
        await asyncio.sleep(0.5)


def summarize(s: RequestStatus) -> str:
    by_kind = {e.kind: e.detail for e in s.events}
    if s.state == "completed":
        approvals = [f"manager ({by_kind.get('manager_approved', '').split(':')[0]})"]
        if "finance_approved" in by_kind:
            approvals.append(f"finance ({by_kind['finance_approved'].split(':')[0]})")
        return (f"{s.request_id} is fully approved by {' and '.join(approvals)}. "
                f"{s.po_number} for ${s.amount_usd:,.2f} with {s.supplier_name} is in the ERP.")
    if s.state == "rejected":
        step = "finance" if "finance_rejected" in by_kind else "manager"
        return f"{s.request_id} was rejected by {step}: {by_kind.get(step + '_rejected', '')}. No PO was created."
    if s.state == "invalid":
        return f"{s.request_id} did not pass validation: {by_kind.get('invalid', '')}"
    if s.state == "expired":
        return f"{s.request_id} expired: {by_kind.get('expired', '')}."
    return f"{s.request_id} is {s.state.replace('_', ' ')}. The workflow keeps its place and will continue."


async def prepare_review(state: AgentState) -> dict:
    rid = state["review_request_id"]
    try:
        s = await _settled_status(rid)
    except RPCError as e:
        return {"messages": [AIMessage(f"I could not read {rid} from Temporal ({e.message}). "
                                       "The request is not lost; ask me for its status again shortly.")],
                "review_request_id": None, "review_step": None}
    if s.pending_step:
        return {"review_step": s.pending_step, "review_snapshot": status_dict(s)}
    return {"messages": [AIMessage(summarize(s))], "review_request_id": None, "review_step": None}


def after_prepare(state: AgentState) -> str:
    return "ask_approver" if state.get("review_step") else END


def _parse_decision(resume) -> tuple[bool, str]:
    """Accept agent-chat-ui's {"decisions": [{"type": "approve"}]} or a plain 'approve'/'reject'."""
    if isinstance(resume, dict) and "decisions" in resume:
        resume = resume["decisions"][0]
    if isinstance(resume, list):
        resume = resume[0]
    if isinstance(resume, str):
        return resume.strip().lower().startswith("approve"), ""
    return resume.get("type") == "approve", resume.get("message") or ""


async def ask_approver(state: AgentState) -> dict:
    snap, step, rid = state["review_snapshot"], state["review_step"], state["review_request_id"]
    approver = snap["expected_approver"]
    resume = interrupt({
        "action_requests": [{
            "name": f"{step}_approval",
            "args": {
                "request_id": rid, "amount_usd": snap["amount_usd"], "supplier": snap["supplier_name"],
                "cost_center": snap["cost_center"], "description": snap["description"],
                "requester": snap["requester"], "budget_remaining_usd": snap["budget_remaining_usd"],
                "approver": approver,
            },
            "description": (f"{step.title()} approval for {rid}: ${snap['amount_usd']:,.2f} from "
                            f"{snap['supplier_name']} on {snap['cost_center']}. Approving signals the Temporal workflow."),
        }],
        "review_configs": [{"action_name": f"{step}_approval", "allowed_decisions": ["approve", "reject"]}],
    })
    approved, comment = _parse_decision(resume)
    client = await connect()
    await send_decision(client, rid, Decision(step=step, approved=approved, approver=approver, comment=comment))
    # Wait until the workflow has applied the signal, so the next read shows the next step.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        s = await get_status(client, rid)
        if s.pending_step != step:
            break
        await asyncio.sleep(0.5)
    verb = "approved" if approved else "rejected"
    return {"messages": [AIMessage(f"Sent the {step} decision to the workflow: {verb} by {approver}.")],
            "review_step": None, "review_snapshot": None}


builder = StateGraph(AgentState)
builder.add_node("agent", agent)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_node("prepare_review", prepare_review)
builder.add_node("ask_approver", ask_approver)
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", after_agent, ["tools", END])
builder.add_conditional_edges("tools", after_tools, ["prepare_review", "agent"])
builder.add_conditional_edges("prepare_review", after_prepare, ["ask_approver", END])
builder.add_edge("ask_approver", "prepare_review")
graph = builder.compile()
