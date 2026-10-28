import importlib.util
import json
import os
import stat
import sys
import time

import pytest

from pyrtan import identity
from pyrtan.cli import main
from pyrtan.identity import IdentityError

needs_crypto = pytest.mark.skipif(importlib.util.find_spec("cryptography") is None, reason="needs pyrtan[identity]")

# RFC 8032 §7.1, tests 1 and 2: secret key, public key, message, signature.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]
# The vector in _meta/identity.md §2: RFC 8032 test 1's public key as an identifier.
VECTOR = "pyr:25njqamcweflpvkl73j4szahhihoc4xt3ktcgjnpaingr5yhkenaooab"
ONION = "a" * 56 + ".onion"
NOW = 1_800_000_000


# ---------------------------------------------------------------- identifiers (standard library only)


def test_the_design_notes_vector():
    key = bytes.fromhex(RFC8032[0][1])
    assert identity.encode(key) == VECTOR
    assert identity.decode(VECTOR) == key
    assert len(VECTOR) == len("pyr:") + 56


def test_decode_takes_any_case_and_compares_lowercase():
    assert identity.decode(VECTOR.upper()) == bytes.fromhex(RFC8032[0][1])
    assert identity.normal(VECTOR.upper()) == VECTOR


@pytest.mark.parametrize("text, reason", [
    (VECTOR[:20] + ("b" if VECTOR[20] != "b" else "c") + VECTOR[21:], "checksum"),
    (VECTOR[:-1], "characters"),
    (VECTOR + "a", "characters"),
    ("pyx:" + VECTOR[4:], "starts pyr:"),
    (VECTOR[:10] + "1" + VECTOR[11:], "base32"),
    (VECTOR[:-2] + "aa", "version"),
    (None, "starts pyr:"),
])
def test_decode_refuses(text, reason):
    with pytest.raises(IdentityError, match=reason):
        identity.decode(text)


def test_encode_refuses_a_key_of_the_wrong_length():
    with pytest.raises(IdentityError):
        identity.encode(b"\x00" * 31)


def test_short_is_for_display():
    assert identity.short(VECTOR) == "pyr:25nj…ooab"


def test_parse_the_three_forms():
    assert identity.parse(VECTOR) == identity.Parsed("identity", id=VECTOR)
    a = identity.parse(f"{VECTOR.upper()}@Hub.Example:8443")
    assert (a.form, a.id, a.locator) == ("address", VECTOR, "hub.example:8443")
    assert str(a) == f"{VECTOR}@hub.example:8443"
    assert identity.parse(f"{VECTOR}@{ONION}").locator == ONION
    assert identity.parse(f"{VECTOR}@[::1]:8700").locator == "[::1]:8700"
    n = identity.parse("Alice@PyrTan.com")
    assert (n.form, n.name, n.domain, str(n)) == ("name", "alice", "pyrtan.com", "alice@pyrtan.com")
    assert identity.parse(f"alice@{ONION}").domain == ONION


@pytest.mark.parametrize("text", [
    "alice", "@pyrtan.com", "-alice@pyrtan.com", "al ice@pyrtan.com", "alice@pyrtan..com", "alice@-pyrtan.com",
    "alice@xyz.onion", f"{VECTOR}@", f"{VECTOR}@host:0", f"{VECTOR}@host:70000", f"{VECTOR}@[nope]:1", f"{VECTOR}@[::1",
    "a" * 64 + "@pyrtan.com", "",
])
def test_parse_refuses(text):
    with pytest.raises(IdentityError):
        identity.parse(text)


def test_without_the_extra_signing_says_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.asymmetric", None)
    with pytest.raises(IdentityError, match=r"pyrtan\[identity\]"):
        identity.Key.generate()
    with pytest.raises(IdentityError, match=r"pyrtan\[identity\]"):
        identity.verify(VECTOR, b"", b"")
    assert identity.decode(VECTOR)          # identifiers still work


# ---------------------------------------------------------------- signatures


@needs_crypto
@pytest.mark.parametrize("secret, public, message, signature", RFC8032)
def test_rfc8032_vectors(secret, public, message, signature):
    key = identity.Key(bytes.fromhex(secret))
    assert key.id == identity.encode(bytes.fromhex(public))
    assert key.sign(bytes.fromhex(message)).hex() == signature
    assert identity.verify(key.id, bytes.fromhex(message), bytes.fromhex(signature))
    assert not identity.verify(key.id, bytes.fromhex(message) + b"x", bytes.fromhex(signature))
    assert not identity.verify(key.id, bytes.fromhex(message), b"short")


