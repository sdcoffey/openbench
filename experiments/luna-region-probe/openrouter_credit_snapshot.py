"""Store a safe account-usage snapshot without recording key identifiers."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path


BASE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in {"before", "during", "after"}:
        raise SystemExit("usage: openrouter_credit_snapshot.py before|during|after")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is unavailable")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        decoded = json.load(response)
    data = decoded.get("data")
    if not isinstance(data, dict):
        raise SystemExit("OpenRouter returned malformed key metadata")
    safe = {
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stage": argv[0],
        "usage": data.get("usage"),
        "usage_daily": data.get("usage_daily"),
        "usage_weekly": data.get("usage_weekly"),
        "usage_monthly": data.get("usage_monthly"),
        "byok_usage": data.get("byok_usage"),
        "limit": data.get("limit"),
        "limit_remaining": data.get("limit_remaining"),
        "is_free_tier": data.get("is_free_tier"),
    }
    destination = BASE / "large-three-arm" / f"openrouter-credit-{argv[0]}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n")
    print(json.dumps(safe, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
