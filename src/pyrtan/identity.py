"""Identity: a person, a hub or a device as an Ed25519 key.

An identity is its public key, written ``pyr:`` and 56 characters: the
lowercase base32 of the 32-byte key, a 2-byte SHA3-256 checksum and a
version byte, the layout of a Tor v3 onion address with Pyrtan's own
checksum string. ``pyr:<key>@host`` says where an identity can be
reached; ``alice@example.org`` is a name a domain vouches for. Encoding,
decoding and parsing those need only the standard library.

Three kinds of signed document stand on their own, so that anyone can
hold a copy and nobody has to trust the holder: the *identity document*
(routes, devices, revocations), the *device note* (a root delegating to
a device key, with what it may do and until when), and the *name claim*
(an identity saying it is ``name@domain``). Each is signed over
``pyrtan-doc-v1\\n`` and the document's canonical JSON without ``sig``.

Signing and verifying need ``cryptography`` (``pip install
'pyrtan[identity]'``), imported only when a signature is made or
checked. This module refuses to do Ed25519 itself: a private key is
handled only by a maintained library. It also refuses floats in a
document, so that every runtime produces the same bytes. The design is
``_meta/identity.md``.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import canonical

PREFIX = "pyr:"
VERSION_ED25519 = 1
VERSIONS = {VERSION_ED25519: 32}
CHECKSUM_TAG = b".pyrtan id"
DOC_TAG = b"pyrtan-doc-v1\n"
ID_LENGTH = 56
WINDOW = 300                      # seconds a document's time may run ahead of ours
YEAR = 365 * 24 * 3600            # the longest a device note may last
ABILITIES = ("ask", "chat", "run", "operate")
KINDS = ("person", "hub")
MAX_ROUTES = 16
MAX_DEVICES = 64

LOCAL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
INSTALL = "signatures need the identity extra: pip install 'pyrtan[identity]'"


class IdentityError(ValueError):
    pass


# ---------------------------------------------------------------- identifiers


def _checksum(key: bytes, version: int) -> bytes:
    return hashlib.sha3_256(CHECKSUM_TAG + key + bytes([version])).digest()[:2]


def encode(key: bytes, version: int = VERSION_ED25519) -> str:
    """The identifier of a public key."""
    if VERSIONS.get(version) != len(key):
        raise IdentityError(f"a version {version} key is {VERSIONS.get(version, '?')} bytes, not {len(key)}")
    raw = key + _checksum(key, version) + bytes([version])
    return PREFIX + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


def decode(text: Any) -> bytes:
    """The public key an identifier names; IdentityError says what is wrong."""
    if not isinstance(text, str) or not text.lower().startswith(PREFIX):
        raise IdentityError(f"{text!r} is not an identity: it starts {PREFIX}")
    body = text[len(PREFIX):].lower()
    if len(body) != ID_LENGTH:
        raise IdentityError(f"{text!r} is not an identity: {len(body)} characters after {PREFIX}, not {ID_LENGTH}")
    try:
        raw = base64.b32decode(body.upper() + "=" * (-len(body) % 8))
    except ValueError:
        raise IdentityError(f"{text!r} is not an identity: not base32") from None
    key, checksum, version = raw[:-3], raw[-3:-1], raw[-1]
    if version not in VERSIONS:
        raise IdentityError(f"{text!r} is version {version}, which this pyrtan does not know")
    if _checksum(key, version) != checksum:
        raise IdentityError(f"{text!r} is not an identity: the checksum does not match (a typo?)")
    return key


def normal(text: Any) -> str:
    """The identifier as it is compared: checked, lowercase."""
    decode(text)
    return text.lower()


def short(text: str) -> str:
    """For display only; never compared."""
    return f"{PREFIX}{text[len(PREFIX):len(PREFIX) + 4]}…{text[-4:]}"


def check_locator(text: Any) -> str:
    """``host``, ``host:port``, ``<56>.onion`` or ``[ip]:port``, lowercased."""
    if not isinstance(text, str) or not text:
        raise IdentityError(f"{text!r} is not a locator")
    loc = text.lower()
    host, port = loc, None
    if loc.startswith("["):
        end = loc.find("]")
        if end < 0:
            raise IdentityError(f"{text!r} is not a locator: an unclosed [")
        host, rest = loc[1:end], loc[end + 1:]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise IdentityError(f"{text!r} is not a locator: {host!r} is not an IPv6 address") from None
        if rest:
            if not rest.startswith(":"):
                raise IdentityError(f"{text!r} is not a locator")
            port = rest[1:]
    else:
        if ":" in loc:
            host, port = loc.rsplit(":", 1)
        check_host(host, text)
    if port is not None and (not port.isdigit() or not 0 < int(port) < 65536):
        raise IdentityError(f"{text!r} is not a locator: {port!r} is not a port")
    return loc


def check_host(host: str, given: Any = None) -> str:
    given = host if given is None else given
    labels = host.split(".")
    if len(host) > 253 or not all(LABEL.match(label) for label in labels):
        raise IdentityError(f"{given!r} is not a locator: {host!r} is not a host name")
    if labels[-1] == "onion" and (len(labels) < 2 or len(labels[-2]) != 56):
        raise IdentityError(f"{given!r} is not a locator: an onion address is 56 characters before .onion")
    return host


@dataclass(frozen=True)
class Parsed:
    form: str                     # "identity", "address" or "name"
    id: str | None = None
    locator: str | None = None
    name: str | None = None
    domain: str | None = None

    def __str__(self) -> str:
        if self.form == "name":
            return f"{self.name}@{self.domain}"
        return self.id + (f"@{self.locator}" if self.locator else "")


def parse(text: Any) -> Parsed:
    """Any of the three forms: ``pyr:<key>``, ``pyr:<key>@locator``, ``name@domain``."""
    if not isinstance(text, str) or not text.strip():
        raise IdentityError(f"{text!r} is not an identity, an address or a name")
    text = text.strip()
    if text.lower().startswith(PREFIX):
        ident, _, loc = text.partition("@")
        ident = normal(ident)
        if not _:
            return Parsed("identity", id=ident)
        return Parsed("address", id=ident, locator=check_locator(loc))
    local, at, domain = text.lower().rpartition("@")
    if not at:
        raise IdentityError(f"{text!r} is not an identity ({PREFIX}…) or a name (name@domain)")
    if not LOCAL.match(local):
        raise IdentityError(f"{text!r} is not a name: {local!r} is not lowercase letters, digits, dot, dash, underscore, at most 63")
    return Parsed("name", name=local, domain=check_host(domain, text))


# ---------------------------------------------------------------- signatures


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise IdentityError(INSTALL) from None
    return ed25519


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: Any) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise IdentityError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Key:
    """A private key, held in memory; ``id`` is its identity."""

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise IdentityError("an Ed25519 private key is 32 bytes")
        self._private = _ed25519().Ed25519PrivateKey.from_private_bytes(seed)
        self._seed = seed
        from cryptography.hazmat.primitives import serialization as s
        self.id = encode(self._private.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw))

    @classmethod
    def generate(cls) -> "Key":
        return cls(os.urandom(32))

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    def __repr__(self) -> str:
        return f"Key({short(self.id)})"

    # A key file is JSON, written once, readable only by its owner.

    def save(self, path: Path, *, kind: str = "root") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = json.dumps({"pyrtan-key": 1, "kind": kind, "id": self.id, "seed": _b64(self._seed)}, indent=2) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        return path

    @classmethod
    def load(cls, path: Path) -> "Key":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise IdentityError(f"{path}: {e}") from None
        if not isinstance(data, dict) or data.get("pyrtan-key") != 1:
            raise IdentityError(f"{path} is not a pyrtan key file")
        key = cls(_unb64(data.get("seed")))
        if data.get("id") != key.id:
            raise IdentityError(f"{path}: the id in the file is not the key's")
        return key


def verify(ident: str, message: bytes, signature: bytes) -> bool:
    """Whether ``signature`` is ``ident``'s over ``message``."""
    public = _ed25519().Ed25519PublicKey.from_public_bytes(decode(ident))
    try:
        public.verify(signature, message)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- documents


