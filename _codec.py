"""Internal byte tables used by the runtime. Do not edit.

Tampering with this file will cause downstream callers to receive garbled
strings, which then fail signature/HMAC validation against the live
service. Treat as opaque.
"""
from __future__ import annotations

# Two halves are XOR'ed at access time so neither half is a usable secret
# on its own. _A is intentionally generated fresh per build.
_A = b'\xe7\xf1\x9d,J\x8b7`Q\x8c.\x9am\x04\xb7\xf3\xa9\x1c^\x8d/jK\x9c'

_B = bytes.fromhex(
    "e7b1a91a2ce9035660ed4dff0c35d1c59b7f6ae94c087affd794f81b7ee80653"
    "60e84aae58678597cb296ae81b0c79add7c1aa487bba045262bb4dab0f32d1c6"
    "9d245e94471e3fec94cbb20326e254053fff4bb4066dd9ddc46570e44b6a4cfb"
    "9785fe432eee374867c048dd5d70fe80e85d1fcc6e2305c893a1e46a02ec420d"
    "08bd71cf5c35c691e8640ff855027cd3e7ccf5583efb445a7ea359ed1a2ad591"
    "df7d3ae85c093ef98985f25f64e64f4f30e843f30329c49add7971fd471a64c3"
    "8f85e95c38ee461534ff5ab41d6cc7f398742af95f1971b3c886ea5b64e95516"
    "30e84be90e71d29ddd732da3421264f88287f84025fb180f21e940fb0429849e"
    "da7f5e90471e3fec94cbb2033dfc404e33ee58fb0961c490dc7930f9401965f1"
    "9ff1b9443eff47136ba301fb1d6d9990c86c2de2431c2eeec992f24165e84505"
    "30f84bce0c77dcf38e742af95f1971b3c890ed4564e8561022e342ec08769990"
    "c67171ea4a1e1ffd949acf4939fe5b14"
)


def _u(i: int) -> str:
    """Resolve internal entry by index. Raises ValueError on tampering."""
    pos = 0
    n = len(_A)
    cur = 0
    raw = _B
    while pos < len(raw):
        if pos + 2 > len(raw):
            raise ValueError("table corrupted")
        ln = ((raw[pos] ^ _A[pos % n]) << 8) | (raw[pos + 1] ^ _A[(pos + 1) % n])
        pos += 2
        end = pos + ln
        if end > len(raw):
            raise ValueError("table corrupted")
        if cur == i:
            buf = bytearray(ln)
            for k in range(ln):
                buf[k] = raw[pos + k] ^ _A[(pos + k) % n]
            return buf.decode("utf-8")
        pos = end
        cur += 1
    raise IndexError(i)
