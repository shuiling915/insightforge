"""Test the MetricRegistry alias matching and integration."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))

from insightforge.data.metrics import MetricRegistry


def load_metrics(workspace: str) -> dict:
    data = {}
    for yf in sorted(Path(workspace).glob("*metrics*.yaml")):
        with open(yf, encoding="utf-8") as f:
            data.update(yaml.safe_load(f) or {})
    return data


def main():
    workspace = str(Path(__file__).parent / "workspace")
    reg = MetricRegistry(load_metrics(workspace))

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

    # 1. Metrics loaded
    check("metrics loaded", len(reg.metrics) > 0, f"got {len(reg.metrics)}")

    # 2. Exact alias match returns score 1.0
    results = reg.find_metrics("销售额", top_k=1)
    check(
        "exact alias match score=1.0",
        results and results[0][1] == 1.0,
        f"got {results}",
    )
    check(
        "exact alias match returns gmv",
        results and results[0][0].key == "gmv",
        f"got {results[0][0].key if results else None}",
    )

    # 3. English alias GMV also matches gmv
    results = reg.find_metrics("GMV", top_k=1)
    check(
        "English alias GMV matches gmv",
        results and results[0][0].key == "gmv",
        f"got {results[0][0].key if results else None}",
    )

    # 4. Active users alias
    results = reg.find_metrics("DAU", top_k=1)
    check(
        "DAU matches active_users",
        results and results[0][0].key == "active_users",
        f"got {results[0][0].key if results else None}",
    )

    # 5. describe_metric returns SQL definition
    detail = reg.describe_metric("gmv")
    check("describe_metric has SQL def", "SUM(oi.quantity" in detail)
    check("describe_metric has tables", "orders" in detail and "order_items" in detail)

    # 6. describe_metric by alias
    detail = reg.describe_metric("销售额")
    check("describe_metric by alias works", "成交额" in detail)

    # 7. Keyword partial match for 品类
    results = reg.find_metrics("品类", top_k=3)
    keys = [r[0].key for r in results]
    check(
        "品类 finds category_sales",
        "category_sales" in keys,
        f"got {keys}",
    )

    # 8. Summary is non-empty and contains metric count
    summary = reg.get_summary()
    check("summary non-empty", "Available Metrics" in summary and "gmv" in summary)

    # 9. Metric not found returns message
    detail = reg.describe_metric("nonexistent_metric_xyz")
    check("unknown metric returns not found", "not found" in detail)

    # 10. Empty query returns empty
    results = reg.find_metrics("", top_k=3)
    check("empty query returns nothing", len(results) == 0)

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)