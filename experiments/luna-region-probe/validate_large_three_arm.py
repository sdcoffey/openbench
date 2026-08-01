"""Fail closed unless the large matched experiment is complete and secret-safe."""

from __future__ import annotations

import collections
import json
import os
import sys
from decimal import Decimal
from pathlib import Path


BASE = Path(__file__).resolve().parent
ARMS = {"direct-personal", "direct-internal", "openrouter-shared"}


def contains_secret(path: Path, secrets: list[bytes]) -> bool:
    overlap = max((len(value) for value in secrets), default=0)
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(256 * 1024):
            data = tail + chunk
            if any(value in data for value in secrets):
                return True
            tail = data[-overlap:] if overlap else b""
    return False


def main(argv: list[str]) -> int:
    destination = Path(argv[0]) if argv else BASE / "large-three-arm"
    manifest = json.loads((destination / "manifest.json").read_text())
    target = int(manifest["target_successful_blocks_per_condition"])
    summary = json.loads((destination / "run-summary.json").read_text())
    analysis = json.loads((destination / "analysis.json").read_text())
    conditions: collections.Counter[str] = collections.Counter()
    coordinates: set[tuple[str, int]] = set()
    openrouter_modes = collections.Counter()
    direct_ids = 0
    gateway_ids = 0
    for line in (destination / "successful-blocks.jsonl").open():
        block = json.loads(line)
        coordinate = (str(block["condition"]), int(block["pair_index"]))
        if coordinate in coordinates:
            raise SystemExit(f"duplicate successful coordinate: {coordinate}")
        coordinates.add(coordinate)
        conditions[coordinate[0]] += 1
        if set(block["samples"]) != ARMS:
            raise SystemExit(f"matched block missing arms: {coordinate}")
        for arm, sample in block["samples"].items():
            if not sample.get("success") or not sample.get("route_verified"):
                raise SystemExit(f"unsuccessful sample in matched dataset: {coordinate}:{arm}")
            if sample.get("reasoning_effort") != "none":
                raise SystemExit(f"invalid reasoning effort: {coordinate}:{arm}")
            if not isinstance(sample.get("ttft_s"), (int, float)):
                raise SystemExit(f"missing semantic TTFT: {coordinate}:{arm}")
            if coordinate[0] == "warm" and sample.get("socket_reused") is not True:
                raise SystemExit(f"warm socket not reused: {coordinate}:{arm}")
            if arm == "openrouter-shared":
                openrouter_modes[str(sample.get("is_byok"))] += 1
                gateway_ids += isinstance(sample.get("response_id"), str)
            else:
                direct_ids += isinstance(sample.get("request_id"), str)

    diagnostics = 0
    for line in (destination / "diagnostics.jsonl").open():
        trace = json.loads(line)
        diagnostics += 1
        controls = trace.get("request_controls", {})
        if controls.get("temperature_present") or controls.get("top_p_present"):
            raise SystemExit("sampling controls unexpectedly appeared in outbound request")
        if controls.get("reasoning") != {"effort": "none"}:
            raise SystemExit("outbound reasoning control drifted")

    expected_blocks = target * 2
    checks = {
        "cold_successes": conditions["cold"] == target,
        "warm_successes": conditions["warm"] == target,
        "three_matched_arms": len(coordinates) == expected_blocks,
        "all_direct_request_ids": direct_ids == expected_blocks * 2,
        "all_gateway_generation_ids": gateway_ids == expected_blocks,
        "all_shared_not_byok": openrouter_modes == {"False": expected_blocks},
        "analysis_complete": analysis["successful_blocks"] == expected_blocks,
        "summary_complete": summary["successful_blocks"] == expected_blocks,
        "conservative_gateway_budget": Decimal(str(summary["openrouter_conservative_spend_usd"])) < Decimal("1.95"),
        "all_wire_requests_sampling_matched": diagnostics >= expected_blocks * 3,
    }
    after = destination / "openrouter-credit-after.json"
    before = destination / "openrouter-credit-before.json"
    if after.exists() and before.exists():
        initial = json.loads(before.read_text()).get("usage")
        final = json.loads(after.read_text()).get("usage")
        if isinstance(initial, (int, float)) and isinstance(final, (int, float)):
            checks["actual_gateway_budget"] = Decimal(str(final)) - Decimal(str(initial)) < Decimal("2")

    required_names = ("OPENAI_API_KEY", "PERSONAL_OPENAI_API_KEY", "OPENROUTER_API_KEY")
    missing = [name for name in required_names if not os.environ.get(name)]
    if missing:
        raise SystemExit("credential values unavailable for secret scan: " + ", ".join(missing))
    secrets = [
        value.encode()
        for name in ("OPENAI_API_KEY", "PERSONAL_OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MGMT_KEY")
        if (value := os.environ.get(name))
    ]
    scanned = 0
    for path in destination.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md", ".toml", ".oql", ".py", ".log"}:
            scanned += 1
            if contains_secret(path, secrets):
                raise SystemExit(f"credential leaked into artifact: {path}")
    checks["no_credentials_persisted"] = True
    result = {
        "checks": checks,
        "checks_passed": all(checks.values()),
        "successful_blocks": len(coordinates),
        "diagnostic_requests": diagnostics,
        "scanned_files": scanned,
    }
    (destination / "validation.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
