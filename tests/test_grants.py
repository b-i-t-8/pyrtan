"""Signed requests, grants, and a hub that answers identities (_meta/identity.md §7)."""

import base64
import json
import secrets
import time

import pytest

pytest.importorskip("cryptography")

from pyrtan import identity  # noqa: E402
from pyrtan.cli import main  # noqa: E402
from pyrtan.federation import sign  # noqa: E402
from pyrtan.grants import Grants, ability_for, parse_may, parse_spec  # noqa: E402
from pyrtan.hub import serve_hub  # noqa: E402
from pyrtan.identity import IdentityError  # noqa: E402
from pyrtan.service import http_json  # noqa: E402

ECHO = """
name = "echo"
description = "echoes"
start = "say"

[[triggers]]
pattern = "echo {text}"

[inputs.text]
type = "str"

[nodes.say]
kind = "done"
say = "echo: {{ inputs.text }}"
"""
AUD = "https://hub.example"


@pytest.fixture
def root():
    return identity.Key.generate()


@pytest.fixture
def phone():
    return identity.Key.generate()


# ---------------------------------------------------------------- signed requests


def test_a_root_signs_for_itself(root):
    body = {"text": "echo hi"}
    h = identity.sign_request(root, AUD, "POST", "/intent", body)
    signer = identity.verify_request(AUD, "post", "/intent", body, h)
    assert signer == identity.Signer(root.id, root.id, None)
    assert identity.verify_request(AUD + "/", "POST", "/intent", body, h).root == root.id   # a trailing slash is the same hub


@pytest.mark.parametrize("change, reason", [
    (lambda a: {**a, "audience": "https://other.example"}, "for https://other.example"),
    (lambda a: {**a, "path": "/run"}, "signature is not"),
    (lambda a: {**a, "method": "GET"}, "signature is not"),
    (lambda a: {**a, "body": {"text": "echo bye"}}, "signature is not"),
    (lambda a: {**a, "now": time.time() + 1000}, "too old"),
])
def test_a_request_signature_binds_everything(root, change, reason):
    args = {"audience": AUD, "method": "POST", "path": "/intent", "body": {"text": "echo hi"}, "now": None}
    h = identity.sign_request(root, AUD, "POST", "/intent", args["body"])
    args = change(args)
    with pytest.raises(IdentityError, match=reason):
        identity.verify_request(args["audience"], args["method"], args["path"], args["body"], h, now=args["now"])


def test_a_device_signs_with_its_note(root, phone):
    note = identity.make_device(root, phone.id, ["ask"])
    h = identity.sign_request(phone, AUD, "GET", "/capabilities", None, note=note)
    signer = identity.verify_request(AUD, "GET", "/capabilities", None, h)
    assert (signer.key, signer.root, signer.note) == (phone.id, root.id, note)
    with pytest.raises(IdentityError, match="not this key"):
        identity.sign_request(root, AUD, "GET", "/", None, note=note)
    # A note for another key, an altered note, or a note that is not one.
    other = identity.Key.generate()
    forged = {**h, "X-Pyrtan-Device": base64.urlsafe_b64encode(json.dumps(identity.make_device(root, other.id, ["ask"])).encode()).decode().rstrip("=")}
    with pytest.raises(IdentityError, match="not the signing key"):
        identity.verify_request(AUD, "GET", "/capabilities", None, forged)
    widened = {**h, "X-Pyrtan-Device": base64.urlsafe_b64encode(json.dumps({**note, "may": ["operate"]}).encode()).decode().rstrip("=")}
    with pytest.raises(IdentityError, match="device note"):
        identity.verify_request(AUD, "GET", "/capabilities", None, widened)
    with pytest.raises(IdentityError, match="not a base64url note"):
        identity.verify_request(AUD, "GET", "/capabilities", None, {**h, "X-Pyrtan-Device": "!!"})


def test_no_signature():
    with pytest.raises(IdentityError, match="no key signature"):
        identity.verify_request(AUD, "GET", "/", None, {})
    assert not identity.has_key_signature({"X-Pyrtan-Signature": "x"})
    assert identity.has_key_signature({"x-pyrtan-key-signature": "x"})