def _plain(value: Any, where: str = "document") -> None:
    if isinstance(value, float):
        raise IdentityError(f"{where} holds a number with a fraction; documents hold strings, integers, lists and objects")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise IdentityError(f"{where} has a key that is not a string")
            _plain(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _plain(v, f"{where}[{i}]")
    elif not (value is None or isinstance(value, (str, int, bool))):
        raise IdentityError(f"{where} holds a {type(value).__name__}")


def signed_bytes(doc: dict[str, Any]) -> bytes:
    """What a document's ``sig`` is over."""
    body = {k: v for k, v in doc.items() if k != "sig"}
    _plain(body)
    return DOC_TAG + canonical(body)


def sign_document(doc: dict[str, Any], key: Key) -> dict[str, Any]:
    body = {k: v for k, v in doc.items() if k != "sig"}
    return {**body, "sig": _b64(key.sign(signed_bytes(body)))}


def check_signature(doc: Any, by: str) -> str | None:
    """None if ``doc`` is signed by ``by``; otherwise what is wrong."""
    if not isinstance(doc, dict) or "sig" not in doc:
        return "not signed"
    try:
        signature = _unb64(doc["sig"])
        message = signed_bytes(doc)
    except IdentityError as e:
        return str(e)
    except ValueError:
        return "the signature is not base64url"
    return None if verify(by, message, signature) else f"the signature is not {short(by)}'s"


def _int(doc: dict[str, Any], field: str) -> int:
    value = doc.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IdentityError(f"{field} is not a time (whole seconds since 1970)")
    return value


def _abilities(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(a in ABILITIES for a in value) or len(set(value)) != len(value):
        raise IdentityError(f"may is a list of {', '.join(ABILITIES)}, each once")
    return value


# Device notes.


def make_device(root: Key, device: str, may: list[str], *, label: str = "", issued: int | None = None, expires: int | None = None) -> dict[str, Any]:
    issued = int(time.time()) if issued is None else issued
    note = {
        "pyrtan-device": 1, "root": root.id, "device": normal(device), "label": label,
        "may": _abilities(may), "issued": issued, "expires": issued + YEAR if expires is None else expires,
    }
    if note["device"] == root.id:
        raise IdentityError("a root does not delegate to itself; it signs for itself")
    _check_device_fields(note)
    return sign_document(note, root)


def _check_device_fields(note: dict[str, Any]) -> None:
    if note.get("pyrtan-device") != 1:
        raise IdentityError("not a device note (pyrtan-device: 1)")
    normal(note.get("root"))
    normal(note.get("device"))
    if not isinstance(note.get("label", ""), str) or len(note.get("label", "")) > 64:
        raise IdentityError("label is a string of at most 64 characters")
    _abilities(note.get("may"))
    issued, expires = _int(note, "issued"), _int(note, "expires")
    if not issued < expires <= issued + YEAR:
        raise IdentityError("a device note expires after it is issued, and within a year")


def check_device(note: Any, *, root: str | None = None, now: float | None = None) -> str | None:
    """None if the note holds (signed by its root, and by ``root`` if given, and current); otherwise why not."""
    if not isinstance(note, dict):
        return "not a device note"
    try:
        _check_device_fields(note)
    except IdentityError as e:
        return str(e)
    if root is not None and note["root"].lower() != root.lower():
        return f"the note is {short(note['root'])}'s, not {short(root)}'s"
    problem = check_signature(note, note["root"])
    if problem:
        return problem
    now = time.time() if now is None else now
    if note["issued"] > now + WINDOW:
        return "the note is issued in the future"
    if note["expires"] <= now:
        return "the note has expired"
    return None


# Identity documents.


def make_identity(root: Key, *, kind: str = "person", routes: list[str] | None = None, devices: list[dict[str, Any]] | None = None,
                  revoked: list[str] | None = None, updated: int | None = None) -> dict[str, Any]:
    doc = {
        "pyrtan-identity": 1, "id": root.id, "kind": kind,
        "routes": [check_locator(r) for r in routes or []],
        "devices": list(devices or []), "revoked": [normal(r) for r in revoked or []],
        "updated": int(time.time()) if updated is None else updated,
    }
    _check_identity_fields(doc)
    return sign_document(doc, root)


def _check_identity_fields(doc: dict[str, Any]) -> None:
    if doc.get("pyrtan-identity") != 1:
        raise IdentityError("not an identity document (pyrtan-identity: 1)")
    ident = normal(doc.get("id"))
    if doc.get("kind") not in KINDS:
        raise IdentityError(f"kind is one of {', '.join(KINDS)}")
    routes = doc.get("routes")
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        raise IdentityError(f"routes is a list of at most {MAX_ROUTES} locators")
    for r in routes:
        if check_locator(r) != r:
            raise IdentityError(f"route {r!r} is not lowercase")
    revoked = doc.get("revoked")
    if not isinstance(revoked, list) or any(normal(r) != r for r in revoked):
        raise IdentityError("revoked is a list of lowercase identities")
    devices = doc.get("devices")
    if not isinstance(devices, list) or len(devices) > MAX_DEVICES:
        raise IdentityError(f"devices is a list of at most {MAX_DEVICES} device notes")
    seen = set()
    for note in devices:
        if not isinstance(note, dict):
            raise IdentityError("devices holds something that is not a device note")
        _check_device_fields(note)
        if note["root"] != ident:
            raise IdentityError(f"device {short(note['device'])} is delegated by {short(note['root'])}, not this identity")
        if note["device"] in seen:
            raise IdentityError(f"device {short(note['device'])} is listed twice")
        seen.add(note["device"])
    _int(doc, "updated")


def check_identity(doc: Any, *, ident: str | None = None, now: float | None = None) -> str | None:
    """None if the document holds (well formed, signed by its own id, every note signed by it, not from the future)."""
    if not isinstance(doc, dict):
        return "not an identity document"
    try:
        _check_identity_fields(doc)
    except IdentityError as e:
        return str(e)
    if ident is not None and doc["id"] != ident.lower():
        return f"the document is {short(doc['id'])}'s, not {short(ident)}'s"
    problem = check_signature(doc, doc["id"])
    if problem:
        return problem
    for note in doc["devices"]:
        problem = check_signature(note, doc["id"])
        if problem:
            return f"device {short(note['device'])}: {problem}"
    if doc["updated"] > (time.time() if now is None else now) + WINDOW:
        return "the document is from the future"
    return None


def newest(docs: list[Any], *, ident: str | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Of several copies, the one with the highest ``updated`` whose checks hold."""
    good = [d for d in docs if check_identity(d, ident=ident, now=now) is None]
    return max(good, key=lambda d: d["updated"], default=None)


def device_status(doc: dict[str, Any], device: str, *, now: float | None = None) -> str:
    """``ok``, ``expired``, ``revoked`` or ``unknown``, for a device under a checked identity document."""
    device = device.lower()
    if device in doc["revoked"]:
        return "revoked"
    for note in doc["devices"]:
        if note["device"] == device:
            return "ok" if note["expires"] > (time.time() if now is None else now) else "expired"
    return "unknown"


def with_device(doc: dict[str, Any], root: Key, note: dict[str, Any]) -> dict[str, Any]:
    """A new document, signed, with ``note`` in place of any earlier note for the same device."""
    devices = [n for n in doc["devices"] if n["device"] != note["device"]] + [note]
    revoked = [r for r in doc["revoked"] if r != note["device"]]
    return _reissue(doc, root, devices=devices, revoked=revoked)


def with_revoked(doc: dict[str, Any], root: Key, device: str) -> dict[str, Any]:
    device = normal(device)
    devices = [n for n in doc["devices"] if n["device"] != device]
    revoked = doc["revoked"] + ([device] if device not in doc["revoked"] else [])
    return _reissue(doc, root, devices=devices, revoked=revoked)


def with_routes(doc: dict[str, Any], root: Key, routes: list[str]) -> dict[str, Any]:
    return _reissue(doc, root, routes=routes)


def _reissue(doc: dict[str, Any], root: Key, **changes: Any) -> dict[str, Any]:
    if doc["id"] != root.id:
        raise IdentityError(f"the document is {short(doc['id'])}'s; this key is {short(root.id)}")
    fields = {k: doc[k] for k in ("kind", "routes", "devices", "revoked")} | changes
    # Strictly newer than the copy it replaces, or a holder would keep the old one.
    return make_identity(root, updated=max(int(time.time()), doc["updated"] + 1), **fields)


# Name claims.


def make_name(key: Key, name: str, *, issued: int | None = None, **extra: Any) -> dict[str, Any]:
    """``key`` claiming ``name`` (``local@domain``). ``extra`` is a domain's own fields (pyrtan.com's ``github``)."""
    parsed = parse(name)
    if parsed.form != "name":
        raise IdentityError(f"{name!r} is not a name (name@domain)")
    claim = {"pyrtan-name": 1, "name": parsed.name, "domain": parsed.domain, "id": key.id,
             "issued": int(time.time()) if issued is None else issued, **extra}
    _plain(claim)
    return sign_document(claim, key)


def check_name(claim: Any, *, name: str | None = None) -> str | None:
    """None if the claim holds, and is for ``name`` if given; otherwise why not."""
    if not isinstance(claim, dict) or claim.get("pyrtan-name") != 1:
        return "not a name claim (pyrtan-name: 1)"
    try:
        normal(claim.get("id"))
        _int(claim, "issued")
        stated = parse(f"{claim.get('name')}@{claim.get('domain')}")
    except IdentityError as e:
        return str(e)
    if stated.form != "name" or (stated.name, stated.domain) != (claim["name"], claim["domain"]):
        return "name and domain are not a lowercase name@domain"
    if name is not None and str(parse(name)) != str(stated):
        return f"the claim is for {stated}, not {name}"
    return check_signature(claim, claim["id"])


def check_any(doc: Any, *, now: float | None = None) -> tuple[str, str | None]:
    """Which kind of document this is, and None or what is wrong with it."""
    if isinstance(doc, dict):
        if "pyrtan-identity" in doc:
            return "identity", check_identity(doc, now=now)
        if "pyrtan-device" in doc:
            return "device", check_device(doc, now=now)
        if "pyrtan-name" in doc:
            return "name", check_name(doc)
    return "unknown", "not a pyrtan identity document, device note or name claim"


# ---------------------------------------------------------------- where the CLI keeps them


def home() -> Path:
    """``$PYRTAN_IDENTITY``, else ``$XDG_CONFIG_HOME/pyrtan/identity``, else ``~/.config/pyrtan/identity``."""
    if os.environ.get("PYRTAN_IDENTITY"):
        return Path(os.environ["PYRTAN_IDENTITY"])
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pyrtan" / "identity"
