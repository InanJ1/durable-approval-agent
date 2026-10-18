"""Durability demo: kill the worker twice mid-workflow and show the request still finishes
with exactly one PO.

  1. Start a $12,400 request (manager + finance). Manager approves.
  2. Kill the worker while the workflow waits for finance. Finance approves while no worker runs.
  3. Start a new worker. It replays history, applies the approval, writes the PO to the ERP.
     Kill it right after the ERP commit, before it reports the activity as done.
  4. Start a third worker. Temporal retries the activity; the insert finds the existing row.
  Result: workflow completed, 1 PO row, 1 "PO created" notification.

Needs the Temporal dev server (scripts/temporal_server.sh start). Uses its own task queue and
ERP file (var/demo_erp.sqlite), so it does not touch the chat demo's data. Repeatable.

python scripts/durability_demo.py
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowExecutionStatus

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from approval.client import apply_policy, connect, load_policy  # noqa: E402
from approval.models import Decision, PurchaseRequest, RequestStatus  # noqa: E402
from approval.workflow import PurchaseApprovalWorkflow  # noqa: E402

QUEUE = "corvane-durability-demo"
DB = ROOT / "var" / "demo_erp.sqlite"
ACK_DELAY_S = 8
T0 = time.perf_counter()


def say(msg: str) -> None:
    print(f"[{time.perf_counter() - T0:6.1f}s] {msg}", flush=True)


class WorkerProcess:
    def __init__(self, n: int):
        self.n, self.log = n, ROOT / "var" / f"demo_worker_{n}.log"
        env = os.environ | {"APPROVAL_TASK_QUEUE": QUEUE, "CORVANE_DB": str(DB),
                            "DEMO_ERP_ACK_DELAY_S": str(ACK_DELAY_S), "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen([sys.executable, "-m", "approval.worker"], cwd=ROOT, env=env,
                                     stdout=open(self.log, "w"), stderr=subprocess.STDOUT)

    async def ready(self) -> None:
        for _ in range(200):
            if "worker started" in self.log.read_text():
                say(f"worker #{self.n} up (pid {self.proc.pid})")
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"worker #{self.n} did not start")

    def kill(self, why: str) -> None:
        # Hard kill of the whole process tree, no graceful shutdown (like a crash or power loss).
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
        else:
            self.proc.kill()
        self.proc.wait()
        say(f"KILLED worker #{self.n} (pid {self.proc.pid}) {why}")

    def lines(self, needle: str) -> list[str]:
        return [ln.split(" ({")[0].strip() for ln in self.log.read_text().splitlines() if needle in ln]


def po_rows(rid: str) -> list[tuple]:
    if not DB.exists():
        return []
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT po_seq, request_id, amount_usd FROM purchase_orders WHERE request_id=?", (rid,)).fetchall()


def outbox_count(rid: str, kind: str) -> int:
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT COUNT(*) FROM notifications WHERE key=?", (f"{rid}:{kind}",)).fetchone()[0]


async def wait_state(handle, state: str, timeout: float = 30) -> RequestStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = await handle.query(PurchaseApprovalWorkflow.status, result_type=RequestStatus)
        if s.state == state:
            return s
        await asyncio.sleep(0.2)
    raise RuntimeError(f"never reached {state}")


async def main() -> None:
    for f in DB.parent.glob(DB.name + "*"):
        f.unlink()  # fresh demo ERP each run
    client = await connect()
    policy = load_policy()
    rid = f"PR-DEMO-{datetime.now():%H%M%S}"
    manager, finance = "ind1.maint.manager@corvane.example", policy["finance_approver"]
    req = apply_policy(PurchaseRequest(
        request_id=rid, requester="ind1.planner@corvane.example", cost_center="CC-IND1-MAINT",
        supplier_id="SUP-1002", supplier_name="Brightline Bearings Inc.",
        description="40 spindle bearings for CNC-03", amount_usd=12400.00), policy)

    say(f"request {rid}: ${req.amount_usd:,.2f}, needs manager and finance (threshold ${req.finance_threshold_usd:,.0f})")
    w1 = WorkerProcess(1)
    await w1.ready()
    handle = await client.start_workflow(PurchaseApprovalWorkflow.run, req, id=rid, task_queue=QUEUE)
    await wait_state(handle, "awaiting_manager")
    say("workflow is waiting for the manager")
    await handle.signal(PurchaseApprovalWorkflow.submit_decision, Decision("manager", True, manager, "OK for CNC-03 rebuild"))
    await wait_state(handle, "awaiting_finance")
    say("manager approved; workflow is waiting for finance")

    w1.kill("while the workflow waits for finance")
    await handle.signal(PurchaseApprovalWorkflow.submit_decision, Decision("finance", True, finance, "Within Q4 plan"))
    say("finance approved while NO worker is running; Temporal stored the signal")
    await asyncio.sleep(3)
    desc = await handle.describe()
    say(f"3 s later: workflow {desc.status.name}, PO rows in ERP = {len(po_rows(rid))}")

    w2 = WorkerProcess(2)
    await w2.ready()
    while not po_rows(rid):
        await asyncio.sleep(0.05)
    say(f"worker #2 replayed history and wrote the PO row (ERP holds the ack for {ACK_DELAY_S} s)")
    w2.kill("after the ERP commit, before the activity reported back")
    desc = await handle.describe()
    say(f"workflow {desc.status.name}, PO rows in ERP = {len(po_rows(rid))}; Temporal does not know the PO exists yet")

    w3 = WorkerProcess(3)
    await w3.ready()
    say("waiting for Temporal to time out the lost attempt (start_to_close 15 s) and retry it")
    result = await handle.result()
    say(f"workflow COMPLETED: state={result.state}, po={result.po_number}")
    w3.kill("(demo finished)")

    attempts = [e.activity_task_started_event_attributes.attempt
                async for e in handle.fetch_history_events()
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
                and e.activity_task_started_event_attributes.attempt > 1]
    rows = po_rows(rid)
    print("\n--- evidence ---")
    print(f"workflow id:                 {rid}  (Temporal UI: http://localhost:8233/namespaces/default/workflows/{rid})")
    print(f"final state:                 {result.state}, {result.po_number}")
    print(f"workers killed:              2 (#1 waiting for finance, #2 after ERP commit)")
    print(f"create_purchase_order:       retried, attempt {max(attempts) if attempts else 1} completed it")
    print(f"PO rows for {rid}: {len(rows)}  {rows}")
    print(f"'PO created' notifications:  {outbox_count(rid, 'po_created')}")
    print(f"worker #2 log:               {w2.lines('PO PO-')}")
    print(f"worker #3 log:               {w3.lines('PO PO-')}")
    print("workflow events:")
    for e in result.events:
        print(f"  {e.at}  {e.kind:<17} {e.detail}")
    ok = result.state == "completed" and len(rows) == 1 and outbox_count(rid, "po_created") == 1
    print(f"\nEXACTLY ONCE: {'PASS' if ok else 'FAIL'}  (total {time.perf_counter() - T0:.1f} s)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