# ---------------------------------------------------------------- grants


def test_abilities_and_specs():
    assert ability_for("POST", "/intent") == "ask" and ability_for("GET", "/capabilities") == "ask"
    assert ability_for("POST", "/chat/start") == "chat"
    assert ability_for("POST", "/follow") == "operate" and ability_for("GET", "/peers") == "operate"
    assert ability_for("POST", "/inbox") is None and ability_for("POST", "/identity") is None
    assert ability_for("GET", "/.well-known/pyrtan/id/x.json") is None and ability_for("GET", "/health") is None
    assert parse_may("run, ask") == ["ask", "run"] and parse_may(["chat"]) == ["chat"]
    with pytest.raises(IdentityError):
        parse_may("fly")
    with pytest.raises(IdentityError):
        parse_may("")
    assert parse_spec("alice@pyrtan.com=ask,chat") == ("alice@pyrtan.com", ["ask", "chat"])
    with pytest.raises(IdentityError):
        parse_spec("alice@pyrtan.com")


def test_grants_by_id_and_device(tmp_path, root, phone):
    g = Grants(tmp_path / "grants.json")
    g.allow(root.id, ["ask", "run"])
    assert g.may(identity.Signer(root.id, root.id)) == {"ask", "run"}
    note = identity.make_device(root, phone.id, ["run", "chat"])
    assert g.may(identity.Signer(phone.id, root.id, note)) == {"run"}             # the note caps the grant
    assert g.may(identity.Signer(phone.id, phone.id)) == set()                    # a key nobody granted
    # A newer document that revokes the phone, from anyone.
    doc = identity.make_identity(root, revoked=[phone.id])
    assert g.take_document(doc) is None and g.document(root.id) == doc
    assert g.may(identity.Signer(phone.id, root.id, note)) == set()
    assert g.take_document(identity.make_identity(root, updated=doc["updated"] - 10)) is None and g.document(root.id) == doc   # older: kept ours
    assert "signature" in g.take_document({**doc, "revoked": [], "updated": doc["updated"] + 5})
    assert "grants that identity nothing" in g.take_document(identity.make_identity(phone))
    # Kept across a restart.
    again = Grants(tmp_path / "grants.json")
    assert again.may(identity.Signer(root.id, root.id)) == {"ask", "run"} and again.document(root.id) == doc
    assert again.disallow(root.id) and not again.disallow(root.id)
    assert Grants(tmp_path / "grants.json").may(identity.Signer(root.id, root.id)) == set()


def test_a_grant_by_name_resolves_once_and_reads_documents(root):
    claim = identity.make_name(root, "alice@example.org")
    doc = identity.make_identity(root, routes=["hub.example"])
    asked = []
    served = {"https://example.org/.well-known/pyrtan/name/alice.json": claim,
              f"https://example.org/.well-known/pyrtan/id/{root.id[4:]}.json": doc}

    def fetch(url):
        asked.append(url)
        return (200, served[url]) if url in served else (404, None)

    g = Grants(fetch=fetch)
    grant = g.allow("Alice@example.org", ["chat"])
    assert (grant.id, grant.name, grant.doc) == (root.id, "alice@example.org", doc)
    # The name is the pin: the domain pointing it elsewhere moves nothing here.
    served["https://example.org/.well-known/pyrtan/name/alice.json"] = identity.make_name(identity.Key.generate(), "alice@example.org")
    asked.clear()
    assert g.allow("alice@example.org", ["chat", "ask"]).id == root.id
    assert not any("/name/" in u for u in asked)
    # The hourly refresh reads the name's domain and the document's routes.
    newer = identity.make_identity(root, routes=["hub.example"], updated=doc["updated"] + 1)
    served[f"https://hub.example/.well-known/pyrtan/id/{root.id[4:]}.json"] = newer
    g.refresh(now=time.time() + 4000)
    assert g.document(root.id) == newer
    assert g.disallow("alice@example.org")


# ---------------------------------------------------------------- a hub


