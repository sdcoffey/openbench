"""Record safe cloud-region and Cloudflare-edge provenance for a probe run."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent


def get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 2.0) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers or {})
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def azure_region() -> dict[str, Any] | None:
    try:
        payload = json.loads(get(
            "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
            headers={"Metadata": "true"},
        ))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return {
        "provider": "azure",
        "region": payload.get("location"),
        "zone": payload.get("zone"),
        "vm_size": payload.get("vmSize"),
    }


def gcp_region() -> dict[str, Any] | None:
    try:
        zone = get(
            "http://169.254.169.254/computeMetadata/v1/instance/zone",
            headers={"Metadata-Flavor": "Google"},
        ).decode().rsplit("/", 1)[-1]
    except (OSError, urllib.error.URLError, UnicodeDecodeError):
        return None
    return {
        "provider": "gcp",
        "region": zone.rsplit("-", 1)[0],
        "zone": zone,
    }


def cloudflare_edge(host: str) -> dict[str, str | None]:
    try:
        raw = get(f"https://{host}/cdn-cgi/trace", timeout=5).decode()
    except (OSError, urllib.error.URLError, UnicodeDecodeError):
        return {"host": host, "status": "unavailable"}
    fields = {}
    for line in raw.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            fields[name] = value
    return {
        "host": host,
        "cloudflare_colo": fields.get("colo"),
        "country": fields.get("loc"),
        "http_version": fields.get("http"),
        "tls_version": fields.get("tls"),
        "egress_ip_sha256": (
            hashlib.sha256(fields["ip"].encode()).hexdigest()[:16]
            if fields.get("ip")
            else None
        ),
    }


def main() -> int:
    region = azure_region() or gcp_region() or {"provider": "unknown", "region": None}
    result = {
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "machine": {
            "hostname_sha256": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16],
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
        "cloud": region,
        "edges": [
            cloudflare_edge("api.openai.com"),
            cloudflare_edge("openrouter.ai"),
        ],
    }
    output = BASE / "large-three-arm" / "region.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
