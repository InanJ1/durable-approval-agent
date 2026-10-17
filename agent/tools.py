"""Tools the purchasing agent can call. ERP reads run in a thread so the async
LangGraph server is not blocked; workflow calls go through approval.client."""

import asyncio
import hashlib
import json
import os
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from temporalio.service import RPCError

from approval import erp
from approval.client import apply_policy, connect, get_status, load_policy, start_request, status_dict
from approval.models import PurchaseRequest

POLICY = load_policy()


def _find(kind: str, query: str):
    with erp.session() as conn:
        if kind == "supplier":
            return erp.find_supplier(conn, query)
        row, candidates = erp.find_cost_center(conn, query)
        if row is not None:
            row["budget_remaining_usd"] = erp.budget_remaining(conn, row["code"])
        return row, candidates


def _requester(config: RunnableConfig) -> str:
    configurable = (config or {}).get("configurable", {})
    return configurable.get("requester") or os.environ.get("DEMO_REQUESTER", "ind1.planner@corvane.example")


@tool
async def lookup_supplier(name: str) -> str:
    """Check a supplier against Corvane's approved-supplier list. Returns its ID and status."""
    row, candidates = await asyncio.to_thread(_find, "supplier", name)
    if row is None:
        return json.dumps({"found": False, "query": name, "close_matches": candidates})
    return json.dumps({
        "found": True, "supplier_id": row["supplier_id"], "name": row["name"], "category": row["category"],
        "status": row["status"] or "missing", "payment_terms": row["payment_terms"],
    })


@tool
async def lookup_budget(cost_center: str) -> str:
    """Look up a cost center's remaining FY2026 budget and its approving manager.
    Accepts a code such as CC-IND1-MAINT or a name such as 'Indianapolis maintenance'."""
    row, candidates = await asyncio.to_thread(_find, "cost_center", cost_center)
    if row is None:
        return json.dumps({"found": False, "query": cost_center, "close_matches": candidates})
    return json.dumps({
        "found": True, "cost_center": row["code"], "name": row["name"], "plant": row["plant"],
        "manager": row["manager"] or "missing", "budget_remaining_usd": row["budget_remaining_usd"],
        "finance_review_above_usd": POLICY["finance_threshold_usd"],
    })


@tool
async def submit_purchase_request(
    cost_center: str, supplier: str, description: str, amount_usd: float,
    tool_call_id: Annotated[str, InjectedToolCallId], config: RunnableConfig,
) -> Command:
    """Submit a purchase request. This starts the approval workflow (manager, then finance above
    the threshold) and returns the request ID."""
    sup, sup_cands = await asyncio.to_thread(_find, "supplier", supplier)
    cc, cc_cands = await asyncio.to_thread(_find, "cost_center", cost_center)
    if sup is None or cc is None:
        result = {"submitted": False, "error": "Could not match the supplier or cost center.",
                  "supplier_matches": sup_cands if sup is None else [sup["name"]],
                  "cost_center_matches": cc_cands if cc is None else [cc["code"]]}
        return Command(update={"messages": [ToolMessage(json.dumps(result), tool_call_id=tool_call_id)]})

    thread_id = (config or {}).get("configurable", {}).get("thread_id", "")
    key = f"{thread_id}|{cc['code']}|{sup['supplier_id']}|{float(amount_usd):.2f}|{description.strip().lower()}"
    request_id = "PR-" + hashlib.sha1(key.encode()).hexdigest()[:6].upper()
    req = apply_policy(PurchaseRequest(
        request_id=request_id, requester=_requester(config), cost_center=cc["code"],
        supplier_id=sup["supplier_id"], supplier_name=sup["name"], description=description.strip(),
        amount_usd=round(float(amount_usd), 2),
    ), POLICY)
    client = await connect()
    _, started = await start_request(client, req)
    result = {"submitted": True, "request_id": request_id, "new": started, "cost_center": cc["code"],
              "supplier": sup["name"], "amount_usd": req.amount_usd}
    return Command(update={
        "messages": [ToolMessage(json.dumps(result), tool_call_id=tool_call_id)],
        "review_request_id": request_id,
    })


@tool
async def get_request_status(request_id: str) -> str:
    """Get the current approval status and recent history of a purchase request (ID like PR-1A2B3C)."""
    client = await connect()
    try:
        return json.dumps(status_dict(await get_status(client, request_id.strip().upper())))
    except RPCError as e:
        return json.dumps({"request_id": request_id, "error": f"Could not read status: {e.message}"})


@tool
async def review_request(request_id: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Open a purchase request for the approver to approve or reject."""
    rid = request_id.strip().upper()
    return Command(update={
        "messages": [ToolMessage(json.dumps({"opened_for_review": rid}), tool_call_id=tool_call_id)],
        "review_request_id": rid,
    })


TOOLS = [lookup_supplier, lookup_budget, submit_purchase_request, get_request_status, review_request]