@needs_crypto
def test_key_files_are_private_and_round_trip(tmp_path):
    key = identity.Key.generate()
    path = key.save(tmp_path / "id" / "root.key")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert identity.Key.load(path).id == key.id
    with pytest.raises(FileExistsError):
        key.save(path)
    data = json.loads(path.read_text())
    data["id"] = identity.Key.generate().id
    path.write_text(json.dumps(data))
    with pytest.raises(IdentityError, match="not the key's"):
        identity.Key.load(path)


# ---------------------------------------------------------------- documents


@pytest.fixture
def root():
    pytest.importorskip("cryptography")
    return identity.Key(bytes.fromhex(RFC8032[0][0]))


@pytest.fixture
def device():
    pytest.importorskip("cryptography")
    return identity.Key(bytes.fromhex(RFC8032[1][0]))


def test_device_notes(root, device):
    note = identity.make_device(root, device.id, ["chat", "run"], label="phone", issued=NOW)
    assert note["expires"] == NOW + identity.YEAR
    assert identity.check_device(note, now=NOW + 10) is None
    assert identity.check_device(note, root=root.id, now=NOW) is None
    assert "not" in identity.check_device(note, root=device.id, now=NOW)
    assert identity.check_device(note, now=NOW + identity.YEAR) == "the note has expired"
    assert identity.check_device(note, now=NOW - 3600) == "the note is issued in the future"
    assert "signature" in identity.check_device({**note, "may": ["chat", "run", "operate"]}, now=NOW)
    assert "signature" in identity.check_device({**note, "expires": NOW + 100}, now=NOW)


def test_device_notes_refuse(root, device):
    with pytest.raises(IdentityError, match="within a year"):
        identity.make_device(root, device.id, ["chat"], issued=NOW, expires=NOW + identity.YEAR + 1)
    with pytest.raises(IdentityError, match="may"):
        identity.make_device(root, device.id, ["fly"])
    with pytest.raises(IdentityError, match="each once"):
        identity.make_device(root, device.id, ["chat", "chat"])
    with pytest.raises(IdentityError, match="itself"):
        identity.make_device(root, root.id, ["chat"])


def test_identity_documents(root, device):
    doc = identity.make_identity(root, routes=["Hub.Example", ONION], updated=NOW)
    assert doc["routes"] == ["hub.example", ONION]
    assert identity.check_identity(doc, now=NOW) is None
    assert identity.check_identity(doc, ident=root.id.upper(), now=NOW) is None
    assert "not" in identity.check_identity(doc, ident=device.id, now=NOW)
    assert "signature" in identity.check_identity({**doc, "routes": ["evil.example"]}, now=NOW)
    assert identity.check_identity(doc, now=NOW - 3600) == "the document is from the future"
    assert "kind" in identity.check_identity({**doc, "kind": "god"}, now=NOW)


def test_devices_and_revocation_reissue_newer_documents(root, device):
    doc = identity.make_identity(root, updated=int(time.time()) - 1000)
    note = identity.make_device(root, device.id, ["chat"], label="phone")
    with_it = identity.with_device(doc, root, note)
    assert with_it["updated"] > doc["updated"]
    assert identity.check_identity(with_it) is None
    assert identity.device_status(with_it, device.id.upper()) == "ok"
    renewed = identity.with_device(with_it, root, identity.make_device(root, device.id, ["chat", "run"]))
    assert len(renewed["devices"]) == 1 and renewed["devices"][0]["may"] == ["chat", "run"]
    revoked = identity.with_revoked(renewed, root, device.id)
    assert revoked["updated"] > renewed["updated"]
    assert identity.device_status(revoked, device.id) == "revoked"
    assert revoked["devices"] == [] and revoked["revoked"] == [device.id]
    assert identity.device_status(revoked, root.id) == "unknown"
    assert identity.device_status(with_it, device.id, now=note["expires"]) == "expired"
    assert identity.newest([doc, revoked, with_it]) == revoked
    with pytest.raises(IdentityError, match="this key"):
        identity.with_revoked(revoked, device, device.id)


def test_newest_ignores_a_forged_or_foreign_copy(root, device):
    doc = identity.make_identity(root, updated=NOW)
    forged = {**doc, "routes": ["evil.example"], "updated": NOW + 10}
    foreign = identity.make_identity(device, updated=NOW + 20)
    assert identity.newest([doc, forged, foreign], ident=root.id, now=NOW) == doc
    assert identity.newest([forged], now=NOW) is None


def test_a_document_cannot_carry_another_roots_device(root, device):
    other = identity.Key.generate()
    note = identity.make_device(other, device.id, ["chat"])
    with pytest.raises(IdentityError, match="delegated by"):
        identity.make_identity(root, devices=[note])


def test_documents_hold_no_floats(root):
    with pytest.raises(IdentityError, match="fraction"):
        identity.sign_document({"pyrtan-name": 1, "x": 1.5}, root)
    assert "fraction" in identity.check_signature({"x": 1.5, "sig": "AA"}, root.id)


