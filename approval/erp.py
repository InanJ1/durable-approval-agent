"""A small SQLite stand-in for Corvane's ERP.

Master data (cost centers, suppliers) is loaded from the seeded CSV exports in data/.
Purchase orders and the notification outbox are written here by the workflow's activities.
Both writes are idempotent: a retried activity finds the existing row instead of adding one.
"""

import csv
import difflib
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from approval.models import Notification, PORecord, PurchaseRequest, Validation

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PO_PREFIX = "PO-2026-"
PO_START = 4100  # first PO number in this ERP is PO-2026-04101

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_centers (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, plant TEXT NOT NULL,
    manager TEXT, budget_usd REAL NOT NULL, spent_ytd_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT,
    status TEXT, payment_terms TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    cost_center TEXT NOT NULL, supplier_id TEXT NOT NULL,
    description TEXT NOT NULL, amount_usd REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    key TEXT PRIMARY KEY, request_id TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

PLACE_NAMES = {
    "indianapolis": "ind1", "indy": "ind1", "monterrey": "mty1",
    "columbus": "cmh", "dallas": "dfw", "atlanta": "atl",
}
SUFFIXES = {"inc", "co", "company", "llc", "corp", "corporation", "ltd"}


def db_path() -> Path:
    return Path(os.environ.get("CORVANE_DB", ROOT / "var" / "corvane.sqlite"))


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM cost_centers").fetchone()[0] == 0:
        load_master_data(conn)
    return conn


@contextmanager
def session(path: Optional[Path] = None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def load_master_data(conn: sqlite3.Connection, data_dir: Path = DATA_DIR) -> None:
    with conn:
        with open(data_dir / "cost_centers.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                conn.execute(
                    "INSERT OR REPLACE INTO cost_centers VALUES (?,?,?,?,?,?)",
                    (r["cost_center"].strip(), r["name"].strip(), r["plant"].strip(),
                     r["manager"].strip() or None, float(r["fy2026_budget_usd"]), float(r["spent_ytd_usd"])),
                )
        with open(data_dir / "suppliers.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                conn.execute(
                    "INSERT OR REPLACE INTO suppliers VALUES (?,?,?,?,?)",
                    (r["supplier_id"].strip(), " ".join(r["name"].split()), r["category"].strip(),
                     r["status"].strip().lower() or None, r["payment_terms"].strip()),
                )
        if conn.execute("SELECT COUNT(*) FROM sqlite_sequence WHERE name='purchase_orders'").fetchone()[0] == 0:
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('purchase_orders', ?)", (PO_START,))


# ---------- lookups ----------

def _norm(text: str) -> str:
    text = text.lower().replace(".", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [PLACE_NAMES.get(w, w) for w in text.split()]
    return " ".join(w for w in words if w not in SUFFIXES)


def _token_score(query: str, target: str) -> float:
    q, t = query.split(), target.split()
    if not q:
        return 0.0
    hits = sum(1 for w in q if any(w == x or (len(w) >= 4 and (x.startswith(w) or w.startswith(x))) for x in t))
    return hits / len(q)


def _score(query: str, *targets: str) -> float:
    q = _norm(query)
    best = 0.0
    for t in targets:
        n = _norm(t)
        best = max(best, difflib.SequenceMatcher(None, q, n).ratio(), 0.95 * _token_score(q, n))
        if q and (q == n or n.startswith(q + " ")):
            best = max(best, 1.0)
    return best


def find_supplier(conn: sqlite3.Connection, query: str) -> tuple[Optional[dict], list[str]]:
    """Return (best match or None, close candidate names)."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM suppliers")]
    scored = sorted(((_score(query, r["name"], r["supplier_id"]), r) for r in rows), key=lambda x: -x[0])
    candidates = [r["name"] for s, r in scored[:3] if s >= 0.5]
    if not scored or scored[0][0] < 0.75:
        return None, candidates
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02 and scored[0][0] < 1.0:
        return None, candidates  # ambiguous
    return scored[0][1], candidates


def find_cost_center(conn: sqlite3.Connection, query: str) -> tuple[Optional[dict], list[str]]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM cost_centers")]
    scored = sorted(((_score(query, r["code"], r["name"]), r) for r in rows), key=lambda x: -x[0])
    candidates = [f'{r["code"]} ({r["name"]})' for s, r in scored[:3] if s >= 0.5]
    if not scored or scored[0][0] < 0.75:
        return None, candidates
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.02 and scored[0][0] < 1.0:
        return None, candidates
    return scored[0][1], candidates


def budget_remaining(conn: sqlite3.Connection, code: str) -> Optional[float]:
    cc = conn.execute("SELECT budget_usd, spent_ytd_usd FROM cost_centers WHERE code=?", (code,)).fetchone()
    if cc is None:
        return None
    committed = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM purchase_orders WHERE cost_center=?", (code,)
    ).fetchone()[0]
    return round(cc["budget_usd"] - cc["spent_ytd_usd"] - committed, 2)


# ---------- validation ----------

def validate(conn: sqlite3.Connection, req: PurchaseRequest) -> Validation:
    reasons: list[str] = []
    cc = conn.execute("SELECT * FROM cost_centers WHERE code=?", (req.cost_center,)).fetchone()
    sup = conn.execute("SELECT * FROM suppliers WHERE supplier_id=?", (req.supplier_id,)).fetchone()
    remaining = budget_remaining(conn, req.cost_center) if cc else 0.0
    if cc is None:
        reasons.append(f"Unknown cost center {req.cost_center}.")
    elif not cc["manager"]:
        reasons.append(f"No approving manager on file for {req.cost_center}.")
    if sup is None:
        reasons.append(f"Unknown supplier {req.supplier_id}.")
    elif sup["status"] not in ("approved", "probation"):
        reasons.append(f"{sup['name']} is not on the approved-supplier list (status: {sup['status'] or 'missing'}).")
    if req.amount_usd <= 0:
        reasons.append("Amount must be greater than zero.")
    elif cc is not None and req.amount_usd > remaining:
        reasons.append(f"${req.amount_usd:,.2f} exceeds the ${remaining:,.2f} left in {req.cost_center}.")
    finance_required = req.amount_usd > req.finance_threshold_usd or (sup is not None and sup["status"] == "probation")
    return Validation(
        ok=not reasons, reasons=reasons, manager=cc["manager"] if cc else None,
        budget_remaining_usd=remaining or 0.0, finance_required=finance_required,
    )


# ---------- idempotent writes ----------

def po_number(seq: int) -> str:
    return f"{PO_PREFIX}{seq:05d}"


def create_purchase_order(conn: sqlite3.Connection, req: PurchaseRequest, created_at: str) -> PORecord:
    """Insert the PO for this request once. A second call returns the same PO number."""
    with conn:
        cur = conn.execute(
            "INSERT INTO purchase_orders (request_id, cost_center, supplier_id, description, amount_usd, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(request_id) DO NOTHING",
            (req.request_id, req.cost_center, req.supplier_id, req.description, req.amount_usd, created_at),
        )
        created = cur.rowcount == 1
    seq = conn.execute("SELECT po_seq FROM purchase_orders WHERE request_id=?", (req.request_id,)).fetchone()[0]
    return PORecord(po_number=po_number(seq), request_id=req.request_id, created=created)


def get_purchase_orders(conn: sqlite3.Connection, request_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM purchase_orders WHERE request_id=?", (request_id,)).fetchall()
    return [dict(r) | {"po_number": po_number(r["po_seq"])} for r in rows]


def add_notification(conn: sqlite3.Connection, n: Notification, created_at: str) -> bool:
    with conn:
        cur = conn.execute(
            "INSERT INTO notifications VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
            (n.key, n.request_id, n.recipient, n.subject, n.body, created_at),
        )
    return cur.rowcount == 1
