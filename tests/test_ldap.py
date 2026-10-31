"""LDAP users on top of the token: the wire, the escaping, the groups, and the
door in front of a service and a hub. The directory is a small one in this
file, speaking the same BER over a real socket."""

import base64
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from pyrtan import Registry, RecordedTransport
from pyrtan.hub import Hub
from pyrtan.ldap import (
    IN_CHAIN, Connection, LdapError, LdapUsers, _int, _items, _number, _octets, _read, _seq, _tlv,
    encode_filter, escape_dn, escape_filter, parse_abilities, parse_group,
)
from pyrtan.service import PEER_HEADER, Server, Service, _with_peer, http_json
from conftest import EXAMPLES, FIXTURES

BASE = "dc=example,dc=org"
PEOPLE = f"ou=people,{BASE}"
GROUPS = f"ou=groups,{BASE}"
SERVICE_DN = f"cn=pyrtan,{BASE}"
OPS = f"cn=ops,{GROUPS}"
CHATTERS = f"cn=chatters,{GROUPS}"
STAFF = f"cn=staff,{GROUPS}"        # holds the ops group, which holds alice
LOOP_A = f"cn=loop-a,{GROUPS}"      # loop-a holds loop-b, which holds loop-a and ops: a cycle
LOOP_B = f"cn=loop-b,{GROUPS}"


def _entries() -> dict[str, dict[str, list[str]]]:
    def person(uid: str, pw: str) -> tuple[str, dict[str, list[str]]]:
        return f"uid={uid},{PEOPLE}", {"objectclass": ["person"], "uid": [uid], "cn": [uid.title()], "userpassword": [pw]}
    out = dict([person("alice", "alice-pw"), person("bob", "bob-pw"), person("carol", "carol-pw"), person("a*b", "star-pw")])
    out[SERVICE_DN] = {"objectclass": ["account"], "cn": ["pyrtan"], "userpassword": ["svc-pw"]}
    out[OPS] = {"objectclass": ["groupOfNames"], "cn": ["ops"], "member": [f"uid=alice,{PEOPLE}"]}
    out[CHATTERS] = {"objectclass": ["posixGroup"], "cn": ["chatters"], "memberuid": ["bob"]}
    out[STAFF] = {"objectclass": ["group"], "cn": ["staff"], "member": [OPS]}
    out[LOOP_A] = {"objectclass": ["group"], "cn": ["loop-a"], "member": [LOOP_B]}
    out[LOOP_B] = {"objectclass": ["group"], "cn": ["loop-b"], "member": [LOOP_A, OPS]}
    return out


def _in_chain(entries: dict[str, dict[str, list[str]]], group: dict[str, list[str]], dn: str) -> bool:
    """What Active Directory's LDAP_MATCHING_RULE_IN_CHAIN says: dn is a member of the
    group, or of a group that is, to any depth; a cycle ends."""
    by_dn = {d.lower(): e for d, e in entries.items()}
    seen, todo = set(), [m.lower() for m in group.get("member", [])]
    while todo:
        m = todo.pop()
        if m == dn.lower():
            return True
        if m not in seen:
            seen.add(m)
            todo.extend(x.lower() for x in by_dn.get(m, {}).get("member", []))
    return False


def _match(f: tuple[int, bytes], attrs: dict[str, list[str]], d: "FakeDirectory | None" = None) -> bool:
    tag, content = f
    if tag == 0xA0:
        return all(_match(x, attrs, d) for x in _items(content))
    if tag == 0xA1:
        return any(_match(x, attrs, d) for x in _items(content))
    if tag == 0xA2:
        return not _match(_items(content)[0], attrs, d)
    if tag == 0xA9:
        parts = {t: v.decode() for t, v in _items(content)}
        if d is not None and d.in_chain and parts.get(0x81) == IN_CHAIN and parts.get(0x82, "").lower() == "member":
            return _in_chain(d.entries, attrs, parts[0x83])
        return False  # a rule this directory does not know: Undefined, which matches nothing
    if tag == 0x87:
        return content.decode().lower() in attrs
    if tag == 0xA3:
        (_, a), (_, v) = _items(content)
        return v.decode().lower() in [x.lower() for x in attrs.get(a.decode().lower(), [])]
    if tag == 0xA4:
        (_, a), (_, subs) = _items(content)
        import re
        pattern, parts = "", _items(subs)
        for kind, piece in parts:  # initial, then any..., then final
            pattern += ("" if kind == 0x80 else ".*") + re.escape(piece.decode().lower())
        pattern += "" if parts and parts[-1][0] == 0x82 else ".*"
        return any(re.fullmatch(pattern, x.lower()) for x in attrs.get(a.decode().lower(), []))
    raise AssertionError(f"filter tag {tag:#x}")


