"""Recover safe OpenRouter generation metadata; never persist payload or secrets."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent


def safe_generation(data: dict[str, Any]) -> dict[str, Any]:
    provider_responses = data.get("provider_responses")
    provider = provider_responses[0] if isinstance(provider_responses, list) and provider_responses else {}
    return {
        "id": data.get("id"),
        "upstream_id": data.get("upstream_id") or provider.get("id"),
        "openrouter_request_id": data.get("request_id"),
        "provider_name": data.get("provider_name"),
        "model": data.get("model"),
        "service_tier": data.get("service_tier"),
        "latency_ms": data.get("latency"),
        "generation_time_ms": data.get("generation_time"),
        "provider_latency_ms": provider.get("latency"),
        "provider_status": provider.get("status"),
        "provider_model": provider.get("model_permaslug"),
        "endpoint_id": provider.get("endpoint_id"),
        "is_byok": provider.get("is_byok"),
        "native_input_tokens": data.get("native_tokens_prompt"),
        "native_output_tokens": data.get("native_tokens_completion"),
        "native_reasoning_tokens": data.get("native_tokens_reasoning"),
        "native_cached_tokens": data.get("native_tokens_cached"),
        "total_cost_usd": data.get("total_cost"),
    }


def main(argv: list[str]) -> int:
    arm = next((value.split("=", 1)[1] for value in argv if value.startswith("--arm=")), "openrouter-openai")
    paths = [value for value in argv if not value.startswith("--")]
    diagnostics_path = Path(paths[0]) if paths else BASE / "diagnostics-full-v2.jsonl"
    destination = Path(paths[1]) if len(paths) > 1 else BASE / "openrouter-generations.jsonl"
    include_primers = "--include-primers" in argv
    existing: set[str] = set()
    if destination.exists():
        for line in destination.open(encoding="utf-8"):
            item = json.loads(line)
            if isinstance(item.get("id"), str):
                existing.add(item["id"])
    selected: list[tuple[str, dict[str, Any]]] = []
    for line in diagnostics_path.open(encoding="utf-8"):
        trace = json.loads(line)
        if trace.get("arm") != arm:
            continue
        if trace.get("role") != "measured" and not include_primers:
            continue
        created = next((event for event in trace.get("events", []) if event.get("type") == "response.created"), {})
        generation_id = created.get("id")
        if isinstance(generation_id, str) and generation_id not in existing:
            selected.append((generation_id, trace))
            existing.add(generation_id)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is unavailable")
    succeeded = 0
    failures: list[dict[str, Any]] = []
    for index, (generation_id, trace) in enumerate(selected, start=1):
        url = "https://openrouter.ai/api/v1/generation?" + urllib.parse.urlencode({"id": generation_id})
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            failures.append({"id": generation_id, "status": getattr(exc, "code", None), "error_type": type(exc).__name__})
            continue
        data = payload.get("data", {})
        if not isinstance(data, dict):
            failures.append({"id": generation_id, "error_type": "invalid_response"})
            continue
        item = safe_generation(data)
        item.update({
            "arm": trace.get("arm"),
            "condition": trace.get("condition"),
            "repetition": trace.get("repetition"),
            "role": trace.get("role"),
        })
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        succeeded += 1
        if index == 1 or index % 20 == 0:
            print(f"generation_metadata completed={index}/{len(selected)} recovered={succeeded}", flush=True)
        time.sleep(0.03)
    errors_path = BASE / ("openrouter-generation-errors.json" if arm == "openrouter-openai" else f"{arm}-generation-errors.json")
    errors_path.write_text(
        json.dumps({"attempted": len(selected), "recovered": succeeded, "failures": failures}, indent=2) + "\n"
    )
    print(json.dumps({"attempted": len(selected), "recovered": succeeded, "failures": len(failures)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
