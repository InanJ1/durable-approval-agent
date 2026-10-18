"""Helpers the agent and scripts use to talk to Temporal."""

import json
import os
from dataclasses import asdict
from datetime import timedelta

from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from approval.erp import ROOT
from approval.models import Decision, PurchaseRequest, RequestStatus
from approval.workflow import TASK_QUEUE, PurchaseApprovalWorkflow


def load_policy() -> dict:
    with open(ROOT / "config" / "policy.json", encoding="utf-8") as f:
        return json.load(f)


def apply_policy(req: PurchaseRequest, policy: dict) -> PurchaseRequest:
    req.finance_threshold_usd = float(policy["finance_threshold_usd"])
    req.finance_approver = policy["finance_approver"]
    req.reminder_after_s = int(policy["reminder_after_hours"] * 3600)
    req.expire_after_s = int(policy["expire_after_hours"] * 3600)
    return req


async def connect() -> Client:
    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )


async def start_request(client: Client, req: PurchaseRequest) -> tuple[WorkflowHandle, bool]:
    """Start the workflow. The request ID is the workflow ID, so a repeat submit
    of the same request returns the running workflow instead of a second one."""
    try:
        handle = await client.start_workflow(
            PurchaseApprovalWorkflow.run, req, id=req.request_id, task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        return handle, True
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle(req.request_id), False


async def get_status(client: Client, request_id: str, timeout_s: float = 5.0) -> RequestStatus:
    """Read the request's state. Completed workflows answer from their result; running
    ones answer a query, which needs a live worker (it fails after timeout_s if none)."""
    handle = client.get_workflow_handle(request_id, result_type=RequestStatus)
    desc = await handle.describe()
    if desc.status == WorkflowExecutionStatus.COMPLETED:
        return await handle.result()
    return await handle.query(
        PurchaseApprovalWorkflow.status, result_type=RequestStatus, rpc_timeout=timedelta(seconds=timeout_s),
    )


async def send_decision(client: Client, request_id: str, decision: Decision) -> None:
    await client.get_workflow_handle(request_id).signal(PurchaseApprovalWorkflow.submit_decision, decision)


def status_dict(s: RequestStatus, last_events: int = 6) -> dict:
    d = asdict(s)
    d["events"] = d["events"][-last_events:]
    return d