class FakeDirectory:
    """Enough of an LDAP server for these tests: simple bind (and the unauthenticated
    bind a DN with no password is), base and subtree search, StartTLS refused."""

    def __init__(self, anonymous_search: bool = True, in_chain: bool = True) -> None:
        self.entries = _entries()
        self.anonymous_search = anonymous_search
        self.in_chain = in_chain  # knows Active Directory's transitive rule (OpenLDAP does not)
        self.binds: list[str] = []
        self.filters: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"ldap://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buf, bound = b"", None
        with conn:
            while True:
                try:
                    tag, body, end = _read(buf)
                except LdapError:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    continue
                buf = buf[end:]
                items = _items(body)
                msgid, (op, content) = _number(items[0][1]), items[1]
                reply = lambda *ops: conn.sendall(b"".join(_seq(_int(msgid), o) for o in ops))  # noqa: E731
                result = lambda code, tag: _tlv(tag, _int(code, 0x0A) + _octets("") + _octets(""))  # noqa: E731
                if op == 0x42:
                    return
                if op == 0x60:
                    (_, _v), (_, dn), (_, pw) = _items(content)
                    dn, pw = dn.decode(), pw.decode()
                    self.binds.append(dn)
                    entry = next((e for d, e in self.entries.items() if d.lower() == dn.lower()), None)
                    if not pw:
                        bound = None
                        reply(result(0, 0x61))  # an unauthenticated bind: success, as many directories say
                    elif entry and pw in entry.get("userpassword", []):
                        bound = dn
                        reply(result(0, 0x61))
                    else:
                        reply(result(49, 0x61))
                elif op == 0x63:
                    parts = _items(content)
                    base, scope, filt = parts[0][1].decode().lower(), _number(parts[1][1]), parts[6]
                    self.filters.append(_tlv(*filt))
                    if bound is None and not self.anonymous_search:
                        reply(result(50, 0x65))
                        continue
                    if scope == 0 and base not in {d.lower() for d in self.entries}:
                        reply(result(32, 0x65))
                        continue
                    found = [d for d, e in self.entries.items() if (d.lower() == base if scope == 0 else d.lower().endswith(base)) and _match(filt, e, self)]
                    reply(*[_seq(_octets(d), _seq(), tag=0x64) for d in found], result(0, 0x65))
                elif op == 0x77:
                    reply(_tlv(0x78, _int(2, 0x0A) + _octets("") + _octets("no TLS here")))
                else:
                    raise AssertionError(f"op {op:#x}")


@pytest.fixture
def directory():
    d = FakeDirectory()
    yield d
    d.close()


def _users(d: FakeDirectory, **kw) -> LdapUsers:
    kw.setdefault("groups", {OPS: ["operate"], CHATTERS: ["chat"]})
    kw.setdefault("may", ["ask"])
    if "base" not in kw:
        kw.setdefault("user_dn", f"uid={{user}},{PEOPLE}")
    return LdapUsers(d.url, **kw)