class Caller:
    def __init__(self, url, key, note=None):
        self.url, self.key, self.note = url.rstrip("/"), key, note
        self.audience = http_json("GET", f"{self.url}/api")[1]["audience"]

    def __call__(self, method, path, body=None):
        if method == "POST" and body is None:
            body = {}
        return http_json(method, f"{self.url}{path}", body, headers=identity.sign_request(self.key, self.audience, method, path, body, note=self.note))


@pytest.fixture
def hub(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "echo.toml").write_text(ECHO)
    h, srv = serve_hub(port=0, quiet=True, name="solo", trees=[tmp_path / "t"], federate=True, token="tok", chat=True, poll_every=60, state=str(tmp_path / "state.json"))
    yield h, srv
    h.stop()
    srv.stop()


def test_a_hub_answers_what_it_grants(hub, root, phone):
    h, srv = hub
    as_root = Caller(srv.url, root)
    assert as_root.audience == h.federation.id == srv.url.rstrip("/")
    # Nothing granted yet: the signature holds, the grant does not.
    status, body = as_root("POST", "/intent", {"text": "echo hi"})
    assert status == 403 and body["reason"] == "not-granted" and "grants it nothing" in body["say"]
    h.grants.allow(root.id, ["ask"])
    status, body = as_root("POST", "/intent", {"text": "echo hi"})
    assert status == 200 and body["say"] == "echo: hi"
    assert as_root("GET", "/capabilities")[0] == 200
    assert as_root("GET", "/peers")[0] == 403                      # operate
    assert as_root("POST", "/chat/start", {"want": "bot"})[0] == 403
    # A device asks with what its note and the grant both allow.
    as_phone = Caller(srv.url, phone, identity.make_device(root, phone.id, ["ask", "chat"]))
    assert as_phone("POST", "/intent", {"text": "echo phone"})[1]["say"] == "echo: phone"
    h.grants.allow(root.id, ["ask", "chat"])
    assert as_phone("POST", "/chat/start", {"want": "bot"})[0] == 200
    # A bad signature is refused, whatever the grants.
    bad = identity.sign_request(root, "https://elsewhere.example", "POST", "/intent", {"text": "echo x"})
    status, body = http_json("POST", f"{srv.url}/intent", {"text": "echo x"}, headers=bad)
    assert status == 401 and "for" in body["say"]
    # The token still works as it did.
    assert http_json("GET", f"{srv.url}/peers", token="tok")[0] == 200


def test_operate_manages_grants(hub, root, phone, capsys, monkeypatch, tmp_path):
    h, srv = hub
    h.grants.allow(root.id, ["operate"])
    as_root = Caller(srv.url, root)
    status, body = as_root("POST", "/allow", {"who": phone.id, "may": ["ask"]})
    assert status == 200 and body["grant"]["may"] == ["ask"]
    assert {g["id"] for g in as_root("GET", "/allow")[1]["grants"]} == {root.id, phone.id}
    assert as_root("POST", "/allow", {"who": phone.id, "may": ["fly"]})[0] == 400
    assert as_root("POST", "/disallow", {"who": phone.id})[1]["removed"] is True
    assert as_root("GET", "/peers")[0] == 200
    # The CLI signs the same way, as the root key in $PYRTAN_IDENTITY.
    me = tmp_path / "me"
    root.save(me / "root.key")
    (me / "identity.json").write_text(json.dumps(identity.make_identity(root)))
    monkeypatch.setenv("PYRTAN_IDENTITY", str(me))
    assert main(["allow", phone.id, "--may", "ask,chat", "--at", srv.url, "--sign"]) == 0
    assert main(["allowed", "--at", srv.url, "--sign"]) == 0
    out = capsys.readouterr().out
    assert f"grants {phone.id}" in out and "may ask,chat" in out
    assert main(["id", "push", "--at", srv.url]) == 0
    assert h.grants.document(root.id) is not None
    assert main(["disallow", phone.id, "--at", srv.url, "--sign"]) == 0
    assert main(["allowed", "--at", srv.url]) == 3                 # neither token nor key


