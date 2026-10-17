"""Temporal activities: the only code in the approval flow that touches the ERP.

They are plain sync functions; the worker runs them on a thread pool.
"""

import os
import time
from datetime import datetime, timezone

from temporalio import activity

from approval import erp
from approval.models import Notification, PORecord, PurchaseRequest, Validation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@activity.defn
def validate_request(req: PurchaseRequest) -> Validation:
    with erp.session() as conn:
        return erp.validate(conn, req)


@activity.defn
def create_purchase_order(req: PurchaseRequest) -> PORecord:
    info = activity.info()
    with erp.session() as conn:
        po = erp.create_purchase_order(conn, req, _now())
    activity.logger.info(
        "PO %s for %s (attempt %d, %s)", po.po_number, req.request_id, info.attempt,
        "inserted" if po.created else "already existed, returned existing row",
    )
    # Demo only: hold the activity open after the ERP commit so the durability demo
    # can kill the worker between "row written" and "Temporal told it finished".
    delay = float(os.environ.get("DEMO_ERP_ACK_DELAY_S", "0"))
    if delay and po.created:
        time.sleep(delay)
    return po


@activity.defn
def send_notification(n: Notification) -> bool:
    """Write to the outbox table (stands in for email/Teams). Idempotent by key."""
    with erp.session() as conn:
        created = erp.add_notification(conn, n, _now())
    activity.logger.info("notify %s: %s%s", n.recipient, n.subject, "" if created else " (duplicate ignored)")
    return created
