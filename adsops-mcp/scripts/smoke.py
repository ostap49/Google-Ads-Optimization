"""Read-only smoke checks against your real MCC, one stage at a time.

    python scripts/smoke.py 1                    # auth + list_accounts + run_gaql
    python scripts/smoke.py 2 --account stryi    # read tools
    python scripts/smoke.py all --account 1234567890

Only read tools and propose_* in validate-only mode are exercised; nothing is ever applied.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402  (registers all tools)
from tools._registry import TOOLS  # noqa: E402


def call(name: str, **kwargs):
    t = time.time()
    out = TOOLS[name].fn(**kwargs)
    dt = time.time() - t
    text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
    print(f"\n=== {name}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})  [{dt:.1f}s]")
    print(text[:4000] + ("\n... (truncated for display)" if len(text) > 4000 else ""))
    return out


def stage1(acc):
    out = call("list_accounts")
    acc = acc or (out.get("accounts") or [{}])[0].get("customer_id")
    if not acc:
        sys.exit("No accounts returned — check login_customer_id / access.")
    call("run_gaql", customer_id=acc, query=(
        "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, metrics.cost_micros "
        "FROM campaign WHERE segments.date DURING LAST_30_DAYS"), sort_by="metrics.cost", limit=10)
    return acc


STAGES = {"1": [stage1]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("stage", help="1..8 or all")
    p.add_argument("--account", help="customer ID or alias (default: first account)")
    a = p.parse_args()
    server.build_server()
    acc = a.account
    for key in (sorted(STAGES) if a.stage == "all" else [a.stage]):
        for fn in STAGES[key]:
            acc = fn(acc) or acc


if __name__ == "__main__":
    main()
