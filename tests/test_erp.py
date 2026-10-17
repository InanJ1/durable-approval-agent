"""Lookups the agent relies on: messy supplier names, cost-center names, budget math."""

import pytest

from approval import erp


@pytest.fixture
def conn(tmp_path):
    with erp.session(tmp_path / "erp.sqlite") as c:
        yield c


@pytest.mark.parametrize("query,expected", [
    ("Brightline", "SUP-1002"),
    ("brightline bearings", "SUP-1002"),
    ("Pellum Seal Company", "SUP-1004"),
    ("Keystone MRO", "SUP-1006"),
    ("Ostrava Motors", "SUP-1003"),
    ("Tarrant Packaging", "SUP-1005"),
])
def test_supplier_matching_handles_export_mess(conn, query, expected):
    row, _ = erp.find_supplier(conn, query)
    assert row is not None and row["supplier_id"] == expected


def test_unknown_supplier_returns_none(conn):
    row, _ = erp.find_supplier(conn, "Acme Rockets")
    assert row is None


@pytest.mark.parametrize("query,expected", [
    ("CC-IND1-MAINT", "CC-IND1-MAINT"),
    ("Indianapolis maintenance", "CC-IND1-MAINT"),
    ("Monterrey maintenance", "CC-MTY1-MAINT"),
    ("Dallas DC", "CC-DC-DFW"),
])
def test_cost_center_matching(conn, query, expected):
    row, _ = erp.find_cost_center(conn, query)
    assert row is not None and row["code"] == expected


def test_ambiguous_cost_center_is_not_guessed(conn):
    row, candidates = erp.find_cost_center(conn, "maintenance")
    assert row is None and len(candidates) >= 2


def test_budget_remaining_counts_open_pos(conn):
    from approval.models import PurchaseRequest
    before = erp.budget_remaining(conn, "CC-IND1-CAST")
    assert before == 3300.00
    req = PurchaseRequest("PR-B1", "x@corvane.example", "CC-IND1-CAST", "SUP-1001", "Halvard Castings", "cores", 1000.0)
    erp.create_purchase_order(conn, req, "2026-10-17T00:00:00+00:00")
    assert erp.budget_remaining(conn, "CC-IND1-CAST") == 2300.00


def test_po_numbers_start_at_seeded_sequence(conn):
    from approval.models import PurchaseRequest
    req = PurchaseRequest("PR-S1", "x@corvane.example", "CC-IND1-MAINT", "SUP-1002", "Brightline", "bearings", 10.0)
    assert erp.create_purchase_order(conn, req, "2026-10-17T00:00:00+00:00").po_number == "PO-2026-04101"