def test_name_claims(root, device):
    claim = identity.make_name(root, "Alice@PyrTan.com", issued=NOW, github=123)
    assert (claim["name"], claim["domain"], claim["github"]) == ("alice", "pyrtan.com", 123)
    assert identity.check_name(claim) is None
    assert identity.check_name(claim, name="ALICE@pyrtan.com") is None
    assert identity.check_name(claim, name="bob@pyrtan.com") == "the claim is for alice@pyrtan.com, not bob@pyrtan.com"
    assert "signature" in identity.check_name({**claim, "github": 456})
    assert "signature" in identity.check_name({**claim, "id": device.id})
    assert "lowercase" in identity.check_name({**claim, "name": "Alice"})
    with pytest.raises(IdentityError):
        identity.make_name(root, VECTOR)


def test_check_any(root, device):
    assert identity.check_any(identity.make_identity(root)) == ("identity", None)
    assert identity.check_any(identity.make_device(root, device.id, ["ask"])) == ("device", None)
    assert identity.check_any(identity.make_name(root, "a@b.example")) == ("name", None)
    assert identity.check_any([])[0] == "unknown"


def test_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PYRTAN_IDENTITY", str(tmp_path / "x"))
    assert identity.home() == tmp_path / "x"
    monkeypatch.delenv("PYRTAN_IDENTITY")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert identity.home() == tmp_path / "pyrtan" / "identity"


# ---------------------------------------------------------------- pyrtan id


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@needs_crypto
def test_cli_new_device_revoke_route(tmp_path, capsys):
    d = str(tmp_path / "me")
    code, out, _ = run(capsys, "id", "new", "--route", "hub.example", "--dir", d)
    assert code == 0 and "you are pyr:" in out and "Back it up" in out
    me = out.split()[2]
    assert stat.S_IMODE(os.stat(tmp_path / "me" / "root.key").st_mode) == 0o600
    code, _, err = run(capsys, "id", "new", "--dir", d)
    assert code == 1 and "exists" in err

    phone = identity.Key.generate().id
    code, out, err = run(capsys, "id", "device", "add", phone, "--may", "chat,run", "--label", "phone", "--days", "30", "--dir", d)
    note = json.loads(out)
    assert code == 0 and note["root"] == me and note["may"] == ["chat", "run"] and identity.check_device(note) is None
    assert note["expires"] - note["issued"] == 30 * 86400
    code, out, _ = run(capsys, "id", "show", "--dir", d)
    assert me in out and "hub.example" in out and phone in out and "ok until" in out

    code, _, _ = run(capsys, "id", "route", "add", ONION, "--dir", d)
    code, _, _ = run(capsys, "id", "route", "remove", "hub.example", "--dir", d)
    code, out, _ = run(capsys, "id", "revoke", phone, "--dir", d)
    assert code == 0 and "revoked" in out
    code, out, _ = run(capsys, "id", "show", "--json", "--dir", d)
    doc = json.loads(out)
    assert identity.check_identity(doc, ident=me) is None
    assert doc["routes"] == [ONION] and doc["devices"] == [] and doc["revoked"] == [phone]

    code, _, err = run(capsys, "id", "device", "add", phone, "--may", "fly", "--dir", d)
    assert code == 1 and "may" in err
    code, _, err = run(capsys, "id", "device", "add", phone, "--days", "400", "--dir", d)
    assert code == 1 and "365" in err


@needs_crypto
def test_cli_name_and_check(tmp_path, capsys):
    d = str(tmp_path / "me")
    run(capsys, "id", "new", "--dir", d)
    out_file = tmp_path / "alice.json"
    code, out, _ = run(capsys, "id", "name", "alice@pyrtan.com", "--github", "123", "--out", str(out_file), "--dir", d)
    assert code == 0 and "/.well-known/pyrtan/name/alice.json" in out
    code, out, _ = run(capsys, "id", "check", str(out_file))
    assert code == 0 and "claims alice@pyrtan.com" in out
    claim = json.loads(out_file.read_text())
    out_file.write_text(json.dumps({**claim, "name": "bob"}))
    code, out, _ = run(capsys, "id", "check", str(out_file))
    assert code == 1 and "refused" in out
    code, out, _ = run(capsys, "id", "check", str(tmp_path / "me" / "identity.json"))
    assert code == 0 and "identity document" in out


def test_cli_check_identifiers(tmp_path, capsys):
    code, out, _ = run(capsys, "id", "check", f"{VECTOR}@hub.example")
    assert code == 0 and "well-formed address" in out and RFC8032[0][1] in out
    code, out, _ = run(capsys, "id", "check", "alice@pyrtan.com")
    assert code == 0 and "well-formed name" in out and "pyrtan id resolve alice@pyrtan.com" in out
    code, _, err = run(capsys, "id", "check", VECTOR[:-3] + "zab")
    assert code == 1 and "pyrtan id:" in err


