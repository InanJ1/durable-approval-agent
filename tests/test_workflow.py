"""Approval paths on Temporal's time-skipping test server (72 h of waiting runs in seconds)."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from approval import erp
from approval.activities import create_purchase_order, send_notification, validate_request
from approval.models import Decision, PurchaseRequest, RequestStatus
from approval.workflow import PurchaseApprovalWorkflow

REQUESTER = "ind1.planner@corvane.example"
MANAGER = "ind1.maint.manager@corvane.example"
FINANCE = "finance.controller@corvane.example"


@pytest.fixture(scope="session")
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


@pytest.fixture(autouse=True)
def fresh_erp(tmp_path, monkeypatch):
    monkeypatch.setenv("CORVANE_DB", str(tmp_path / "erp.sqlite"))
    monkeypatch.delenv("DEMO_ERP_ACK_DELAY_S", raising=False)


def make_request(amount: float, supplier_id: str = "SUP-1002", supplier: str = "Brightline Bearings Inc.") -> PurchaseRequest:
    return PurchaseRequest(
        request_id=f"PR-T{uuid.uuid4().hex[:6].upper()}", requester=REQUESTER, cost_center="CC-IND1-MAINT",
        supplier_id=supplier_id, supplier_name=supplier, description="Spindle bearings for CNC-03",
        amount_usd=amount,
    )


async def run_flow(env, req, decisions, activities=None) -> RequestStatus:
    """Start the workflow, send each decision once its step is pending, return the final status."""
    queue = f"tq-{uuid.uuid4().hex[:6]}"
    acts = activities or [validate_request, create_purchase_order, send_notification]
    with ThreadPoolExecutor(4) as pool:
        async with Worker(env.client, task_queue=queue, workflows=[PurchaseApprovalWorkflow],
                          activities=acts, activity_executor=pool):
            handle = await env.client.start_workflow(PurchaseApprovalWorkflow.run, req, id=req.request_id, task_queue=queue)
            for d in decisions:
                await wait_for_step(handle, d.step)
                await handle.signal(PurchaseApprovalWorkflow.submit_decision, d)
            return await handle.result()


async def wait_for_step(handle, step, tries=100):
    for _ in range(tries):
        s = await handle.query(PurchaseApprovalWorkflow.status)
        if s.pending_step == step:
            return s
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached {step}; state {s.state}")


def po_rows(request_id):
    with erp.session() as conn:
        return erp.get_purchase_orders(conn, request_id)


def outbox(request_id):
    with erp.session() as conn:
        return [r[0].split(":")[1] for r in conn.execute("SELECT key FROM notifications WHERE request_id=? ORDER BY created_at", (request_id,))]


async def test_manager_approves_under_threshold(env):
    req = make_request(1840.00)
    s = await run_flow(env, req, [Decision("manager", True, MANAGER)])
    assert s.state == "completed" and not s.finance_required
    assert [r["po_number"] for r in po_rows(req.request_id)] == [s.po_number]
    assert set(outbox(req.request_id)) == {"manager_needed", "po_created"}


async def test_manager_rejects(env):
    req = make_request(1840.00)
    s = await run_flow(env, req, [Decision("manager", False, MANAGER, "Use stock from DC-CMH")])
    assert s.state == "rejected" and s.po_number is None
    assert po_rows(req.request_id) == []
    assert "rejected" in outbox(req.request_id)


async def test_over_threshold_needs_finance(env):
    req = make_request(12400.00)
    s = await run_flow(env, req, [Decision("manager", True, MANAGER), Decision("finance", True, FINANCE)])
    assert s.state == "completed" and s.finance_required
    kinds = [e.kind for e in s.events]
    assert kinds.index("manager_approved") < kinds.index("awaiting_finance") < kinds.index("finance_approved") < kinds.index("po_created")
    assert len(po_rows(req.request_id)) == 1


async def test_finance_rejects(env):
    req = make_request(12400.00)
    s = await run_flow(env, req, [Decision("manager", True, MANAGER), Decision("finance", False, FINANCE, "Defer to Q1")])
    assert s.state == "rejected"
    assert po_rows(req.request_id) == []


async def test_exactly_threshold_skips_finance(env):
    req = make_request(5000.00)
    s = await run_flow(env, req, [Decision("manager", True, MANAGER)])
    assert s.state == "completed" and not s.finance_required


async def test_probation_supplier_needs_finance_even_when_small(env):
    req = make_request(900.00, "SUP-1008", "Sierra Madre Fasteners")
    s = await run_flow(env, req, [Decision("manager", True, MANAGER), Decision("finance", True, FINANCE)])
    assert s.state == "completed" and s.finance_required


async def test_no_decision_sends_reminder_then_expires(env):
    req = make_request(1840.00)
    t0 = await env.get_current_time()
    s = await run_flow(env, req, [])
    skipped_h = (await env.get_current_time() - t0).total_seconds() / 3600
    assert s.state == "expired" and s.po_number is None
    assert [e.kind for e in s.events][-3:] == ["awaiting_manager", "reminder", "expired"]
    assert outbox(req.request_id) == ["manager_needed", "manager_reminder", "expired"]
    assert skipped_h >= 72


async def test_blocked_supplier_is_invalid_without_waiting(env):
    req = make_request(400.00, "SUP-1009", "Quickbuy Surplus Parts")
    s = await run_flow(env, req, [])
    assert s.state == "invalid"
    assert "not on the approved-supplier list" in s.events[-1].detail


async def test_over_budget_is_invalid(env):
    req = make_request(4000.00)
    req.cost_center = "CC-IND1-CAST"  # $3,300 left in the seed data
    s = await run_flow(env, req, [])
    assert s.state == "invalid" and "exceeds" in s.events[-1].detail


async def test_bad_signals_are_ignored(env):
    req = make_request(1840.00)
    s = await run_flow(env, req, [
        Decision("manager", True, REQUESTER),                 # self-approval
        Decision("manager", True, "someone.else@corvane.example"),  # not the assigned approver
        Decision("manager", True, MANAGER),
    ])
    assert s.state == "completed"
    ignored = [e.detail for e in s.events if e.kind == "ignored"]
    assert len(ignored) == 2


# ---------- exactly-once PO ----------

def test_create_po_twice_returns_same_po():
    req = make_request(1840.00)
    with erp.session() as conn:
        first = erp.create_purchase_order(conn, req, "2026-10-17T00:00:00+00:00")
        second = erp.create_purchase_order(conn, req, "2026-10-17T00:00:05+00:00")
    assert first.created and not second.created
    assert first.po_number == second.po_number
    assert len(po_rows(req.request_id)) == 1


@activity.defn(name="create_purchase_order")
def create_po_then_crash_once(req: PurchaseRequest):
    """Write the PO, then fail on the first attempt as if the worker died before reporting back."""
    po = create_purchase_order(req)
    if activity.info().attempt == 1:
        raise ApplicationError("simulated crash after ERP commit")
    return po


async def test_retry_after_crash_creates_one_po(env):
    req = make_request(12400.00)
    s = await run_flow(env, req, [Decision("manager", True, MANAGER), Decision("finance", True, FINANCE)],
                       activities=[validate_request, create_po_then_crash_once, send_notification])
    rows = po_rows(req.request_id)
    assert s.state == "completed"
    assert len(rows) == 1 and rows[0]["po_number"] == s.po_number
