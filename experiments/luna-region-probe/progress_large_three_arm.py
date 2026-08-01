"""Read a compact live, success-only three-arm latency snapshot."""

from __future__ import annotations

import collections
import json
import statistics
import sys
from pathlib import Path

import numpy as np


BASE = Path(__file__).resolve().parent
ARM_IDS = ("direct-personal", "direct-internal", "openrouter-shared")


def main(argv: list[str]) -> int:
    destination = Path(argv[0]) if argv else BASE / "large-three-arm"
    path = destination / "successful-blocks.jsonl"
    blocks = [json.loads(line) for line in path.open()] if path.exists() else []
    failed_path = destination / "failures.jsonl"
    failures = [json.loads(line) for line in failed_path.open()] if failed_path.exists() else []
    groups = {}
    for condition in ("cold", "warm"):
        selected = [block for block in blocks if block.get("condition") == condition]
        for arm in ARM_IDS:
            values = [float(block["samples"][arm]["ttft_s"]) for block in selected]
            totals = [float(block["samples"][arm]["total_s"]) for block in selected]
            if not values:
                continue
            groups[f"{arm}:{condition}"] = {
                "count": len(values),
                "ttft_p50": round(statistics.median(values), 3),
                "ttft_p95": round(float(np.quantile(values, 0.95)), 3),
                "total_p95": round(float(np.quantile(totals, 0.95)), 3),
            }
    print(json.dumps({
        "successful_blocks": len(blocks),
        "discarded_blocks": len(failures),
        "failed_by_arm": dict(collections.Counter(item["failed_arm"] for item in failures)),
        "groups": groups,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