def _basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def _raw(url: str, headers: dict | None = None, method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


# ---------------------------------------------------------------- escaping and filters


def test_escaping_keeps_a_name_from_widening_anything():
    assert escape_filter("a*b)(uid=*") == "a\\2ab\\29\\28uid=\\2a"
    assert escape_filter("back\\slash\0") == "back\\5cslash\\00"
    assert escape_dn("smith, john+admin") == "smith\\, john\\+admin"
    assert escape_dn("#lead ") == "\\#lead\\ "
    assert escape_dn("x=y;z") == "x\\=y\\;z"


def test_filters_encode_to_ber():
    attrs = {"objectclass": ["person"], "uid": ["alice"], "cn": ["Alice Liddell"]}
    for text, want in [
        ("(uid=alice)", True), ("uid=alice", True), ("(uid=bob)", False), ("(uid=*)", True), ("(mail=*)", False),
        ("(&(objectClass=person)(uid=alice))", True), ("(|(uid=bob)(cn=alice liddell))", True), ("(!(uid=alice))", False),
        ("(cn=Al*)", True), ("(cn=*Lid*)", True), ("(cn=*dell)", True), ("(cn=A*e*l)", True), ("(cn=*bob*)", False),
        ("(uid=\\61lice)", True),
    ]:
        assert _match(_read(encode_filter(text))[:2], attrs) is want, text
    assert encode_filter("(uid>=5)")[0] == 0xA5 and encode_filter("(uid<=5)")[0] == 0xA6 and encode_filter("(cn~=x)")[0] == 0xA8
    for bad in ["(uid=alice", "(&)", "(=x)", "(uid=a\\zz)", "(uid=a))", "(bad attr=x)", "(:=x)", "(a:b:c:=x)", "(bad attr:=x)", "(:dn:=x)"]:
        with pytest.raises(LdapError):
            encode_filter(bad)


def test_extensible_matches_encode_to_ber():
    tag, content, _ = _read(encode_filter(f"(member:{IN_CHAIN}:=uid=alice\\2cou=people)"))
    assert tag == 0xA9 and _items(content) == [(0x81, IN_CHAIN.encode()), (0x82, b"member"), (0x83, b"uid=alice,ou=people")]
    _, content, _ = _read(encode_filter("(cn:dn:2.5.13.5:=John)"))
    assert _items(content) == [(0x81, b"2.5.13.5"), (0x82, b"cn"), (0x83, b"John"), (0x84, b"\xff")]
    _, content, _ = _read(encode_filter("(:caseExactMatch:=x)"))
    assert _items(content) == [(0x81, b"caseExactMatch"), (0x83, b"x")]
    _, content, _ = _read(encode_filter("(o:dn:=Ace)"))
    assert _items(content) == [(0x82, b"o"), (0x83, b"Ace"), (0x84, b"\xff")]


def test_groups_and_abilities_parse():
    assert parse_group(f"{OPS}=ask,operate") == (OPS, ["ask", "operate"])
    with pytest.raises(LdapError):
        parse_group(f"{OPS}=fly")
    with pytest.raises(LdapError):
        parse_group("ops=ask")
    assert parse_abilities(None) is None and parse_abilities("none") == [] and parse_abilities("chat, ask") == ["ask", "chat"]


def test_configuration_is_checked_at_start(directory):
    with pytest.raises(LdapError, match="find a person"):
        LdapUsers(directory.url)
    with pytest.raises(LdapError, match="{user}"):
        LdapUsers(directory.url, user_dn=f"uid=alice,{PEOPLE}")
    with pytest.raises(LdapError, match="PASSWORD"):
        LdapUsers(directory.url, base=BASE, bind_dn=SERVICE_DN)
    with pytest.raises(LdapError, match="not an ldap"):
        LdapUsers("http://x", user_dn="uid={user}")
    with pytest.raises(LdapError):
        LdapUsers(directory.url, base=BASE, filter="(uid={user}")
    assert "svc-pw" not in repr(LdapUsers(directory.url, base=BASE, bind_dn=SERVICE_DN, bind_password="svc-pw"))
    # No group listed: anyone who binds may ask. A group listed: only what the groups give.
    assert LdapUsers(directory.url, user_dn="uid={user}").may == {"ask"}
    assert LdapUsers(directory.url, user_dn="uid={user}", groups={OPS: ["operate"]}).may == set()


# ---------------------------------------------------------------- signing in


def test_a_bind_by_template_and_the_groups(directory):
    users = _users(directory)
    alice = users.authenticate("alice", "alice-pw")
    assert alice is not None and alice.dn == f"uid=alice,{PEOPLE}" and alice.may == {"ask", "operate"}
    assert users.authenticate("bob", "bob-pw").may == {"ask", "chat"}       # a posixGroup, by memberUid
    assert users.authenticate("carol", "carol-pw").may == {"ask"}           # in no group
    assert users.authenticate("alice", "wrong") is None
    assert users.authenticate("nobody", "x") is None


def test_nested_groups_as_active_directory_follows_them(directory):
    groups = {STAFF: ["chat"], OPS: ["operate"]}
    assert _users(directory, groups=groups).authenticate("alice", "alice-pw").may == {"ask", "operate"}  # staff holds ops, not alice
    directory.filters.clear()
    nested = _users(directory, groups=groups, nested=True)
    assert nested.authenticate("alice", "alice-pw").may == {"ask", "chat", "operate"}
    in_chain = [f for f in directory.filters if f[0] == 0xA9]
    assert len(in_chain) == 1  # only for staff: ops named alice directly, so the chain was never asked
    assert nested.authenticate("bob", "bob-pw").may == {"ask"}  # in neither, however deep
    # A cycle (loop-a holds loop-b, which holds loop-a and ops) is the directory's to end.
    assert _users(directory, groups={LOOP_A: ["run"]}, nested=True).authenticate("alice", "alice-pw").may == {"ask", "run"}
    assert _users(directory, groups={LOOP_A: ["run"]}).authenticate("alice", "alice-pw").may == {"ask"}


def test_nested_on_a_directory_without_the_rule_keeps_direct_members():
    d = FakeDirectory(in_chain=False)  # OpenLDAP, say: the rule matches nothing
    try:
        users = _users(d, groups={STAFF: ["chat"], OPS: ["operate"]}, nested=True)
        assert users.authenticate("alice", "alice-pw").may == {"ask", "operate"}
    finally:
        d.close()


def test_an_empty_password_is_nobody(directory):
    users = _users(directory)
    assert users.authenticate("alice", "") is None
    assert directory.binds == []  # never sent: the directory would have said yes to it
    assert users.check(_basic("alice", "")["Authorization"]) is None


def test_search_then_bind_as_the_service_account():
    d = FakeDirectory(anonymous_search=False)
    try:
        users = _users(d, base=BASE, filter="(&(objectClass=person)(uid={user}))", bind_dn=SERVICE_DN, bind_password="svc-pw")
        assert users.authenticate("alice", "alice-pw").may == {"ask", "operate"}
        assert d.binds == [SERVICE_DN, f"uid=alice,{PEOPLE}", SERVICE_DN]  # find, prove, then read the groups as the service
        # A name with filter characters in it finds itself and nothing wider.
        assert users.authenticate("*", "alice-pw") is None
        assert users.authenticate("a*b", "star-pw").dn == f"uid=a*b,{PEOPLE}"
        assert users.authenticate("alice)(uid=*", "alice-pw") is None
        # A service account the directory refuses is the directory's fault, not a wrong password.
        broken = _users(d, base=BASE, bind_dn=SERVICE_DN, bind_password="nope")
        with pytest.raises(LdapError, match="bind-dn"):
            broken.authenticate("alice", "alice-pw")
    finally:
        d.close()


def test_search_anonymously(directory):
    users = _users(directory, base=PEOPLE)
    assert users.authenticate("bob", "bob-pw").dn == f"uid=bob,{PEOPLE}"
    assert users.authenticate("zed", "x") is None


def test_a_bind_is_remembered_for_a_moment(directory):
    users = _users(directory)
    header = _basic("alice", "alice-pw")["Authorization"]
    assert users.check(header) is not None and users.check(header) is not None
    assert directory.binds.count(f"uid=alice,{PEOPLE}") == 1
    assert users.check(_basic("alice", "other")["Authorization"]) is None
    assert directory.binds.count(f"uid=alice,{PEOPLE}") == 2  # a different password is asked again


def test_guessing_rests_the_name(directory):
    users = _users(directory)
    for _ in range(5):
        assert users.authenticate("alice", "guess") is None
    asked = len(directory.binds)
    assert users.authenticate("alice", "alice-pw") is None  # resting: not even the right password is tried
    assert len(directory.binds) == asked
    assert users.authenticate("bob", "bob-pw") is not None  # only that name rests


def test_a_directory_that_cannot_be_reached():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    users = LdapUsers(f"ldap://127.0.0.1:{port}", user_dn="uid={user}", timeout=2)
    with pytest.raises(LdapError, match="cannot reach"):
        users.authenticate("alice", "pw")
    assert users.login({"user": "alice", "password": "pw"})[0] == 503


def test_starttls_refused_is_an_error(directory):
    with pytest.raises(LdapError, match="StartTLS"):
        Connection(directory.url, starttls=True)
    with pytest.raises(LdapError):
        LdapUsers(directory.url.replace("ldap:", "ldaps:"), user_dn="uid={user}", starttls=True)


def test_sessions(directory):
    users = _users(directory)
    assert users.login({"user": "alice", "password": "nope"})[0] == 401
    assert users.login({"user": "alice"})[0] == 400
    status, out = users.login({"user": "alice", "password": "alice-pw"})
    assert status == 200 and out["token"].startswith("pyu_") and out["may"] == ["ask", "operate"] and out["user"] == "alice"
    assert users.check(f"Bearer {out['token']}").name == "alice" and len(users) == 1
    assert users.check("Bearer pyu_madeup") is None
    assert users.logout(f"Bearer {out['token']}") == (200, {"status": "ok", "ended": True})
    assert users.check(f"Bearer {out['token']}") is None
    short = _users(directory, session_ttl=-1)
    _, out = short.login({"user": "bob", "password": "bob-pw"})
    assert short.check(f"Bearer {out['token']}") is None  # expired
    nobody = _users(directory, may=[])
    assert nobody.login({"user": "carol", "password": "carol-pw"})[0] == 403  # binds, in no group: may nothing


# ---------------------------------------------------------------- the door


@pytest.fixture
def door(directory):
    reg = Registry.from_paths([EXAMPLES])
    svc = Service(reg, name="door", transport=RecordedTransport.from_file(FIXTURES / "examples.json"), token="s3cret")
    server = Server(svc.route, quiet=True, token="s3cret", users=_users(directory)).start()
    yield server
    server.stop()


def test_a_service_takes_directory_accounts_on_top_of_its_token(door):
    url = door.url
    status, headers, body = _raw(f"{url}/capabilities")
    assert status == 401 and "/login" in body["say"]
    assert headers["WWW-Authenticate"] == 'Bearer realm="pyrtan"'  # no Basic challenge: a browser would prompt over the console
    assert _raw(f"{url}/capabilities", {"Authorization": "Bearer s3cret"})[0] == 200  # the token is what it was
    assert _raw(f"{url}/capabilities", _basic("carol", "carol-pw"))[0] == 200
    assert _raw(f"{url}/capabilities", _basic("carol", "wrong"))[0] == 401
    # What a person may do is checked here: /gaps is operate's.
    status, _, body = _raw(f"{url}/gaps", _basic("carol", "carol-pw"))
    assert status == 403 and body["reason"] == "not-granted" and "carol may not operate" in body["say"]
    assert _raw(f"{url}/gaps", _basic("alice", "alice-pw"))[0] == 200
    # /health shows all of itself to an operator only.
    assert "trees" not in _raw(f"{url}/health", _basic("carol", "carol-pw"))[2]
    assert "trees" in _raw(f"{url}/health", _basic("alice", "alice-pw"))[2]
    # /api says people may sign in, and who this is.
    _, _, api = _raw(f"{url}/api", _basic("bob", "bob-pw"))
    assert api["login"] == "ldap" and "/login" in api["endpoints"] and api["user"]["may"] == ["ask", "chat"]
    # A sentence, asked as a person.
    status, out = http_json("POST", f"{url}/intent", {"text": "weather in Oslo"}, headers=_basic("carol", "carol-pw"))
    assert status == 200 and out["status"] == "done"


def test_login_gives_a_session_token_for_the_bearer(door):
    url = door.url
    status, out = http_json("POST", f"{url}/login", {"user": "alice", "password": "alice-pw"})
    assert status == 200 and out["token"].startswith("pyu_")
    assert http_json("GET", f"{url}/gaps", token=out["token"])[0] == 200
    assert http_json("POST", f"{url}/login", {"user": "alice", "password": "nope"})[0] == 401
    assert http_json("POST", f"{url}/logout", {}, token=out["token"])[1]["ended"] is True
    assert http_json("GET", f"{url}/gaps", token=out["token"])[0] == 401


def test_the_server_headers_are_never_the_callers():
    spoofed = {"X-Pyrtan-User": "root", "x-pyrtan-user-may": "operate", PEER_HEADER: "10.0.0.1"}
    out = _with_peer(spoofed, "192.0.2.7")
    assert "X-Pyrtan-User" not in out and "x-pyrtan-user-may" not in out and out[PEER_HEADER] == "192.0.2.7"


def test_ldap_needs_a_token(directory):
    with pytest.raises(ValueError, match="token"):
        Server(lambda *a: (200, {}), quiet=True, users=_users(directory))


def test_a_hub_gives_people_what_their_groups_say(directory):
    hub = Hub(poll_every=60, token="s3cret", chat=True)
    server = Server(hub.route, quiet=True, token="s3cret", users=_users(directory), keyed=True).start()
    try:
        url = server.url
        assert _raw(f"{url}/peers", _basic("alice", "alice-pw"))[0] == 200     # operate
        assert _raw(f"{url}/peers", _basic("bob", "bob-pw"))[0] == 403         # chat, not operate
        assert _raw(f"{url}/api", _basic("bob", "bob-pw"))[2]["chat_bots"] is True   # chat: meets bots
        assert _raw(f"{url}/api", _basic("carol", "carol-pw"))[2]["chat_bots"] is False
        # Saying you may is not may.
        forged = {**_basic("carol", "carol-pw"), "X-Pyrtan-User-May": "chat,operate"}
        assert _raw(f"{url}/api", forged)[2]["chat_bots"] is False
        assert _raw(f"{url}/peers", forged)[0] == 403
        assert _raw(f"{url}/api", {"X-Pyrtan-User-May": "chat"})[2]["chat_bots"] is False
    finally:
        hub.stop()
        server.stop()


# ---------------------------------------------------------------- the command


def test_pyrtan_login(door, monkeypatch, capsys):
    from pyrtan.cli import main
    monkeypatch.setenv("PYRTAN_PASSWORD", "alice-pw")
    assert main(["login", "--at", door.url, "--user", "alice"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert token.startswith("pyu_") and "alice may ask, operate" in out.err
    assert main(["logout", "--at", door.url, "--token", token]) == 0
    monkeypatch.setenv("PYRTAN_PASSWORD", "wrong")
    assert main(["login", "--at", door.url, "--user", "alice"]) == 1
    assert "not taken" in capsys.readouterr().err


def test_pyrtan_login_to_a_pyrtan_without_ldap(monkeypatch, capsys):
    from pyrtan.cli import main
    server = Server(Service(Registry.from_paths([EXAMPLES])).route, quiet=True, token="t").start()
    try:
        monkeypatch.setenv("PYRTAN_PASSWORD", "x")
        assert main(["login", "--at", server.url, "--user", "alice"]) == 1
        assert "not started with --ldap" in capsys.readouterr().err
    finally:
        server.stop()


def test_serve_refuses_ldap_without_a_token(monkeypatch, directory):
    from pyrtan.cli import main
    monkeypatch.delenv("PYRTAN_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="on top of a token"):
        main(["serve", str(EXAMPLES), "--port", "0", "--ldap", directory.url, "--ldap-user-dn", f"uid={{user}},{PEOPLE}"])
    monkeypatch.setenv("PYRTAN_TOKEN", "t")
    with pytest.raises(SystemExit, match="ABILITIES"):
        main(["serve", str(EXAMPLES), "--port", "0", "--ldap", directory.url, "--ldap-user-dn", f"uid={{user}},{PEOPLE}", "--ldap-group", "ops"])


def _node_playwright() -> bool:
    import shutil
    import subprocess
    return shutil.which("node") is not None and subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
def test_the_console_signs_in(door):
    import subprocess
    from pathlib import Path
    script = Path(__file__).parent / "browser" / "console_ldap.cjs"
    run = subprocess.run(["node", str(script), door.url, "carol", "carol-pw"], capture_output=True, text=True, timeout=120)
    lines = dict(line.split(" ", 1) for line in run.stdout.splitlines() if " " in line)
    assert run.returncode == 0 and "PAGEERROR" not in lines, run.stdout + run.stderr
    assert "directory account" in lines["FIELDS"]
    assert "signed in as carol" in lines["WHERE"]
    assert "pyu_" in lines["KEPT"] and "carol-pw" not in lines["KEPT"]  # the session token is kept, never the password
    assert "Oslo" in lines["ASKED"]


def test_serve_takes_ldap_nested(monkeypatch, directory):
    import pyrtan.cli as cli

    class Started(Exception):
        pass

    def started(*args, users=None, **kw):
        raise Started(users)

    monkeypatch.setattr(cli, "serve", started)
    monkeypatch.setenv("PYRTAN_TOKEN", "t")
    common = ["serve", str(EXAMPLES), "--ldap", directory.url, "--ldap-user-dn", f"uid={{user}},{PEOPLE}", "--ldap-group", f"{STAFF}=chat"]
    with pytest.raises(Started) as got:
        cli.main(common + ["--ldap-nested"])
    assert got.value.args[0].nested is True
    with pytest.raises(Started) as got:
        cli.main(common)
    assert got.value.args[0].nested is False
    monkeypatch.setenv("PYRTAN_LDAP_NESTED", "1")
    with pytest.raises(Started) as got:
        cli.main(common)
    assert got.value.args[0].nested is True
