"""Render the large, three-account statistical benchmark findings."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


BASE = Path(__file__).resolve().parent
ARM_ORDER = ("direct-personal", "direct-internal", "openrouter-shared")


def pretty(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def main(argv: list[str]) -> int:
    destination = Path(argv[0]) if argv else BASE / "large-three-arm"
    analysis = json.loads((destination / "analysis.json").read_text())
    run = json.loads((destination / "run-summary.json").read_text())
    before = json.loads((destination / "openrouter-credit-before.json").read_text())
    after_path = destination / "openrouter-credit-after.json"
    after = json.loads(after_path.read_text()) if after_path.exists() else {}
    usage_before = before.get("usage")
    usage_after = after.get("usage")
    actual_spend = (
        round(float(usage_after) - float(usage_before), 8)
        if isinstance(usage_before, (int, float)) and isinstance(usage_after, (int, float))
        else None
    )
    failures = analysis["failures"]
    roles: Counter[str] = Counter()
    with (destination / "diagnostics.jsonl").open() as handle:
        for line in handle:
            roles[str(json.loads(line).get("role"))] += 1

    lines = [
        "# Large three-account Luna gateway benchmark",
        "",
        "## Experiment",
        "",
        f"- Matched, successful cold blocks: **{run['successful_blocks_per_condition']['cold']}**.",
        f"- Matched, successful warm blocks: **{run['successful_blocks_per_condition']['warm']}**.",
        "- Routes: personal OpenAI API key, internal OpenAI API key, and OpenRouter shared OpenAI provider account.",
        "- Model: `gpt-5.6-luna`; reasoning effort `none`; maximum output 64 tokens.",
        "- Each route omits `temperature` and `top_p`, so OpenAI receives the same sampling defaults.",
        "- Every failed attempt discards the entire matched block; a fresh nonce is used when retrying.",
        f"- Concurrency: {json.loads((destination / 'manifest.json').read_text())['workers']} independent matched-block workers.",
        f"- Total attempted arm operations: {run['attempted_requests']}; {roles['measured']} measured HTTP requests and {roles['primer']} warm primers.",
        "",
        "## Success-only latency distributions",
        "",
        "| Condition | Route | Header p50 | TTFT p50 | TTFT p95 | TTFT p99 | Stream p50 | Stream p95 | Stream p99 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("cold", "warm"):
        for arm in ARM_ORDER:
            group = analysis["groups"][f"{arm}:{condition}"]["metrics"]
            lines.append(
                f"| {condition} | {arm} | {pretty(group['header_s']['p50'])} s | "
                f"{pretty(group['ttft_s']['p50'])} s | {pretty(group['ttft_s']['p95'])} s | "
                f"{pretty(group['ttft_s']['p99'])} s | {pretty(group['total_s']['p50'])} s | "
                f"{pretty(group['total_s']['p95'])} s | {pretty(group['total_s']['p99'])} s |"
            )

    lines += [
        "",
        "## Paired statistical contrasts",
        "",
        "Negative values favor the first-named route. P values use a paired two-sided sign test with Holm correction across every route, timing, and temperature comparison. Confidence intervals use paired block bootstrap resampling.",
        "",
        "| Contrast | Condition | Metric | Paired median difference | 95% median CI | p95 difference | 95% p95 CI | Holm-adjusted p | Faster blocks |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("cold", "warm"):
        for left, right in (
            ("openrouter-shared", "direct-personal"),
            ("openrouter-shared", "direct-internal"),
            ("direct-internal", "direct-personal"),
        ):
            contrast = analysis["paired_contrasts"][f"{left}_minus_{right}:{condition}"]
            for metric in ("ttft_s", "total_s", "header_s"):
                row = contrast["metrics"][metric]
                lines.append(
                    f"| {left} − {right} | {condition} | {metric} | "
                    f"{pretty(row['paired_difference']['p50'])} s | "
                    f"[{pretty(row['paired_median_95ci'][0])}, {pretty(row['paired_median_95ci'][1])}] s | "
                    f"{pretty(row['p95_difference_s'])} s | "
                    f"[{pretty(row['p95_difference_95ci'][0])}, {pretty(row['p95_difference_95ci'][1])}] s | "
                    f"{row['holm_adjusted_sign_test_p']:.3g} | "
                    f"{row['left_faster_count']}/{contrast['matched_blocks']} |"
                )

    lines += [
        "",
        "## Failures excluded from the latency analysis",
        "",
        f"- Discarded matched blocks: **{failures['discarded_blocks']}**.",
    ]
    for arm in ARM_ORDER:
        attempts = failures["attempted_requests_by_arm"].get(arm, 0)
        rejected = failures["failed_requests_by_arm"].get(arm, 0)
        rate = failures["failure_rate_by_arm"].get(arm, 0)
        lines.append(f"- `{arm}`: {rejected}/{attempts} failed attempts ({rate * 100:.2f}%).")
    lines += [
        f"- Failure codes: `{json.dumps(failures['error_codes'], sort_keys=True)}`.",
        "",
        "## Sampling and header discrepancy",
        "",
        "Both direct routes expose their real OpenAI sampling values: temperature `1.0` and `top_p=0.98`. OpenRouter's client-visible stream reports `top_p=1`, but its sanitized upstream request echo confirms that neither `temperature` nor `top_p` is forwarded; OpenAI consequently applies the same Luna defaults. Router response-object sampling fields are therefore not reliable evidence of upstream execution parameters.",
        "",
        "Direct routes expose `x-request-id`, `openai-processing-ms`, and API quota headers. OpenRouter strips those upstream headers and supplies its own gateway generation ID; selected gateway generation records are resolved separately to upstream OpenAI response IDs for production tracing.",
        "",
        "| Condition | Route | Header p50 | Header-to-body p50 | Header-to-useful-token p50 | Output tokens p50 | Cached input p50 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for condition in ("cold", "warm"):
        for arm in ARM_ORDER:
            group = analysis["groups"][f"{arm}:{condition}"]
            lines.append(
                f"| {condition} | {arm} | {pretty(group['metrics']['header_s']['p50'])} s | "
                f"{pretty(group['post_header_phases']['first_body_s']['p50'])} s | "
                f"{pretty(group['post_header_phases']['semantic_ttft_s']['p50'])} s | "
                f"{pretty(group['tokens']['output_tokens'].get('p50'))} | "
                f"{pretty(group['tokens']['cached_tokens'].get('p50'))} |"
            )
    lines += [
        "",
        "## Production routing and matched outliers",
        "",
        "Production log lookups use hashed organization identifiers rather than retaining account, project, or user identifiers. All accounts receive final service tier `default`, but their frontend-cluster distributions differ.",
        "",
        "| Route | Employee account | Organization fingerprints | Sampled frontend clusters |",
        "|---|---|---|---|",
    ]
    for arm in ARM_ORDER:
        route_path = destination / f"routing-final-{arm}.json"
        if not route_path.exists():
            continue
        routing = json.loads(route_path.read_text())["arms"][arm]
        clusters = ", ".join(f"`{key}` ({value})" for key, value in sorted(routing["frontend_counts"].items()))
        employees = ", ".join(f"{key}: {value}" for key, value in sorted(routing["employee_values"].items()))
        organizations = ", ".join(f"`{key}` ({value})" for key, value in sorted(routing["organization_fingerprints"].items()))
        lines.append(f"| {arm} | {employees} | {organizations} | {clusters} |")

    lines += [
        "",
        "Representative slow requests show that any of the three accounts can land on an unlucky serving path while the simultaneous controls remain fast:",
        "",
        "| Slow route | Slow request TTFT | Matched personal TTFT | Matched internal TTFT | Matched OpenRouter TTFT | Frontend | Engine region |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for arm in ARM_ORDER:
        trace_path = destination / f"production-trace-{arm}.json"
        if not trace_path.exists():
            continue
        trace = json.loads(trace_path.read_text())
        samples = trace["matched_samples"]
        frontend = ", ".join(trace["trace"].get("frontend_clusters", []))
        region = ", ".join(trace["trace"].get("engine_regions", []))
        lines.append(
            f"| {arm} | {pretty(trace['observed_ttft_s'])} s | "
            f"{pretty(samples['direct-personal']['ttft_s'])} s | "
            f"{pretty(samples['direct-internal']['ttft_s'])} s | "
            f"{pretty(samples['openrouter-shared']['ttft_s'])} s | `{frontend}` | `{region}` |"
        )

    correlation_path = destination / "gateway-correlation-summary.json"
    if correlation_path.exists():
        correlation = json.loads(correlation_path.read_text())
        endpoints = ", ".join(f"`{key}` ({value})" for key, value in correlation["provider_endpoint_ids"].items())
        models = ", ".join(f"`{key}` ({value})" for key, value in correlation["provider_models"].items())
        lines += [
            "",
            f"All {correlation['generation_records']} inspected OpenRouter bucket generations report shared-account operation; provider endpoint: {endpoints}; exact provider model: {models}.",
        ]

    lines += [
        "",
        "## Spend and safety",
        "",
        f"- Conservative OpenAI-list-price OpenRouter debit: **${analysis['openrouter_conservative_frozen_debit_usd']:.6f}**.",
        f"- OpenRouter account-usage delta during the run: **{('$' + format(actual_spend, '.8f')) if actual_spend is not None else 'unavailable'}**.",
        "- No BYOK credential was created; every successful OpenRouter response explicitly reports `is_byok=false`.",
        "- API keys, request bodies, prompt contents, and model output are absent from result artifacts.",
        "",
        "## Local artifacts",
        "",
        "- `analysis.json`: complete distribution statistics and multiple-comparison-adjusted paired tests.",
        "- `request-id-buckets.json`: direct request IDs and OpenRouter generation IDs around p50, p95, p99, and maximum latency.",
        "- `successful-blocks.jsonl`: all success-only matched blocks.",
        "- `attempts.jsonl`: every attempt, including failures and discarded successes.",
        "- `failures.jsonl`: every replaced block and its triggering route.",
        "- `diagnostics.jsonl`: sanitized HTTP headers, SSE lifecycle timing, router metadata, and request controls.",
        "- `gateway-generation-metadata.jsonl`: selected OpenRouter generation-to-upstream response correlations.",
        "",
    ]
    target = destination / "FINDINGS.md"
    target.write_text("\n".join(lines))
    print(json.dumps({"report": str(target), "actual_openrouter_spend_usd": actual_spend}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
