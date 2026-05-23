"""Client lisensi untuk versi rilis publik (run-public.bat).

Validasi key terhadap KISS license server (lihat https://github.com/boii/license).
Dipanggil oleh run-public.bat sebelum main.py dieksekusi.

CLI:
    python -m _license activate <KEY>   # aktivasi pertama kali, simpan key
    python -m _license check            # validasi (untuk gate runtime)
    python -m _license status           # info lokal (key, machine_id, last_ok)
    python -m _license deactivate       # lepas mesin dari lisensi
    python -m _license clear            # hapus state lokal saja

Exit code:
    0  = OK / lisensi valid
    1  = signature mismatch / state corrupt / config error
    2  = lisensi tidak valid (revoked, expired, limit_reached, dll.)
    3  = network error tanpa grace period valid
    4  = belum ada key tersimpan (hanya untuk action 'check')
    130 = dibatalkan user (Ctrl+C)

Hanya butuh `requests` (sudah ada di requirements.txt).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
import uuid

import requests

# ============================================================================
# KONFIGURASI RILIS — isi sebelum packaging untuk dijual.
# Nilai-nilai di bawah ini di-bake ke binary/source distribusi publik.
# ============================================================================

# Endpoint license server (HTTPS wajib untuk produksi).
# Override via env LICENSE_API hanya untuk testing internal.
LICENSE_API_URL = os.getenv("LICENSE_API", "https://license.kin.my.id").rstrip("/")

# HARUS sama persis dengan SIGNING_KEY di .env server.
# Jangan di-rotate setelah rilis — semua client lama akan tolak respons.
# Saat packaging untuk dijual: ganti string kosong ini dengan key dari `openssl rand -hex 32`.
LICENSE_SIGNING_KEY = os.getenv("LICENSE_SIGNING_KEY", "REPLACE_WITH_REAL_SIGNING_KEY_BEFORE_RELEASE")

# Nama produk di server (`/new <product> ...`). Cocokkan dengan yang dibuat
# admin via Telegram bot.
LICENSE_PRODUCT = os.getenv("LICENSE_PRODUCT", "bbva-gptcode")

# Grace period offline (hari). Kalau network error tapi terakhir validate
# sukses < N hari yang lalu, izinkan jalan. Setelah itu paksa online.
OFFLINE_GRACE_DAYS = 7

# Timeout HTTP ke license server.
HTTP_TIMEOUT = 10.0

# Lokasi state lokal di luar repo (tidak ter-commit, tidak terbawa kalau
# user copy folder app).
CONFIG_DIR = pathlib.Path.home() / ".bbva-gptcode"


# ============================================================================
# Implementasi
# ============================================================================


class LicenseError(RuntimeError):
    """Dilempar saat lisensi tidak valid atau respons tidak terpercaya."""


class LicenseClient:
    def __init__(self, config_dir: pathlib.Path = CONFIG_DIR):
        self.api_url = LICENSE_API_URL
        if not LICENSE_SIGNING_KEY or LICENSE_SIGNING_KEY.startswith("REPLACE_WITH_"):
            raise LicenseError(
                "SIGNING_KEY belum diisi di _license.py. "
                "Build distribusi tidak siap untuk rilis publik."
            )
        self.signing_key = LICENSE_SIGNING_KEY.encode()
        self.product = LICENSE_PRODUCT
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)

    # ----- public -----

    def activate(self, key: str, fingerprint: str | None = None) -> dict:
        return self._call("/v1/activate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
            "fingerprint": fingerprint,
        })

    def validate(self, key: str) -> dict:
        return self._call("/v1/validate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
        })

    def deactivate(self, key: str) -> dict:
        return self._call("/v1/deactivate", {
            "key": key,
            "machine_id": self.machine_id(),
            "product": self.product,
        })

    def check_with_grace(self, key: str) -> tuple[bool, str]:
        """validate + offline grace.

        Returns (ok, reason). reason kosong kalau ok=True.
        """
        try:
            res = self.validate(key)
        except requests.RequestException as exc:
            if self._within_grace():
                return True, ""
            return False, f"network error & grace habis: {exc}"

        if res.get("valid"):
            self._touch_last_ok()
            return True, ""
        return False, res.get("status") or "invalid"

    def machine_id(self) -> str:
        p = self.config_dir / "machine.id"
        if not p.exists():
            p.write_text(uuid.uuid4().hex)
            try:
                # Best-effort: sembunyikan dari listing biasa (hanya berlaku di Windows).
                if os.name == "nt":
                    os.system(f'attrib +h "{p}"')
            except OSError:
                pass
        return p.read_text().strip()

    # ----- key storage -----

    def save_key(self, key: str) -> None:
        (self.config_dir / "license.key").write_text(key.strip())

    def load_key(self) -> str | None:
        p = self.config_dir / "license.key"
        if not p.exists():
            return None
        key = p.read_text().strip()
        return key or None

    def clear_key(self) -> None:
        for name in ("license.key", "last_ok"):
            p = self.config_dir / name
            if p.exists():
                p.unlink()

    def last_ok_ts(self) -> int | None:
        p = self.config_dir / "last_ok"
        if not p.exists():
            return None
        try:
            return int(p.read_text().strip())
        except ValueError:
            return None

    # ----- internal -----

    def _call(self, path: str, body: dict) -> dict:
        body = {k: v for k, v in body.items() if v is not None}
        r = requests.post(self.api_url + path, json=body, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not self._verify(dict(data)):
            raise LicenseError("signature mismatch — koneksi tidak terpercaya")
        return data

    def _verify(self, resp: dict) -> bool:
        sig = resp.pop("signature", "")
        raw = json.dumps(resp, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(self.signing_key, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _touch_last_ok(self) -> None:
        (self.config_dir / "last_ok").write_text(str(int(time.time())))

    def _within_grace(self) -> bool:
        last = self.last_ok_ts()
        if last is None:
            return False
        return (int(time.time()) - last) < OFFLINE_GRACE_DAYS * 86400


# ============================================================================
# Pesan ramah user (Indonesia)
# ============================================================================

_STATUS_MSG = {
    "ok": "Lisensi valid.",
    "activated": "Aktivasi berhasil. Lisensi terkunci ke mesin ini.",
    "deactivated": "Mesin dilepas dari lisensi.",
    "not_found": "Key salah ketik atau tidak ada di server.",
    "revoked": "Lisensi telah di-revoke oleh penjual.",
    "expired": "Lisensi sudah lewat masa berlaku.",
    "product_mismatch": "Key ini bukan untuk produk ini.",
    "machine_limit_reached": "Slot mesin sudah penuh. Hubungi penjual untuk reset atau upgrade.",
    "machine_not_activated": "Mesin ini belum diaktivasi. Jalankan ulang dengan key untuk aktivasi.",
}


def _say(status: str) -> str:
    return _STATUS_MSG.get(status, f"Status tidak dikenali: {status}")


# ============================================================================
# CLI
# ============================================================================


def _cmd_activate(args) -> int:
    client = LicenseClient()
    key = (args.key or "").strip().upper()
    if not key:
        print("ERROR: key kosong.", file=sys.stderr)
        return 1
    try:
        res = client.activate(key)
    except LicenseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: tidak bisa hubungi server lisensi: {exc}", file=sys.stderr)
        return 3

    status = res.get("status", "")
    if res.get("valid"):
        client.save_key(key)
        client._touch_last_ok()
        print(_say(status))
        lic = res.get("license") or {}
        if lic.get("expires_at"):
            print(f"  Expires: {time.strftime('%Y-%m-%d %H:%M', time.localtime(lic['expires_at']))}")
        if lic.get("max_machines"):
            print(f"  Slots:   {lic['max_machines']}")
        return 0

    print(f"GAGAL: {_say(status)}", file=sys.stderr)
    return 2


def _cmd_check(args) -> int:
    client = LicenseClient()
    key = client.load_key()
    if not key:
        print("Belum ada key tersimpan. Jalankan 'activate <KEY>' dulu.", file=sys.stderr)
        return 4

    ok, reason = client.check_with_grace(key)
    if ok:
        if args.verbose:
            print(_say("ok"))
        return 0

    # Map reason ke exit code
    if "network" in reason.lower():
        print(f"GAGAL: {reason}", file=sys.stderr)
        return 3
    print(f"GAGAL: {_say(reason)}", file=sys.stderr)
    return 2


def _cmd_status(args) -> int:
    client = LicenseClient()
    key = client.load_key()
    last = client.last_ok_ts()
    print(f"API:        {LICENSE_API_URL}")
    print(f"Product:    {LICENSE_PRODUCT}")
    print(f"Machine ID: {client.machine_id()}")
    print(f"Key:        {key or '(belum ada)'}")
    if last:
        print(f"Last OK:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last))}")
    else:
        print("Last OK:    (never)")
    return 0


def _cmd_deactivate(args) -> int:
    client = LicenseClient()
    key = client.load_key()
    if not key:
        print("Tidak ada key tersimpan.")
        return 0
    try:
        res = client.deactivate(key)
    except LicenseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"ERROR: tidak bisa hubungi server lisensi: {exc}", file=sys.stderr)
        return 3

    client.clear_key()
    print(_say(res.get("status", "deactivated")))
    return 0


def _cmd_clear(args) -> int:
    client = LicenseClient()
    client.clear_key()
    print("State lokal dihapus (server tidak diberitahu).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="_license", description="Client lisensi BBVA GPT Code Grabber.")
    sub = parser.add_subparsers(dest="action", required=True)

    p_act = sub.add_parser("activate", help="Aktivasi key di mesin ini.")
    p_act.add_argument("key")
    p_act.set_defaults(func=_cmd_activate)

    p_chk = sub.add_parser("check", help="Validasi key tersimpan (gate runtime).")
    p_chk.add_argument("--verbose", action="store_true")
    p_chk.set_defaults(func=_cmd_check)

    p_st = sub.add_parser("status", help="Info state lokal.")
    p_st.set_defaults(func=_cmd_status)

    p_de = sub.add_parser("deactivate", help="Lepas mesin dari lisensi (server-side).")
    p_de.set_defaults(func=_cmd_deactivate)

    p_cl = sub.add_parser("clear", help="Hapus state lokal saja, tanpa kontak server.")
    p_cl.set_defaults(func=_cmd_clear)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
