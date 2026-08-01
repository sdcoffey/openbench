"""Analyze paired success-only account comparisons and separately retain failures."""

from __future__ import annotations

import collections
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats


BASE = Path(__file__).resolve().parent
ARM_IDS = ("direct-personal", "direct-internal", "openrouter-shared")
COMPARISONS = (
    ("openrouter-shared", "direct-personal"),
    ("openrouter-shared", "direct-internal"),
    ("direct-internal", "direct-personal"),
)
METRICS = ("header_s", "first_body_s", "ttft_s", "total_s")


def summarize(values: np.ndarray) -> dict[str, Any]:
    if not len(values):
        return {"count": 0}
    quantiles = np.quantile(values, [0.5, 0.9, 0.95, 0.99, 0.999])
    return {
        "count": int(len(values)),
        "mean": round(float(np.mean(values)), 6),
        "p50": round(float(quantiles[0]), 6),
        "p90": round(float(quantiles[1]), 6),
        "p95": round(float(quantiles[2]), 6),
        "p99": round(float(quantiles[3]), 6),
        "p999": round(float(quantiles[4]), 6),
        "max": round(float(np.max(values)), 6),
    }


def bootstrap_contrast(left: np.ndarray, right: np.ndarray, quantile: float, *, seed: int) -> list[float]:
    generator = np.random.default_rng(seed)
    draws = 2_000
    batch = 100
    observed: list[float] = []
    for start in range(0, draws, batch):
        size = min(batch, draws - start)
        indexes = generator.integers(0, len(left), size=(size, len(left)))
        if quantile == 0.0:
            differences = np.median(left[indexes] - right[indexes], axis=1)
        else:
            differences = (
                np.quantile(left[indexes], quantile, axis=1)
                - np.quantile(right[indexes], quantile, axis=1)
            )
        observed.extend(float(item) for item in differences)
    return [round(float(value), 6) for value in np.quantile(observed, [0.025, 0.975])]


def holm_adjust(values: list[tuple[str, float]]) -> dict[str, float]:
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, (name, pvalue) in enumerate(sorted(values, key=lambda item: item[1])):
        running = min(1.0, max(running, pvalue * (len(values) - index)))
        adjusted[name] = running
    return adjusted


