"""Data passed between the agent, the workflow and the activities.

Plain dataclasses so Temporal's default JSON converter can serialize them.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PurchaseRequest:
    request_id: str
    requester: str
    cost_center: str
    supplier_id: str
    supplier_name: str
    description: str
    amount_usd: float
    # Policy values are copied in at start time so the workflow stays deterministic.
    finance_threshold_usd: float = 5000.0
    finance_approver: str = "finance.controller@corvane.example"
    reminder_after_s: int = 24 * 3600
    expire_after_s: int = 72 * 3600


@dataclass
class Validation:
    ok: bool
    reasons: list[str]
    manager: Optional[str]
    budget_remaining_usd: float
    finance_required: bool


@dataclass
class Decision:
    step: str  # "manager" or "finance"
    approved: bool
    approver: str
    comment: str = ""


@dataclass
class Event:
    at: str
    kind: str
    detail: str


@dataclass
class RequestStatus:
    request_id: str
    state: str  # validating, awaiting_manager, awaiting_finance, creating_po, completed, rejected, expired, invalid
    amount_usd: float
    cost_center: str
    supplier_name: str
    description: str
    requester: str
    finance_required: bool
    pending_step: Optional[str]
    expected_approver: Optional[str]
    budget_remaining_usd: Optional[float]
    po_number: Optional[str]
    events: list[Event] = field(default_factory=list)


@dataclass
class PORecord:
    po_number: str
    request_id: str
    created: bool  # False when the PO already existed (a retried activity)


@dataclass
class Notification:
    key: str  # idempotency key, one row per key in the outbox
    request_id: str
    recipient: str
    subject: str
    body: str


TERMINAL_STATES = {"completed", "rejected", "expired", "invalid"}