def test_documents_are_pushed_and_served(hub, root):
    h, srv = hub
    doc = identity.make_identity(root, routes=["hub.example"])
    status, body = http_json("POST", f"{srv.url}/identity", {"document": doc})
    assert status == 400 and "grants that identity nothing" in body["say"]
    h.grants.allow(root.id, ["chat"])
    assert http_json("POST", f"{srv.url}/identity", {"document": doc})[0] == 200
    assert http_json("GET", f"{srv.url}/.well-known/pyrtan/id/{root.id[4:]}.json") == (200, doc)
    assert http_json("GET", f"{srv.url}/.well-known/pyrtan/id/nope.json")[0] == 404


def follow_as(srv_url, key, note=None, *, actor=None, audience=None):
    """A member's Follow, HMAC-signed with its pair secret as always, and signed with ``key``."""
    actor = actor or key.id
    pair = secrets.token_hex(32)
    hub = srv_url.rstrip("/")
    activity = {"id": f"{actor}/activities/follow", "actor": actor, "published": "now", "type": "Follow", "object": hub, "secret": pair, "inbox": "mailbox", "name": "phone"}
    headers = sign(pair, actor, "POST", "/inbox", activity)
    headers.update(identity.sign_request(key, audience or hub, "POST", "/inbox", activity, note=note))
    return http_json("POST", f"{hub}/inbox", activity, headers=headers)


def test_members_follow_as_an_identity(hub, root, phone):
    h, srv = hub
    fed = h.federation
    note = identity.make_device(root, phone.id, ["run"])
    # Granted nothing: a keeper, but it says who it is.
    status, body = follow_as(srv.url, phone, note)
    assert status == 202 and body["trusted"] is False and body["identity"] == root.id
    # Granted run: trusted, as if it had joined with the token.
    h.grants.allow(root.id, ["run"])
    status, body = follow_as(srv.url, phone, note)
    assert status == 202 and body["trusted"] is True
    member = fed.followers[phone.id]
    assert (member.kind, member.trusted_by, member.identity, member.device) == ("member", "key", root.id, phone.id)
    # A member whose id is a key must sign with that key.
    other = identity.Key.generate()
    status, body = follow_as(srv.url, other, actor=phone.id)
    assert status == 401 and "signs its follow with that key" in body["say"]
    # Revoking the phone in a pushed document drops it.
    doc = identity.make_identity(root, revoked=[phone.id])
    assert http_json("POST", f"{srv.url}/identity", {"document": doc})[0] == 200
    assert phone.id not in fed.followers
    # Taking back run drops a member trusted by key, and leaves one trusted by token.
    laptop = identity.Key.generate()
    assert follow_as(srv.url, laptop)[1]["trusted"] is False
    h.grants.allow(laptop.id, ["run"])
    assert follow_as(srv.url, laptop)[1]["trusted"] is True
    h.grants.disallow(laptop.id)
    assert laptop.id not in fed.followers


def test_a_hub_with_no_token_only_asks_who(tmp_path, root):
    h, srv = serve_hub(port=0, quiet=True, name="open", poll_every=60)
    try:
        assert http_json("GET", f"{srv.url}/api")[1]["audience"] == srv.url.rstrip("/")
        as_root = Caller(srv.url, root)
        assert as_root("GET", "/peers")[0] == 200                  # open hub: a key is who, not a permission
        bad = identity.sign_request(root, "https://elsewhere.example", "GET", "/peers", None)
        assert http_json("GET", f"{srv.url}/peers", headers=bad)[0] == 401
    finally:
        h.stop()
        srv.stop()


def test_hub_flags(tmp_path, root, capsys):
    from pyrtan.grants import Grants as G
    h, srv = serve_hub(port=0, quiet=True, name="flags", token="tok", poll_every=60, allow=[f"{root.id}=ask,chat"], state=str(tmp_path / "state.json"))
    try:
        assert h.grants.may(identity.Signer(root.id, root.id)) == {"ask", "chat"}
        assert G(tmp_path / "grants.json").may(identity.Signer(root.id, root.id)) == {"ask", "chat"}
    finally:
        h.stop()
        srv.stop()
    with pytest.raises(IdentityError):
        serve_hub(port=0, quiet=True, allow=["nobody@127.0.0.1:9=ask"])
