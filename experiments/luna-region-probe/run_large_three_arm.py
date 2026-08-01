"""Run resumable, sampling-matched personal/internal/OpenRouter Luna blocks.

This ignored local wrapper deliberately preserves stock Gateway Probe HTTP,
SSE, pricing, warm-socket, and route-verification behavior.  The published
Gateway Probe schema admits exactly one direct arm, so the second direct
account is represented by a derived in-memory RoutePlan rather than a tracked
schema change.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextvars
import dataclasses
import datetime as dt
import hashlib
import json
import os
import random
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BASE))

from obench import gateway_probe_http, gateway_probe_spec, gateway_run, gateway_spec  # noqa: E402
from obench.gateway_probe_models import ProbeBlock  # noqa: E402
from run_instrumented import ConnectionTrace  # noqa: E402


PERSONAL = "direct-personal"
INTERNAL = "direct-internal"
OPENROUTER = "openrouter-shared"
ARM_IDS = (PERSONAL, INTERNAL, OPENROUTER)
CONDITIONS = ("cold", "warm")
HARD_OPENROUTER_CAP_USD = Decimal("1.95")
PRIOR_OPENROUTER_ALLOWANCE_USD = Decimal("0.01")
MAX_INFLIGHT_RESERVATION_USD = Decimal("0.001")
UNKNOWN_GATEWAY_CALL_RESERVATION_USD = Decimal("0.0005")
MAX_BLOCK_ATTEMPTS = 30
SEED = 20260804
ACTIVE_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "large_gateway_probe_context", default=None
)


def append_json(path: Path, value: dict[str, Any], lock: threading.Lock) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    for name in ("OPENAI_API_KEY", "PERSONAL_OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MGMT_KEY"):
        secret = os.environ.get(name)
        if secret and secret in encoded:
            raise RuntimeError("refusing to persist an API credential")
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class SharedState:
    def __init__(self, destination: Path, target_per_condition: int):
        self.destination = destination
        self.target_per_condition = target_per_condition
        self.attempts_path = destination / "attempts.jsonl"
        self.diagnostics_path = destination / "diagnostics.jsonl"
        self.success_path = destination / "successful-blocks.jsonl"
        self.failures_path = destination / "failures.jsonl"
        self.io_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.fatal = threading.Event()
        self.fatal_reason: str | None = None
        self.started_at = time.monotonic()
        self.openrouter_spent = PRIOR_OPENROUTER_ALLOWANCE_USD
        self.openrouter_reserved = Decimal(0)
        self.completed: set[tuple[str, int]] = set()
        self.previous_attempts: dict[tuple[str, int], int] = {}
        self.failures = 0
        self.attempted_requests = 0

        if self.attempts_path.exists():
            with self.attempts_path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    self.attempted_requests += 1
                    coordinate = (str(row["condition"]), int(row["pair_index"]))
                    self.previous_attempts[coordinate] = max(
                        self.previous_attempts.get(coordinate, 0),
                        int(row["block_attempt"]),
                    )
                    if row.get("arm") == OPENROUTER:
                        self.openrouter_spent += Decimal(str(row.get("openrouter_budget_debit_usd", "0")))
        if self.success_path.exists():
            with self.success_path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    coordinate = (str(row["condition"]), int(row["pair_index"]))
                    if coordinate in self.completed:
                        raise RuntimeError(f"duplicate successful block: {coordinate}")
                    self.completed.add(coordinate)
        if self.failures_path.exists():
            with self.failures_path.open(encoding="utf-8") as handle:
                self.failures = sum(1 for _ in handle)

    def stop(self, message: str) -> None:
        with self.state_lock:
            if not self.fatal.is_set():
                self.fatal_reason = message
                self.fatal.set()

    def reserve_gateway(self) -> None:
        with self.state_lock:
            prospective = (
                self.openrouter_spent
                + self.openrouter_reserved
                + MAX_INFLIGHT_RESERVATION_USD
            )
            if prospective >= HARD_OPENROUTER_CAP_USD:
                self.stop("OpenRouter conservative hard spending boundary reached")
                raise RuntimeError("OpenRouter spending boundary reached")
            self.openrouter_reserved += MAX_INFLIGHT_RESERVATION_USD

    def release_gateway(self, actual: Decimal) -> None:
        with self.state_lock:
            self.openrouter_reserved -= MAX_INFLIGHT_RESERVATION_USD
            self.openrouter_spent += actual

    def recorded_request(self) -> None:
        with self.state_lock:
            self.attempted_requests += 1

    def recorded_failure(self) -> None:
        with self.state_lock:
            self.failures += 1

    def recorded_success(self, coordinate: tuple[str, int]) -> tuple[int, int, int, Decimal]:
        with self.state_lock:
            if coordinate in self.completed:
                raise RuntimeError(f"successful block already recorded: {coordinate}")
            self.completed.add(coordinate)
            cold = sum(condition == "cold" for condition, _ in self.completed)
            warm = len(self.completed) - cold
            return cold, warm, self.failures, self.openrouter_spent


def install_instrumentation(state: SharedState) -> None:
    original_body = gateway_probe_http.request_body
    original_consume = gateway_probe_http._consume

    def matched_body(*args: Any, **kwargs: Any) -> bytes:
        payload = json.loads(original_body(*args, **kwargs))
        payload.pop("temperature", None)
        payload.pop("top_p", None)
        payload["reasoning"] = {"effort": "none"}
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def traced_consume(connection: Any, path: str, body: bytes, headers: Any, plan: Any, **kwargs: Any) -> Any:
        current = ACTIVE_CONTEXT.get()
        if current is None:
            raise RuntimeError("request executed outside a benchmark block")
        consume_index = int(current.get("consume_count", 0))
        current["consume_count"] = consume_index + 1
        role = "primer" if current["condition"] == "warm" and consume_index == 0 else "measured"
        if current["arm"] == OPENROUTER:
            headers["X-OpenRouter-Metadata"] = "enabled"
        decoded = json.loads(body)
        trace: dict[str, Any] = {
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "arm": current["arm"],
            "condition": current["condition"],
            "pair_index": current["pair_index"],
            "block_attempt": current["block_attempt"],
            "worker": threading.current_thread().name,
            "role": role,
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "request_controls": {
                "model": decoded.get("model"),
                "reasoning": decoded.get("reasoning"),
                "max_output_tokens": decoded.get("max_output_tokens"),
                "temperature_present": "temperature" in decoded,
                "top_p_present": "top_p" in decoded,
                "provider": decoded.get("provider"),
            },
        }
        proxy = ConnectionTrace(connection, trace)
        try:
            return original_consume(proxy, path, body, headers, plan, **kwargs)
        finally:
            trace.pop("request_sent_at", None)
            current.setdefault("traces", []).append(trace)
            append_json(state.diagnostics_path, trace, state.io_lock)

    gateway_probe_http.request_body = matched_body
    gateway_probe_http._consume = traced_consume


def slim_sample(arm: str, result: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("request_metrics", {})
    timing = metrics.get("timing", {})
    usage = metrics.get("usage") or {}
    headers = trace.get("response_headers", {})
    events = trace.get("events", [])
    created = next((item for item in events if item.get("type") == "response.created"), {})
    completed = next((item for item in events if item.get("type") == "response.completed"), {})
    failed = next((item for item in events if item.get("type") == "response.failed"), {})
    return {
        "arm": arm,
        "success": result.get("outcome", {}).get("success") is True,
        "route_verified": result.get("route_integrity", {}).get("pass") is True,
        "http_status": result.get("outcome", {}).get("http_status"),
        "error_class": result.get("outcome", {}).get("error_class"),
        "error_detail": result.get("outcome", {}).get("error_detail"),
        "error_code": failed.get("error_code"),
        "request_id": headers.get("x-request-id"),
        "response_id": created.get("id", completed.get("id", failed.get("id"))),
        "cf_ray": headers.get("cf-ray"),
        "openai_processing_ms": headers.get("openai-processing-ms"),
        "header_s": timing.get("request_to_response_headers_s"),
        "first_body_s": timing.get("request_to_first_body_byte_s"),
        "ttft_s": timing.get("request_to_semantic_ttft_s"),
        "total_s": timing.get("request_stream_total_s"),
        "cold_e2e_ttft_s": timing.get("cold_end_to_end_semantic_ttft_s"),
        "cold_e2e_total_s": timing.get("cold_end_to_end_stream_total_s"),
        "created_event_s": created.get("offset_s"),
        "completed_event_s": completed.get("offset_s"),
        "model": completed.get("model", created.get("model")),
        "initial_service_tier": created.get("service_tier"),
        "final_service_tier": completed.get("service_tier", failed.get("service_tier")),
        "reasoning_effort": completed.get("reasoning_effort", created.get("reasoning_effort")),
        "effective_temperature": completed.get("temperature", created.get("temperature")),
        "effective_top_p": completed.get("top_p", created.get("top_p")),
        "is_byok": completed.get("is_byok"),
        "router_region": completed.get("router_region"),
        "router_strategy": completed.get("router_strategy"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        "primer_completed": result.get("reuse_evidence", {}).get("completed"),
        "socket_reused": result.get("reuse_evidence", {}).get("socket_reused"),
        "frozen_budget_debit_usd": result.get("billing", {}).get("budget_debit_usd"),
        "header_names": sorted(headers),
    }


def deterministic_order(condition: str, pair_index: int, block_attempt: int) -> tuple[str, ...]:
    payload = f"{SEED}:{condition}:{pair_index}:{block_attempt}".encode()
    local_seed = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    values = list(ARM_IDS)
    random.Random(local_seed).shuffle(values)
    return tuple(values)


def fatal_outcome(sample: dict[str, Any]) -> bool:
    return bool(
        sample.get("http_status") in {400, 401, 403, 404}
        or sample.get("error_code") in {"invalid_api_key", "model_not_found", "insufficient_quota"}
    )


def run_block(
    state: SharedState,
    experiment: gateway_probe_spec.GatewayProbeExperiment,
    plans: dict[str, gateway_spec.RoutePlan],
    secrets: dict[str, str],
    prices: dict[str, gateway_run.Price],
    condition: str,
    pair_index: int,
) -> None:
    coordinate = (condition, pair_index)
    if coordinate in state.completed or state.fatal.is_set():
        return
    case = experiment.cases[0]
    block_started_at = time.monotonic()
    initial_attempt = state.previous_attempts.get(coordinate, 0) + 1
    for block_attempt in range(initial_attempt, MAX_BLOCK_ATTEMPTS + 1):
        if state.fatal.is_set():
            return
        arm_order = deterministic_order(condition, pair_index, block_attempt)
        block = ProbeBlock(
            case.case_id,
            case.prompt_digest,
            condition,
            pair_index + (block_attempt - 1) * 1_000_000,
            arm_order,
        )
        samples: dict[str, dict[str, Any]] = {}
        failed_sample: dict[str, Any] | None = None
        for arm in arm_order:
            if state.fatal.is_set():
                return
            reserved = arm == OPENROUTER
            if reserved:
                state.reserve_gateway()
            context = {
                "arm": arm,
                "condition": condition,
                "pair_index": pair_index,
                "block_attempt": block_attempt,
                "consume_count": 0,
                "traces": [],
            }
            context_token = ACTIVE_CONTEXT.set(context)
            operation_started = time.monotonic()
            debit = Decimal(0)
            try:
                result = gateway_probe_http.execute_request(
                    experiment=experiment,
                    case=case,
                    block=block,
                    plan=plans[arm],
                    secret=secrets[arm],
                    prices=prices,
                    remaining_usd_cap=None,
                )
                measured = next((trace for trace in context["traces"] if trace.get("role") == "measured"), {})
                sample = slim_sample(arm, result, measured)
                if arm == OPENROUTER and sample.get("success") and sample.get("is_byok") is not False:
                    sample["success"] = False
                    sample["error_class"] = "route"
                    sample["error_code"] = "shared_provider_not_confirmed"
                if sample.get("success") and not sample.get("route_verified"):
                    sample["success"] = False
                    sample["error_class"] = "route"
                    sample["error_code"] = "route_not_verified"
                if sample.get("success") and sample.get("reasoning_effort") != "none":
                    sample["success"] = False
                    sample["error_class"] = "controls"
                    sample["error_code"] = "reasoning_not_none"
                if sample.get("success") and condition == "warm" and sample.get("socket_reused") is not True:
                    sample["success"] = False
                    sample["error_class"] = "controls"
                    sample["error_code"] = "warm_socket_not_reused"
                if reserved:
                    debit = Decimal(str(result["billing"]["budget_debit_usd"]))
                    if result["billing"].get("cost_status") != "observed":
                        debit = max(debit, UNKNOWN_GATEWAY_CALL_RESERVATION_USD)
                attempt_row = {
                    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "condition": condition,
                    "pair_index": pair_index,
                    "block_attempt": block_attempt,
                    "arm_order": list(arm_order),
                    "arm": arm,
                    "sample": sample,
                    "result": result,
                    "wall_duration_s": round(time.monotonic() - operation_started, 6),
                    "openrouter_budget_debit_usd": str(debit),
                }
                append_json(state.attempts_path, attempt_row, state.io_lock)
                state.recorded_request()
                samples[arm] = sample
                if not sample["success"]:
                    failed_sample = sample
                    break
            finally:
                ACTIVE_CONTEXT.reset(context_token)
                if reserved:
                    state.release_gateway(debit)

        if failed_sample is not None:
            failure_row = {
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "condition": condition,
                "pair_index": pair_index,
                "block_attempt": block_attempt,
                "arm_order": list(arm_order),
                "failed_arm": failed_sample["arm"],
                "failed_sample": failed_sample,
                "completed_arms": list(samples),
            }
            append_json(state.failures_path, failure_row, state.io_lock)
            state.recorded_failure()
            print(
                f"replacing_block condition={condition} index={pair_index} "
                f"attempt={block_attempt} failed_arm={failed_sample['arm']} "
                f"http={failed_sample.get('http_status')} error={failed_sample.get('error_code')}",
                flush=True,
            )
            if fatal_outcome(failed_sample):
                state.stop(f"non-retryable failure on {failed_sample['arm']}: {failed_sample}")
                return
            time.sleep(min(block_attempt * 0.1, 1.0))
            continue

        if len(samples) != len(ARM_IDS):
            state.stop("matched block ended without all three successful arms")
            return

        success = {
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "condition": condition,
            "pair_index": pair_index,
            "block_attempt": block_attempt,
            "arm_order": list(arm_order),
            "elapsed_since_first_attempt_s": round(time.monotonic() - block_started_at, 6),
            "samples": samples,
        }
        append_json(state.success_path, success, state.io_lock)
        cold, warm, failures, spent = state.recorded_success(coordinate)
        completed = cold + warm
        if completed == 1 or completed % 20 == 0:
            elapsed = time.monotonic() - state.started_at
            rate = completed / max(elapsed, 0.001)
            remaining = max(state.target_per_condition * 2 - completed, 0)
            print(
                f"progress successful_blocks={completed}/{state.target_per_condition * 2} "
                f"cold={cold} warm={warm} replaced_blocks={failures} "
                f"requests={state.attempted_requests} "
                f"openrouter_conservative_usd={spent:.6f} "
                f"elapsed_min={elapsed / 60:.1f} eta_min={remaining / max(rate, 0.001) / 60:.1f}",
                flush=True,
            )
        return

    state.stop(f"matched block exceeded {MAX_BLOCK_ATTEMPTS} attempts: {coordinate}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=BASE / "large-three-arm")
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.samples <= 10_000:
        raise SystemExit("--samples must be between 1 and 10,000")
    if not 1 <= arguments.workers <= 12:
        raise SystemExit("--workers must be between 1 and 12")
    for name in ("OPENAI_API_KEY", "PERSONAL_OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        if not os.environ.get(name):
            raise SystemExit(f"required credential missing: {name}")
    if os.environ["OPENAI_API_KEY"] == os.environ["PERSONAL_OPENAI_API_KEY"]:
        raise SystemExit("internal and personal direct credentials must differ")

    arguments.output.mkdir(parents=True, exist_ok=True)
    experiment = gateway_probe_spec.load_experiment(BASE / "luna-full.toml")
    original_plans, original_secrets = gateway_probe_spec.compile_route_plans(
        experiment,
        environ=os.environ,
        admitted_auth_envs={"PERSONAL_OPENAI_API_KEY", "OPENROUTER_API_KEY"},
    )
    by_id = {plan.arm_id: plan for plan in original_plans}
    original_personal = by_id["direct-openai"]
    plans = {
        PERSONAL: dataclasses.replace(
            original_personal,
            arm_id=PERSONAL,
            arm_digest=gateway_spec.canonical_digest({"base": original_personal.arm_digest, "account": "personal"}),
        ),
        INTERNAL: dataclasses.replace(
            original_personal,
            arm_id=INTERNAL,
            auth_env="OPENAI_API_KEY",
            arm_digest=gateway_spec.canonical_digest({"base": original_personal.arm_digest, "account": "internal"}),
        ),
        OPENROUTER: dataclasses.replace(
            by_id["openrouter-openai"],
            arm_id=OPENROUTER,
            arm_digest=gateway_spec.canonical_digest({"base": by_id["openrouter-openai"].arm_digest, "account": "shared"}),
        ),
    }
    secrets = {
        PERSONAL: original_secrets.value_for("direct-openai"),
        INTERNAL: os.environ["OPENAI_API_KEY"],
        OPENROUTER: original_secrets.value_for("openrouter-openai"),
    }
    prices = {
        "openai/gpt-5.6-luna": gateway_run.Price(
            Decimal("0.20"), Decimal("1.20"), "2026-07-30T00:00:00Z"
        )
    }
    state = SharedState(arguments.output, arguments.samples)
    install_instrumentation(state)

    manifest = {
        "experiment": "large-three-arm-shared-account",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": "gpt-5.6-luna",
        "reasoning_effort": "none",
        "sampling_controls": "omitted_equally_on_all_arms",
        "target_successful_blocks_per_condition": arguments.samples,
        "workers": arguments.workers,
        "arms": list(ARM_IDS),
        "openrouter_account": "shared_provider_only",
        "openrouter_hard_cap_usd": str(HARD_OPENROUTER_CAP_USD),
        "warm_primers": "fresh_same_socket_primer_per_arm_per_block_attempt",
        "failure_policy": "discard_entire_block_and_retry_with_fresh_nonce",
        "stock_harness_limitation": "published_gateway_probe_schema_allows_only_one_direct_arm",
    }
    manifest_path = arguments.output / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing.get("target_successful_blocks_per_condition") != arguments.samples:
            raise SystemExit("resume target differs from existing large-run manifest")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    randomizer = random.Random(SEED)
    indexes = {condition: list(range(1, arguments.samples + 1)) for condition in CONDITIONS}
    for values in indexes.values():
        randomizer.shuffle(values)
    jobs = [
        (condition, indexes[condition][index])
        for index in range(arguments.samples)
        for condition in CONDITIONS
        if (condition, indexes[condition][index]) not in state.completed
    ]
    print(
        f"starting_large_run jobs={len(jobs)} resumed_successes={len(state.completed)} "
        f"workers={arguments.workers} prior_gateway_conservative_usd={state.openrouter_spent:.6f}",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=arguments.workers, thread_name_prefix="luna-probe"
    ) as executor:
        futures = {
            executor.submit(
                run_block, state, experiment, plans, secrets, prices, condition, pair_index
            ): (condition, pair_index)
            for condition, pair_index in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - abort all benchmark workers safely
                state.stop(f"worker failed on {futures[future]}: {type(exc).__name__}: {exc}")
            if state.fatal.is_set():
                for pending in futures:
                    pending.cancel()
                break

    if state.fatal.is_set():
        raise SystemExit(state.fatal_reason or "benchmark stopped")
    expected = arguments.samples * len(CONDITIONS)
    if len(state.completed) != expected:
        raise SystemExit(f"incomplete benchmark: {len(state.completed)}/{expected} blocks")
    summary = {
        "successful_blocks": len(state.completed),
        "successful_blocks_per_condition": {
            condition: sum(item[0] == condition for item in state.completed)
            for condition in CONDITIONS
        },
        "replaced_blocks": state.failures,
        "attempted_requests": state.attempted_requests,
        "openrouter_conservative_spend_usd": str(state.openrouter_spent),
        "elapsed_seconds": round(time.monotonic() - state.started_at, 3),
    }
    (arguments.output / "run-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
