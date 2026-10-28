"""Write tests/conformance/identity.json: what both identity codes must say.

Python's src/pyrtan/identity.py says it; the console's
src/pyrtan/js/identity.js must say the same: SHA3-256 digests,
identifiers from keys, identifiers to refuse, canonical JSON, request
messages, and request headers signed with fixed keys at fixed times
(Ed25519 is deterministic, so the signatures are too).
tests/conformance/identity.cjs checks it; tests/test_conformance.py keeps
this file from drifting.

    uv run python tests/conformance/make_identity_vectors.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pyrtan import identity  # noqa: E402
from pyrtan.store import canonical  # noqa: E402

DATE = 1_793_000_000
# RFC 8032 §7.1 tests 1 and 2, and a third seed of our own.
SEEDS = [
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
    "00" * 31 + "07",
]
VALUES = [
    None, True, 0, -12, 12345678901, "", "plain", "é ü 日本 🙂", "quote \" back \\ slash\nline",
    [], {}, [1, "two", [3]], {"b": 1, "a": {"d": [], "c": None}}, {"text": "echo hi", "trace": True},
]


def build() -> dict:
    keys = [identity.Key(bytes.fromhex(s)) for s in SEEDS]
    public = [identity.decode(k.id).hex() for k in keys]
    root, phone = keys[0], keys[1]
    note = identity.make_device(root, phone.id, ["ask", "chat"], label="browser", issued=DATE - 100, expires=DATE + 86400)
    good = keys[2].id
    requests = []
    for key, note_for, method, path, body in [
        (root, None, "POST", "/intent", {"text": "echo hi"}),
        (root, None, "GET", "/capabilities", None),
        (phone, note, "POST", "/chat/start", {"want": "bot", "interests": ["disk"]}),
        (phone, note, "POST", "/inbox", {"type": "Follow", "actor": phone.id, "secret": "ab" * 32, "inbox": "mailbox", "name": "café"}),
    ]:
        audience = "https://hub.example/"
        requests.append({
            "seed": SEEDS[keys.index(key)], "public": public[keys.index(key)], "audience": audience, "method": method, "path": path, "date": DATE,
            "body": body, "note": note_for, "message": identity.request_message(audience, method, path, str(DATE), body).decode("utf-8"),
            "headers": identity.sign_request(key, audience, method, path, body, note=note_for, date=DATE),
        })
    samples = [b"", b"abc", bytes(range(256)), b"x" * 135, b"x" * 136, b"x" * 137]
    return {
        "sha3_256": [{"in": s.hex(), "out": hashlib.sha3_256(s).hexdigest()} for s in samples],
        "ids": [{"key": p, "id": k.id} for p, k in zip(public, keys)],
        "refuse": [good[:-1], good + "a", "pyx:" + good[4:], good[:10] + "1" + good[11:], good[:20] + ("b" if good[20] != "b" else "c") + good[21:], good[:-2] + "aa"],
        "accept": [good, good.upper()],
        "canonical": [{"value": v, "canonical": canonical(v).decode("utf-8")} for v in VALUES],
        "requests": requests,
        "note": note,
    }


def main() -> int:
    out = ROOT / "tests" / "conformance" / "identity.json"
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
