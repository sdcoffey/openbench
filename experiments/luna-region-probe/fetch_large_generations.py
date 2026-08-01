"""Recover upstream OpenAI response IDs for representative gateway buckets."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from fetch_generation_metadata import safe_generation  # noqa: E402


def main(argv: list[str]) -> int:
    destination = Path(argv[0]) if argv else BASE / "large-three-arm"
    buckets = json.loads((destination / "request-id-buckets.json").read_text())
    choices = {}
    for name, groups in buckets.items():
        if not name.startswith("openrouter-shared:"):
            continue
        for bucket, rows in groups.items():
            for row in rows:
                generation_id = row.get("response_id")
                if isinstance(generation_id, str):
                    choices.setdefault(generation_id, []).append(f"{name}:{bucket}")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is unavailable")
    output = destination / "gateway-generation-metadata.jsonl"
    existing = {}
    if output.exists():
        for line in output.open():
            item = json.loads(line)
            existing[item["id"]] = item
    pending = {identifier: names for identifier, names in choices.items() if identifier not in existing}
    for round_number in range(1, 5):
        if not pending:
            break
        remaining = {}
        for generation_id, names in pending.items():
            url = "https://openrouter.ai/api/v1/generation?" + urllib.parse.urlencode({"id": generation_id})
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.load(response)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                remaining[generation_id] = names
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                remaining[generation_id] = names
                continue
            item = safe_generation(data)
            item["buckets"] = names
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        pending = remaining
        print(json.dumps({"round": round_number, "recovered": len(choices) - len(pending), "total": len(choices)}), flush=True)
        if pending and round_number < 4:
            time.sleep(min(round_number * 3, 8))
    (destination / "gateway-generation-metadata-summary.json").write_text(json.dumps({
        "selected": len(choices),
        "recovered": len(choices) - len(pending),
        "unresolved": sorted(pending),
    }, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