def samples_for_bucket(members: list[dict[str, Any]], arm: str, metric: str) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(members, key=lambda item: float(item["samples"][arm][metric]))
    positions = {
        "p50": len(ordered) // 2,
        "p95": min(len(ordered) - 1, math.floor(0.95 * (len(ordered) - 1))),
        "p99": min(len(ordered) - 1, math.floor(0.99 * (len(ordered) - 1))),
        "max": len(ordered) - 1,
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for name, position in positions.items():
        indexes = range(max(0, position - 2), min(len(ordered), position + 3))
        result[name] = [
            {
                "condition": ordered[index]["condition"],
                "pair_index": ordered[index]["pair_index"],
                "block_attempt": ordered[index]["block_attempt"],
                "metric_s": ordered[index]["samples"][arm][metric],
                "arm": arm,
                "request_id": ordered[index]["samples"][arm]["request_id"],
                "response_id": ordered[index]["samples"][arm]["response_id"],
                "matched_samples": ordered[index]["samples"],
            }
            for index in indexes
        ]
    return result


def main(argv: list[str]) -> int:
    destination = Path(argv[0]) if argv else BASE / "large-three-arm"
    blocks = [
        json.loads(line)
        for line in (destination / "successful-blocks.jsonl").open(encoding="utf-8")
    ]
    failures_path = destination / "failures.jsonl"
    failures = (
        [json.loads(line) for line in failures_path.open(encoding="utf-8")]
        if failures_path.exists()
        else []
    )
    attempts_path = destination / "attempts.jsonl"
    attempted_counts: collections.Counter[str] = collections.Counter()
    attempt_failure_counts: collections.Counter[str] = collections.Counter()
    gateway_frozen = 0.0
    for line in attempts_path.open(encoding="utf-8"):
        attempt = json.loads(line)
        arm = str(attempt["arm"])
        attempted_counts[arm] += 1
        if not attempt["sample"].get("success"):
            attempt_failure_counts[arm] += 1
        if arm == "openrouter-shared":
            gateway_frozen += float(attempt.get("openrouter_budget_debit_usd", 0))

    groups: dict[str, Any] = {}
    contrasts: dict[str, Any] = {}
    buckets: dict[str, Any] = {}
    hypothesis_tests: list[tuple[str, float]] = []
    for condition in ("cold", "warm"):
        members = [block for block in blocks if block["condition"] == condition]
        for arm in ARM_IDS:
            samples = [block["samples"][arm] for block in members]
            groups[f"{arm}:{condition}"] = {
                "successful_samples": len(samples),
                "metrics": {
                    metric: summarize(np.array([float(sample[metric]) for sample in samples]))
                    for metric in METRICS
                },
                "post_header_phases": {
                    "first_body_s": summarize(np.array([
                        float(sample["first_body_s"]) - float(sample["header_s"])
                        for sample in samples
                    ])),
                    "semantic_ttft_s": summarize(np.array([
                        float(sample["ttft_s"]) - float(sample["header_s"])
                        for sample in samples
                    ])),
                },
                "tokens": {
                    name: summarize(np.array([
                        float(sample[name])
                        for sample in samples
                        if isinstance(sample.get(name), (int, float))
                    ]))
                    for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
                },
                "service_tiers": dict(collections.Counter(str(sample.get("final_service_tier")) for sample in samples)),
                "effective_sampling": dict(collections.Counter(
                    f"temperature={sample.get('effective_temperature')} top_p={sample.get('effective_top_p')}"
                    for sample in samples
                )),
                "reasoning_efforts": dict(collections.Counter(str(sample.get("reasoning_effort")) for sample in samples)),
                "router_byok_values": dict(collections.Counter(str(sample.get("is_byok")) for sample in samples)),
                "router_regions": dict(collections.Counter(str(sample.get("router_region")) for sample in samples)),
                "header_names": dict(collections.Counter(header for sample in samples for header in sample.get("header_names", []))),
            }
            buckets[f"{arm}:{condition}:ttft"] = samples_for_bucket(members, arm, "ttft_s")
            buckets[f"{arm}:{condition}:headers"] = samples_for_bucket(members, arm, "header_s")

        for comparison_index, (left_arm, right_arm) in enumerate(COMPARISONS):
            key = f"{left_arm}_minus_{right_arm}:{condition}"
            comparison: dict[str, Any] = {"matched_blocks": len(members), "metrics": {}}
            for metric_index, metric in enumerate(METRICS):
                left = np.array([float(block["samples"][left_arm][metric]) for block in members])
                right = np.array([float(block["samples"][right_arm][metric]) for block in members])
                difference = left - right
                nonzero = difference[difference != 0]
                faster_count = int(np.sum(nonzero < 0))
                slower_count = int(np.sum(nonzero > 0))
                sign_p = float(stats.binomtest(faster_count, len(nonzero), 0.5).pvalue) if len(nonzero) else 1.0
                test_name = f"{key}:{metric}"
                hypothesis_tests.append((test_name, sign_p))
                seed = 20260810 + comparison_index * 100 + metric_index * 10 + (condition == "warm")
                comparison["metrics"][metric] = {
                    "paired_difference": summarize(difference),
                    "paired_median_95ci": bootstrap_contrast(left, right, 0.0, seed=seed),
                    "p95_difference_s": round(float(np.quantile(left, 0.95) - np.quantile(right, 0.95)), 6),
                    "p95_difference_95ci": bootstrap_contrast(left, right, 0.95, seed=seed + 1000),
                    "p99_difference_s": round(float(np.quantile(left, 0.99) - np.quantile(right, 0.99)), 6),
                    "left_faster_count": faster_count,
                    "right_faster_count": slower_count,
                    "sign_test_p": sign_p,
                }
            contrasts[key] = comparison

    corrected = holm_adjust(hypothesis_tests)
    for contrast_name, contrast in contrasts.items():
        for metric_name, metric in contrast["metrics"].items():
            metric["holm_adjusted_sign_test_p"] = corrected[f"{contrast_name}:{metric_name}"]

    failure_summary = {
        "discarded_blocks": len(failures),
        "by_arm": dict(collections.Counter(item["failed_arm"] for item in failures)),
        "by_arm_condition": dict(collections.Counter(f"{item['failed_arm']}:{item['condition']}" for item in failures)),
        "error_codes": dict(collections.Counter(str(item["failed_sample"].get("error_code")) for item in failures)),
        "attempted_requests_by_arm": dict(attempted_counts),
        "failed_requests_by_arm": dict(attempt_failure_counts),
        "failure_rate_by_arm": {
            arm: round(attempt_failure_counts[arm] / attempted_counts[arm], 6)
            for arm in ARM_IDS
            if attempted_counts[arm]
        },
    }
    result = {
        "successful_blocks": len(blocks),
        "groups": groups,
        "paired_contrasts": contrasts,
        "failures": failure_summary,
        "openrouter_conservative_frozen_debit_usd": round(gateway_frozen, 8),
    }
    (destination / "analysis.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    (destination / "request-id-buckets.json").write_text(json.dumps(buckets, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
