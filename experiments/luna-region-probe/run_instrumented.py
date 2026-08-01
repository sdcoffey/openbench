"""Run stock Gateway Probe with local-only, secret-safe transport diagnostics."""

from __future__ import annotations

import codecs
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from obench import gateway_probe_cli, gateway_probe_http  # noqa: E402


SAFE_HEADER = re.compile(
    r"^(?:content-type|date|server|server-timing|openai-processing-ms|"
    r"openai-version|x-request-id|request-id|openai-request-id|"
    r"x-envoy-upstream-service-time|x-ratelimit-[a-z0-9-]+|"
    r"x-openrouter-[a-z0-9-]+|x-openai-[a-z0-9-]+|"
    r"cf-ray|cf-cache-status|x-cache(?:-[a-z0-9-]+)?|"
    r"x-served-by|via|transfer-encoding|content-encoding|"
    r"connection|alt-svc|x-service-tier)$",
    re.IGNORECASE,
)
BLOCKED_HEADER = re.compile(
    r"(?:authorization|cookie|token|secret|credential|organization|project)",
    re.IGNORECASE,
)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/@+=-]{1,256}$")


def safe_headers(items: list[tuple[str, str]]) -> dict[str, str]:
    captured: dict[str, str] = {}
    for raw_name, raw_value in items:
        name = raw_name.casefold()
        value = raw_value.strip()
        if (
            SAFE_HEADER.fullmatch(name)
            and not BLOCKED_HEADER.search(name)
            and len(value) <= 512
            and "\n" not in value
            and "\r" not in value
        ):
            captured[name] = value
    return captured


class ResponseTrace:
    def __init__(self, response: Any, trace: dict[str, Any]):
        self._response = response
        self._trace = trace
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def getheaders(self) -> list[tuple[str, str]]:
        headers = self._response.getheaders()
        self._trace["response_headers"] = safe_headers(headers)
        return headers

    def read1(self, size: int) -> bytes:
        chunk = self._response.read1(size)
        observed = time.monotonic()
        if chunk:
            self._trace.setdefault("body_chunks", []).append({
                "offset_s": round(observed - self._trace["request_sent_at"], 6),
                "bytes": len(chunk),
            })
            self._buffer += self._decoder.decode(chunk, final=False)
            self._buffer = self._buffer.replace("\r\n", "\n")
            while "\n\n" in self._buffer:
                raw_event, self._buffer = self._buffer.split("\n\n", 1)
                data_lines = [
                    line[5:].lstrip()
                    for line in raw_event.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                raw_data = "\n".join(data_lines)
                if raw_data == "[DONE]":
                    event: dict[str, Any] = {"type": "[DONE]"}
                else:
                    try:
                        payload = json.loads(raw_data)
                    except (json.JSONDecodeError, UnicodeError):
                        event = {"type": "malformed"}
                    else:
                        event = self._event_metadata(payload)
                event["offset_s"] = round(
                    observed - self._trace["request_sent_at"], 6
                )
                self._trace.setdefault("events", []).append(event)
        return chunk

    @staticmethod
    def _event_metadata(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"type": "non_object"}
        event = {"type": str(payload.get("type", "unknown"))[:100]}
        response = payload.get("response")
        if not isinstance(response, dict):
            response = {}
        for key in ("id", "model", "provider", "service_tier", "status"):
            value = response.get(key, payload.get(key))
            if isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value):
                event[key] = value
        for key in ("temperature", "top_p"):
            value = response.get(key, payload.get(key))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                event[key] = value
        max_output_tokens = response.get("max_output_tokens", payload.get("max_output_tokens"))
        if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool):
            event["max_output_tokens"] = max_output_tokens
        store_value = response.get("store", payload.get("store"))
        if isinstance(store_value, bool):
            event["store"] = store_value
        reasoning = response.get("reasoning", payload.get("reasoning"))
        if isinstance(reasoning, dict):
            effort = reasoning.get("effort")
            if isinstance(effort, str) and SAFE_IDENTIFIER.fullmatch(effort):
                event["reasoning_effort"] = effort
            summary = reasoning.get("summary")
            if isinstance(summary, str) and SAFE_IDENTIFIER.fullmatch(summary):
                event["reasoning_summary"] = summary
        error = response.get("error", payload.get("error"))
        if isinstance(error, dict):
            for key in ("code", "type"):
                value = error.get(key)
                if isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value):
                    event[f"error_{key}"] = value
        error_type = response.get("error_type", payload.get("error_type"))
        if isinstance(error_type, str) and SAFE_IDENTIFIER.fullmatch(error_type):
            event["error_type"] = error_type
        metadata = payload.get("openrouter_metadata")
        if not isinstance(metadata, dict):
            metadata = response.get("openrouter_metadata")
        if isinstance(metadata, dict):
            byok = metadata.get("is_byok")
            if isinstance(byok, bool):
                event["is_byok"] = byok
            for key in ("region", "strategy", "requested"):
                value = metadata.get(key)
                if isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value):
                    event[f"router_{key}"] = value
        return event