def test_cli_without_an_identity(tmp_path, capsys):
    code, _, err = run(capsys, "id", "show", "--dir", str(tmp_path / "nobody"))
    assert code == 1 and "pyrtan id new" in err


# ---------------------------------------------------------------- backups, releases, handovers


@needs_crypto
def test_backups(root, device):
    doc = identity.make_identity(root, updated=int(time.time()) - 10)
    b = identity.make_backup(root, "correct horse battery", document=doc, iterations=identity.MIN_ITERATIONS)
    assert identity.backup_problem(b) is None and identity.check_any(b) == ("backup", None)
    assert identity.open_backup(b, "correct horse battery").id == root.id
    with pytest.raises(IdentityError, match="passphrase does not open"):
        identity.open_backup(b, "wrong horse battery")
    # The ciphertext is bound to the identity it names, and a weakened kdf is refused.
    with pytest.raises(IdentityError, match="passphrase does not open"):
        identity.open_backup({**b, "id": device.id, "document": None}, "correct horse battery")
    assert "iterations" in identity.backup_problem({**b, "kdf": {**b["kdf"], "iterations": 1000}})
    assert "document" in identity.backup_problem({**b, "document": identity.make_identity(device)})
    with pytest.raises(IdentityError, match="at least 8"):
        identity.make_backup(root, "short")
    assert identity.make_backup(root, "correct horse battery", iterations=identity.MIN_ITERATIONS)["kdf"]["salt"] != b["kdf"]["salt"]


@needs_crypto
def test_releases_and_handovers(root, device):
    release = identity.make_release(root, "alice@example.org", issued=NOW)
    assert identity.check_release(release, name="alice@example.org") is None
    assert "bob" in identity.check_release(release, name="bob@example.org")
    assert "signature" in identity.check_release({**release, "id": device.id})
    claim = identity.make_name(device, "alice@example.org", issued=NOW)
    assert "without a handover" in identity.check_handover(claim, root.id)
    moved = identity.make_name(device, "alice@example.org", issued=NOW, handover_from=root)
    assert identity.check_name(moved) is None                        # still the new key's own claim
    assert identity.check_handover(moved, root.id) is None
    assert "not" in identity.check_handover(moved, device.id)
    assert "not signed" in identity.check_handover({**moved, "issued": NOW + 1}, root.id)


@needs_crypto
def test_cli_backup_and_restore(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PP", "correct horse battery staple")
    a, c = str(tmp_path / "a"), str(tmp_path / "c")
    run(capsys, "id", "new", "--dir", a)
    phone = identity.Key.generate().id
    run(capsys, "id", "device", "add", phone, "--dir", a)
    code, out, _ = run(capsys, "id", "backup", "--out", str(tmp_path / "b.json"), "--passphrase-env", "PP", "--dir", a)
    assert code == 0 and "never in a public one" in out
    assert stat.S_IMODE(os.stat(tmp_path / "b.json").st_mode) == 0o600
    assert run(capsys, "id", "backup", "--out", str(tmp_path / "b.json"), "--passphrase-env", "PP", "--dir", a)[0] == 1   # not overwritten
    code, out, _ = run(capsys, "id", "check", str(tmp_path / "b.json"))
    assert code == 0 and "a key backup of" in out
    code, out, _ = run(capsys, "id", "restore", str(tmp_path / "b.json"), "--passphrase-env", "PP", "--dir", c)
    assert code == 0 and "again" in out
    code, out, _ = run(capsys, "id", "show", "--dir", c)
    assert phone in out                                              # the document came back with the key
    monkeypatch.setenv("PP", "not the passphrase")
    code, _, err = run(capsys, "id", "restore", str(tmp_path / "b.json"), "--passphrase-env", "PP", "--dir", str(tmp_path / "d"))
    assert code == 1 and "does not open" in err
    other = str(tmp_path / "other")
    run(capsys, "id", "new", "--dir", other)
    monkeypatch.setenv("PP", "correct horse battery staple")
    code, _, err = run(capsys, "id", "restore", str(tmp_path / "b.json"), "--passphrase-env", "PP", "--dir", other)
    assert code == 1 and "already holds" in err


def test_replays_are_refused_while_a_signature_holds():
    from pyrtan.identity import Replays

    r = Replays(window=300, limit=3)
    now = 1_000_000
    assert r.fresh("sig-a", now, now=now) and not r.fresh("sig-a", now, now=now + 299)
    assert r.fresh("sig-a", now, now=now + 302)   # its window is over: the date check refuses it now, not this
    assert not r.fresh("sig-b", "soon", now=now)  # no date, no signature
    for s in ("x", "y", "z", "w"):
        assert r.fresh(s, now, now=now)
    assert len(r._seen) == 3                       # remembering is bounded
