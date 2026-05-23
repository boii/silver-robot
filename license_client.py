"""Service client for Python 3.9+.

Signed RPC client for the project's gating service. Every response carries
an HMAC-SHA256 signature that is verified locally against a shared secret
before being trusted. Only depends on `requests`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import time
import uuid
from typing import Any

import requests


class LicenseError(RuntimeError):
    """Raised when a response is invalid or its signature can't be verified."""


class LicenseClient:
    def __init__(
        self,
        api_url: str,
        signing_key: str,
        product: str,
        *,
        config_dir: pathlib.Path | None = None,
        timeout: float = 10.0,
        offline_grace_days: int = 7,
    ):
        self.api_url = api_url.rstrip("/")
        self.signing_key = signing_key.encode()
        self.product = product
        self.timeout = timeout
        self.offline_grace_seconds = offline_grace_days * 86400
        self.config_dir = config_dir or (pathlib.Path.home() / ".config" / product)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    # ----- public API -----

    def activate(self, key: str, fingerprint: str | None = None) -> dict[str, Any]:
        return self._call("/v1/activate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
            "fingerprint": fingerprint,
        })

    def validate(self, key: str) -> dict[str, Any]:
        return self._call("/v1/validate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
        })

    def deactivate(self, key: str) -> dict[str, Any]:
        return self._call("/v1/deactivate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
        })

    def check(self, key: str) -> bool:
        try:
            res = self.validate(key)
        except requests.RequestException:
            return self._within_grace()
        if res.get("valid"):
            self._touch_last_ok()
            return True
        return False

    def machine_id(self) -> str:
        """Stable per-machine id, persisted under the user's state dir."""
        p = self.config_dir / "m.id"
        if not p.exists():
            p.write_text(uuid.uuid4().hex)
        return p.read_text().strip()

    # ----- internal -----

    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in body.items() if v is not None}
        r = requests.post(self.api_url + path, json=body, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not self._verify(dict(data)):
            raise LicenseError("response signature could not be verified")
        return data

    def _verify(self, resp: dict[str, Any]) -> bool:
        sig = resp.pop("signature", "")
        raw = json.dumps(resp, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(self.signing_key, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _touch_last_ok(self) -> None:
        (self.config_dir / "h").write_text(str(int(time.time())))

    def _within_grace(self) -> bool:
        p = self.config_dir / "h"
        if not p.exists():
            return False
        try:
            last = int(p.read_text().strip())
        except ValueError:
            return False
        return (int(time.time()) - last) < self.offline_grace_seconds


# --- Quick CLI for smoke testing ---
if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["activate", "validate", "deactivate", "check"])
    ap.add_argument("key")
    ap.add_argument("--api", default=os.getenv("LICENSE_API", ""))
    ap.add_argument("--key-secret", dest="signing_key",
                    default=os.getenv("LICENSE_SIGNING_KEY", ""))
    ap.add_argument("--product", default=os.getenv("LICENSE_PRODUCT", ""))
    args = ap.parse_args()

    if not args.signing_key or not args.api or not args.product:
        sys.exit("Set LICENSE_API, LICENSE_SIGNING_KEY and LICENSE_PRODUCT (or pass --api / --key-secret / --product).")

    client = LicenseClient(args.api, args.signing_key, args.product)
    fn = getattr(client, args.action)
    res = fn(args.key)
    if isinstance(res, bool):
        print("OK" if res else "BLOCKED")
    else:
        print(json.dumps(res, indent=2))
