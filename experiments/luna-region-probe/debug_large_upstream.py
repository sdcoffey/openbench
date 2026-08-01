"""Verify gateway-visible sampling fields against the actual upstream body."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from debug_upstream import describe  # noqa: E402


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is unavailable")
    body = {
        "debug": {"echo_upstream_body": True},
        "input": "[openbench_probe_nonce:large-sampling-parity] Reply with exactly six ordinary words describing a blue circle.",
        "max_output_tokens": 32,
        "model": "openai/gpt-5.6-luna",
        "provider": {"only": ["openai"], "allow_fallbacks": False},
        "reasoning": {"effort": "none"},
        "store": False,
        "stream": True,
    }
    outbound = urllib.request.Request(
        "https://openrouter.ai/api/v1/responses",
        data=json.dumps(body, sort_keys=True).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-OpenRouter-Metadata": "enabled",
        },
    )
    observed: dict[str, object] = {"sent_body_structure": describe(body)}
    with urllib.request.urlopen(outbound, timeout=45) as response:
        observed["http_status"] = response.status
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            value = event.get("response")
            if isinstance(value, dict) and event.get("type") in {"response.created", "response.completed"}:
                observed[f"{event['type']}_sampling"] = {
                    "temperature": value.get("temperature"),
                    "top_p": value.get("top_p"),
                }
            debug = event.get("debug")
            if isinstance(debug, dict) and isinstance(debug.get("echo_upstream_body"), dict):
                upstream = debug["echo_upstream_body"]
                observed["upstream_body_structure"] = describe(upstream)
                observed["upstream_temperature_present"] = "temperature" in upstream
                observed["upstream_top_p_present"] = "top_p" in upstream
    destination = BASE / "large-three-arm-smoke" / "upstream-request-shape.json"
    destination.write_text(json.dumps(observed, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "http_status": observed.get("http_status"),
        "upstream_temperature_present": observed.get("upstream_temperature_present"),
        "upstream_top_p_present": observed.get("upstream_top_p_present"),
        "gateway_created_sampling": observed.get("response.created_sampling"),
        "gateway_completed_sampling": observed.get("response.completed_sampling"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
