"""Inspect OpenRouter request translation without persisting prompt/output content."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
SAFE_VALUE_KEYS = {
    "allow_fallbacks", "background", "frequency_penalty", "include_usage",
    "max_completion_tokens", "max_output_tokens", "max_tokens", "model", "parallel_tool_calls",
    "presence_penalty", "reasoning_effort", "seed", "service_tier", "store", "stream",
    "temperature", "top_p", "truncation",
}


def describe(value: Any, key: str = "") -> Any:
    if key in {"input", "messages", "instructions", "prompt", "content", "text"}:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
        return {"type": type(value).__name__, "serialized_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    if isinstance(value, dict):
        return {name: describe(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [describe(item, key) for item in value[:5]]
    if key in SAFE_VALUE_KEYS or key == "effort":
        return value
    return {"type": type(value).__name__}


def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is unavailable")
    payload = {
        "debug": {"echo_upstream_body": True},
        "input": "[openbench_probe_nonce:translation-debug] Reply with exactly six ordinary words describing a blue circle.",
        "max_output_tokens": 32,
        "model": "openai/gpt-5.6-luna",
        "provider": {"only": ["openai"], "allow_fallbacks": False},
        "reasoning": {"effort": "none"},
        "store": False,
        "stream": True,
        "temperature": 0,
        "top_p": 1,
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/responses",
        data=json.dumps(payload, sort_keys=True).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    started = time.monotonic()
    result: dict[str, Any] = {
        "sent_body_structure": describe(payload),
        "events": [],
    }
    with urllib.request.urlopen(request, timeout=45) as response:
        result["http_status"] = response.status
        result["headers_s"] = round(time.monotonic() - started, 6)
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                result["events"].append({"type": "[DONE]", "offset_s": round(time.monotonic() - started, 6)})
                continue
            try:
                item = json.loads(data)
            except json.JSONDecodeError:
                continue
            event: dict[str, Any] = {"type": item.get("type"), "offset_s": round(time.monotonic() - started, 6)}
            response_object = item.get("response", {})
            if isinstance(response_object, dict):
                for key in ("id", "model", "service_tier", "status"):
                    if key in response_object:
                        event[key] = response_object[key]
                if isinstance(response_object.get("usage"), dict):
                    usage = response_object["usage"]
                    result["usage"] = {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
                    }
            result["events"].append(event)
            debug = item.get("debug", {})
            if isinstance(debug, dict) and isinstance(debug.get("echo_upstream_body"), dict):
                result["upstream_body_structure"] = describe(debug["echo_upstream_body"])
    (BASE / "openrouter-upstream-body.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "http_status": result["http_status"],
        "upstream_keys": sorted(result.get("upstream_body_structure", {})),
        "events": [item.get("type") for item in result["events"]],
        "usage": result.get("usage"),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
