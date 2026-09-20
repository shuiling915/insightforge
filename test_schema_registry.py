"""Test the SchemaRegistry with the large 200-table database."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))

from insightforge.data.registry import SchemaRegistry


def load_descriptions(workspace: str) -> dict:
    merged = {"tables": {}}
    for yf in sorted(Path(workspace).glob("*descriptions*.yaml")):
        with open(yf, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for t, info in data.get("tables", {}).items():
            merged["tables"].setdefault(t, {}).update(
                info if isinstance(info, dict) else {"description": str(info)}
            )
    return merged


def main():
    workspace = str(Path(__file__).parent / "workspace")
    descriptions = load_descriptions(workspace)
    reg = SchemaRegistry(workspace, descriptions)

    passed = 0
    failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"PASS: {name}")
        else:
            failed += 1
            print(f"FAIL: {name} {detail}")

    # 1. All 200 tables scanned
    check("scanned 200 tables", len(reg.tables) == 200, f"got {len(reg.tables)}")

    # 2. All tables have descriptions
    desc_count = sum(1 for t in reg.tables.values() if t.description)
    check("all tables have descriptions", desc_count == 200, f"got {desc_count}")

    # 3. Summary is within reasonable token budget (~5000 chars for 200 tables)
    summary = reg.get_summary()
    check(
        "summary under 25000 chars",
        len(summary) < 25000,
        f"got {len(summary)}",
    )
    check("summary contains table count", "(200)" in summary)

    # 4. describe_table returns column details
    detail = reg.describe_table("orders")
    check("describe_table has columns", "Columns:" in detail)
    check("describe_table has status enum", "values:" in detail)

    # 5. list_related Chinese query finds correct table
    related = reg.list_related("用户行为", top_k=3)
    top_names = [r[0] for r in related]
    check(
        "list_related Chinese finds user_behavior",
        "user_behavior" in top_names,
        f"got {top_names}",
    )

    # 6. list_related Chinese query for payments
    related = reg.list_related("订单支付", top_k=3)
    top_names = [r[0] for r in related]
    check(
        "list_related finds order_payments",
        "order_payments" in top_names,
        f"got {top_names}",
    )

    # 7. list_related English query
    related = reg.list_related("sales amount", top_k=5)
    top_names = [r[0] for r in related]
    check(
        "list_related English finds order tables",
        any("order" in n for n in top_names),
        f"got {top_names}",
    )

    # 8. get_sample returns rows
    sample = reg.get_sample("orders", n=2)
    check("get_sample returns data", "id" in sample and "created_at" in sample)

    # 9. Ambiguous tables are distinguishable by description
    t1 = reg.tables.get("transactions")
    t2 = reg.tables.get("transactions_log")
    check(
        "ambiguous tables have different descriptions",
        t1 is not None and t2 is not None and t1.description != t2.description,
    )

    # 10. Column descriptions loaded from YAML
    orders = reg.tables.get("orders")
    status_col = next((c for c in orders.columns if c.name == "status"), None)
    check(
        "column description loaded",
        status_col is not None and "订单状态" in (status_col.description or ""),
        f"got '{status_col.description if status_col else None}'",
    )

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)