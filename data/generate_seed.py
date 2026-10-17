"""Generate Corvane Industrial's synthetic purchasing master data (seed=42).

Writes two CSV files shaped like Corvane's ERP exports:
  data/cost_centers.csv  budgets and approving managers per cost center
  data/suppliers.csv     the approved-supplier list, with the usual export mess

Run: python data/generate_seed.py
"""

import csv
import random
from pathlib import Path

SEED = 42
OUT = Path(__file__).resolve().parent

# code, name, plant, manager (blank = missing in the export)
COST_CENTERS = [
    ("CC-IND1-MAINT", "IND1 Plant Maintenance", "IND1", "ind1.maint.manager@corvane.example"),
    ("CC-IND1-PROD", "IND1 Pump Assembly", "IND1", "ind1.prod.manager@corvane.example"),
    ("CC-IND1-CAST", "IND1 Casting Line", "IND1", "ind1.cast.manager@corvane.example"),
    ("CC-IND1-QUAL", "IND1 Quality and Test Stands", "IND1", "ind1.quality.manager@corvane.example"),
    ("CC-MTY1-MAINT", "MTY1 Plant Maintenance", "MTY1", "mty1.maint.manager@corvane.example"),
    ("CC-MTY1-PROD", "MTY1 Gearbox Assembly", "MTY1", "mty1.prod.manager@corvane.example"),
    ("CC-DC-CMH", "DC Columbus Operations", "DC-CMH", "dc.cmh.manager@corvane.example"),
    ("CC-DC-DFW", "DC Dallas Operations", "DC-DFW", "dc.dfw.manager@corvane.example"),
    # Real-world gap: the export has no approving manager for Atlanta.
    ("CC-DC-ATL", "DC Atlanta Operations", "DC-ATL", ""),
]

# supplier_id, name as it appears in the export, category, status (blank = missing)
SUPPLIERS = [
    ("SUP-1001", "Halvard Castings", "castings", "approved"),
    ("SUP-1002", "Brightline Bearings Inc.", "bearings", "approved"),
    ("SUP-1003", "Ostrava Motor Works", "motors", "approved"),
    ("SUP-1004", "PELLUM SEAL CO", "seals", "approved"),
    ("SUP-1005", "Tarrant Packaging ", "packaging", "approved"),
    ("SUP-1006", "Keystone M.R.O. Supply", "mro", "approved"),
    ("SUP-1007", "Ridgeway Freight", "logistics", "approved"),
    ("SUP-1008", "Sierra Madre Fasteners", "fasteners", "probation"),
    ("SUP-1009", "Quickbuy Surplus Parts", "mro", "blocked"),
    ("SUP-1010", "Lakeshore Electric Controls", "electrical", ""),
]

TERMS = ["Net 30", "Net 45", "Net 60", "2/10 Net 30"]


def main() -> None:
    rng = random.Random(SEED)

    with open(OUT / "cost_centers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["cost_center", "name", "plant", "manager", "fy2026_budget_usd", "spent_ytd_usd"])
        for code, name, plant, manager in COST_CENTERS:
            budget = rng.randrange(80_000, 600_000, 5_000)
            spent_share = rng.uniform(0.35, 0.80)
            if code == "CC-IND1-CAST":
                spent_share = 0.985  # casting line is nearly out of budget this year
            spent = round(budget * spent_share, 2)
            w.writerow([code, name, plant, manager, f"{budget:.2f}", f"{spent:.2f}"])

    with open(OUT / "suppliers.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["supplier_id", "name", "category", "status", "payment_terms"])
        for sid, name, category, status in SUPPLIERS:
            w.writerow([sid, name, category, status, rng.choice(TERMS)])

    print(f"wrote {len(COST_CENTERS)} cost centers and {len(SUPPLIERS)} suppliers to {OUT}")


if __name__ == "__main__":
    main()
