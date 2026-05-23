"""License client for Python 3.9+.

Talks to the project's licensing service. The server signs every response
with HMAC-SHA256, and we verify that signature with the shared SIGNING_KEY
before trusting any answer.

Usage:

    from license_client import LicenseClient, LicenseError

    client = LicenseClient(
        api_url="https://license.example.com",
        signing_key="<same as SIGNING_KEY in the server's .env>",
        product="gpt-promo-grabber",
    )

    # First time the user enters the key:
    res = client.activate("VPXNC-YP98C-T4BH9-APW5Q")
    if not res["valid"]:
        raise LicenseError(res["status"])

    # Every app start (already includes an offline grace period):
    if not client.check(saved_key):
        sys.exit("License is not valid")

Only dependency is `requests`.
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
    """Raised when a license is invalid or the response can't be trusted."""


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
        """Convenience: validate plus an offline grace window.

        - Online and valid    -> True, last_ok timestamp refreshed.
        - Online and invalid  -> False.
        - Network error       -> True if last_ok is within the grace window,
                                 False otherwise.
        """
        try:
            res = self.validate(key)
        except requests.RequestException:
            return self._within_grace()

        if res.get("valid"):
            self._touch_last_ok()
            return True
        return False

    def machine_id(self) -> str:
        """Stable per-machine id, persisted under the user's config dir."""
        p = self.config_dir / "machine.id"
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
            raise LicenseError("signature mismatch - response cannot be trusted")
        return data

    def _verify(self, resp: dict[str, Any]) -> bool:
        sig = resp.pop("signature", "")
        raw = json.dumps(resp, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(self.signing_key, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _touch_last_ok(self) -> None:
        (self.config_dir / "last_ok").write_text(str(int(time.time())))

    def _within_grace(self) -> bool:
        p = self.config_dir / "last_ok"
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
    ap.add_argument("--product", default=os.getenv("LICENSE_PRODUCT", "gptcode"))
    args = ap.parse_args()

    if not args.signing_key or not args.api:
        sys.exit(
            "Set LICENSE_API and LICENSE_SIGNING_KEY env vars (or pass --api / "
            "--key-secret). The signing key must match SIGNING_KEY on the server."
        )

    client = LicenseClient(args.api, args.signing_key, args.product)
    fn = getattr(client, args.action)
    res = fn(args.key)
    if isinstance(res, bool):
        print("OK" if res else "BLOCKED")
    else:
        print(json.dumps(res, indent=2))
