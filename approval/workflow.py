"""The durable purchase-request approval workflow.

validate -> manager approval (signal) -> finance approval if over the threshold (signal)
-> create PO in the ERP (idempotent activity) -> notify.

All state lives in Temporal's event history, so a worker crash or restart loses nothing:
a new worker replays the history and continues from the last completed step.
"""

import asyncio
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from approval.activities import create_purchase_order, send_notification, validate_request
    from approval.models import Decision, Event, Notification, PurchaseRequest, RequestStatus

TASK_QUEUE = "corvane-purchase-approvals"

ERP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30), maximum_attempts=10,
)
NOTIFY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10), maximum_attempts=5,
)


@workflow.defn
class PurchaseApprovalWorkflow:
    @workflow.init
    def __init__(self, req: PurchaseRequest) -> None:
        self._req = req
        self._state = "validating"
        self._pending: Optional[str] = None
        self._approvers: dict[str, str] = {}
        self._decisions: dict[str, Decision] = {}
        self._finance_required = False
        self._budget_remaining: Optional[float] = None
        self._po: Optional[str] = None
        self._events: list[Event] = []

    @workflow.run
    async def run(self, req: PurchaseRequest) -> RequestStatus:
        self._log("submitted", f"{req.requester} asked for ${req.amount_usd:,.2f} from {req.supplier_name}")

        v = await workflow.execute_activity(
            validate_request, req, start_to_close_timeout=timedelta(seconds=30), retry_policy=ERP_RETRY,
        )
        self._budget_remaining = v.budget_remaining_usd
        self._finance_required = v.finance_required
        if not v.ok:
            self._state = "invalid"
            self._log("invalid", " ".join(v.reasons))
            await self._notify(req.requester, "invalid", f"{req.request_id} was not submitted", " ".join(v.reasons))
            return self.status()
        self._log("validated", f"budget left ${v.budget_remaining_usd:,.2f}; finance review {'required' if v.finance_required else 'not required'}")

        if not await self._approval_step("manager", v.manager):
            return self.status()
        if v.finance_required and not await self._approval_step("finance", req.finance_approver):
            return self.status()

        self._state = "creating_po"
        po = await workflow.execute_activity(
            create_purchase_order, req, start_to_close_timeout=timedelta(seconds=15), retry_policy=ERP_RETRY,
        )
        self._po = po.po_number
        self._state = "completed"
        self._log("po_created", f"{po.po_number} written to ERP")
        await self._notify(req.requester, "po_created", f"{req.request_id} approved: {po.po_number}",
                           f"{po.po_number} for ${req.amount_usd:,.2f} with {req.supplier_name} is in the ERP.")
        return self.status()

    async def _approval_step(self, step: str, approver: str) -> bool:
        req = self._req
        self._pending, self._state = step, f"awaiting_{step}"
        self._approvers[step] = approver
        self._log(f"awaiting_{step}", f"waiting for {approver}")
        await self._notify(approver, f"{step}_needed", f"Approval needed: {req.request_id}",
                           f"${req.amount_usd:,.2f} {req.supplier_name}: {req.description}")

        decided = await self._wait_for(step, req.reminder_after_s)
        if not decided:
            self._log("reminder", f"no {step} decision after {req.reminder_after_s // 3600} h, reminded {approver}")
            await self._notify(approver, f"{step}_reminder", f"Reminder: {req.request_id} is waiting for you", "")
            decided = await self._wait_for(step, req.expire_after_s - req.reminder_after_s)
        self._pending = None
        if not decided:
            self._state = "expired"
            self._log("expired", f"no {step} decision after {req.expire_after_s // 3600} h")
            await self._notify(req.requester, "expired", f"{req.request_id} expired", f"No {step} decision in time.")
            return False

        d = self._decisions[step]
        if not d.approved:
            self._state = "rejected"
            await self._notify(req.requester, "rejected", f"{req.request_id} rejected by {step}", d.comment)
            return False
        return True

    async def _wait_for(self, step: str, seconds: int) -> bool:
        try:
            await workflow.wait_condition(lambda: step in self._decisions, timeout=timedelta(seconds=seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def _notify(self, recipient: str, kind: str, subject: str, body: str) -> None:
        n = Notification(key=f"{self._req.request_id}:{kind}", request_id=self._req.request_id,
                         recipient=recipient, subject=subject, body=body)
        try:
            await workflow.execute_activity(
                send_notification, n, start_to_close_timeout=timedelta(seconds=10), retry_policy=NOTIFY_RETRY,
            )
        except ActivityError:
            # A failed notification must not block or fail the approval itself.
            self._log("notify_failed", f"could not notify {recipient} ({kind})")

    @workflow.signal
    def submit_decision(self, d: Decision) -> None:
        if d.step != self._pending:
            self._log("ignored", f"{d.step} decision from {d.approver}: step is not pending")
        elif d.step in self._decisions:
            self._log("ignored", f"duplicate {d.step} decision from {d.approver}")
        elif d.approver == self._req.requester:
            self._log("ignored", f"{d.approver} cannot approve their own request")
        elif d.approver != self._approvers.get(d.step):
            self._log("ignored", f"{d.approver} is not the assigned {d.step} approver")
        else:
            self._decisions[d.step] = d
            verb = "approved" if d.approved else "rejected"
            self._log(f"{d.step}_{verb}", f"{d.approver}{': ' + d.comment if d.comment else ''}")

    @workflow.query
    def status(self) -> RequestStatus:
        r = self._req
        return RequestStatus(
            request_id=r.request_id, state=self._state, amount_usd=r.amount_usd, cost_center=r.cost_center,
            supplier_name=r.supplier_name, description=r.description, requester=r.requester,
            finance_required=self._finance_required, pending_step=self._pending,
            expected_approver=self._approvers.get(self._pending) if self._pending else None,
            budget_remaining_usd=self._budget_remaining, po_number=self._po, events=list(self._events),
        )

    def _log(self, kind: str, detail: str) -> None:
        self._events.append(Event(at=workflow.now().isoformat(timespec="seconds"), kind=kind, detail=detail))
