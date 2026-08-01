"""Add upstream response/request correlations to gateway latency buckets."""

from __future__ import annotations

import collections
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
DESTINATION = BASE / "large-three-arm"


def main() -> int:
    metadata_path = DESTINATION / "gateway-generation-metadata.jsonl"
    generations = {
        row["id"]: row
        for line in metadata_path.open()
        if isinstance((row := json.loads(line)).get("id"), str)
    }
    routing_path = DESTINATION / "routing-final-openrouter-shared.json"
    routing = json.loads(routing_path.read_text()) if routing_path.exists() else {}
    records = routing.get("arms", {}).get("openrouter-shared", {}).get("records", [])
    upstream_to_request = {
        row.get("response_id"): row.get("request_id")
        for row in records
        if isinstance(row.get("response_id"), str)
        and isinstance(row.get("request_id"), str)
    }
    buckets_path = DESTINATION / "request-id-buckets.json"
    buckets = json.loads(buckets_path.read_text())
    enriched = 0
    real_request_ids = 0
    for bucket in buckets.values():
        for rows in bucket.values():
            for row in rows:
                if row.get("arm") != "openrouter-shared":
                    continue
                generation = generations.get(row.get("response_id"))
                if not generation:
                    continue
                row["upstream_response_id"] = generation.get("upstream_id")
                row["provider_endpoint_id"] = generation.get("endpoint_id")
                row["provider_model"] = generation.get("provider_model")
                row["generation_cost_usd"] = generation.get("total_cost_usd")
                enriched += 1
                if request_id := upstream_to_request.get(row.get("upstream_response_id")):
                    row["openai_request_id"] = request_id
                    real_request_ids += 1
    buckets_path.write_text(json.dumps(buckets, indent=2, sort_keys=True) + "\n")
    summary = {
        "generation_records": len(generations),
        "bucket_entries_enriched": enriched,
        "bucket_entries_with_real_openai_request_id": real_request_ids,
        "provider_endpoint_ids": dict(collections.Counter(str(row.get("endpoint_id")) for row in generations.values())),
        "provider_models": dict(collections.Counter(str(row.get("provider_model")) for row in generations.values())),
        "byok_values": dict(collections.Counter(str(row.get("is_byok")) for row in generations.values())),
        "generation_costs_usd": round(sum(float(row.get("total_cost_usd") or 0) for row in generations.values()), 8),
    }
    (DESTINATION / "gateway-correlation-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