class ConnectionTrace:
    def __init__(self, connection: Any, trace: dict[str, Any]):
        self._connection = connection
        self._trace = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def request(self, *args: Any, **kwargs: Any) -> Any:
        result = self._connection.request(*args, **kwargs)
        self._trace["request_sent_at"] = time.monotonic()
        sock = getattr(self._connection, "sock", None)
        if sock is not None:
            try:
                peer = sock.getpeername()
                self._trace["peer"] = {"ip": str(peer[0]), "port": peer[1]}
            except OSError:
                pass
            for method, field in (("version", "tls_version"), ("selected_alpn_protocol", "alpn")):
                getter = getattr(sock, method, None)
                if callable(getter):
                    value = getter()
                    if isinstance(value, str):
                        self._trace[field] = value
        return result

    def getresponse(self, *args: Any, **kwargs: Any) -> ResponseTrace:
        response = self._connection.getresponse(*args, **kwargs)
        now = time.monotonic()
        self._trace["headers_offset_s"] = round(
            now - self._trace["request_sent_at"], 6
        )
        self._trace["http_status"] = response.status
        self._trace["http_version"] = response.version
        return ResponseTrace(response, self._trace)


def run(argv: list[str]) -> int:
    if not 3 <= len(argv) <= 4:
        raise SystemExit(
            "usage: run_instrumented.py EXPERIMENT OUTPUT_DIR "
            "DIAGNOSTICS_JSONL [--allow-cost-unavailable-block-recovery]"
        )
    experiment_path, output_dir, diagnostics_path, *options = argv
    if options and options != ["--allow-cost-unavailable-block-recovery"]:
        raise SystemExit("unsupported benchmark option")
    destination = Path(diagnostics_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    original_body = gateway_probe_http.request_body
    original_consume = gateway_probe_http._consume
    original_execute = gateway_probe_http.execute_request
    context: dict[str, Any] = {}
    measured = 0

    def traced_body(*args: Any, **kwargs: Any) -> bytes:
        payload = json.loads(original_body(*args, **kwargs))
        payload["reasoning"] = {"effort": "none"}
        plan = kwargs.get("plan") if "plan" in kwargs else args[2]
        if plan.arm_id == "direct-openrouter-shape":
            original_input = payload["input"]
            payload["input"] = [{"role": "user", "content": [{"type": "input_text", "text": original_input}]}]
            payload.pop("temperature", None)
            payload.pop("top_p", None)
            payload["truncation"] = "disabled"
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def traced_consume(connection: Any, path: str, body: bytes, headers: Any, plan: Any, **kwargs: Any) -> Any:
        index = context.get("consumes", 0)
        context["consumes"] = index + 1
        role = "primer" if context.get("condition") == "warm" and index == 0 else "measured"
        payload = json.loads(body)
        if plan.arm_id == "openrouter-byok":
            headers["X-OpenRouter-Metadata"] = "enabled"
        trace: dict[str, Any] = {
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "arm": plan.arm_id,
            "condition": context.get("condition"),
            "repetition": context.get("repetition"),
            "role": role,
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "request_controls": {
                "model": payload.get("model"),
                "reasoning": payload.get("reasoning"),
                "max_output_tokens": payload.get("max_output_tokens"),
                "temperature": payload.get("temperature"),
                "top_p": payload.get("top_p"),
                "provider": payload.get("provider"),
            },
        }
        proxy = ConnectionTrace(connection, trace)
        try:
            return original_consume(proxy, path, body, headers, plan, **kwargs)
        finally:
            trace.pop("request_sent_at", None)
            with destination.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def traced_execute(**kwargs: Any) -> Any:
        nonlocal measured
        block = kwargs["block"]
        context.update(condition=block.condition, repetition=block.repetition, consumes=0)
        try:
            result = original_execute(**kwargs)
            measured += 1
            if measured == 1 or measured % 20 == 0:
                print(
                    f"progress measured={measured} condition={block.condition} "
                    f"repetition={block.repetition} arm={kwargs['plan'].arm_id} "
                    f"status={result['outcome']['http_status']} "
                    f"route={result['route_integrity']['status']}",
                    flush=True,
                )
            return result
        finally:
            context.clear()

    gateway_probe_http.request_body = traced_body
    gateway_probe_http._consume = traced_consume
    gateway_probe_http.execute_request = traced_execute
    return gateway_probe_cli.main([
        "benchmark", experiment_path, "--output-dir", output_dir, *options
    ])


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
