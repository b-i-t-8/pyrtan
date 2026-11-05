"""OIDC sign-in on top of the token: the JWT arithmetic, the claims, the two legs
of the flow, and the door in front of a service and a hub. The provider is a
small one in this file, serving discovery, the authorization and token
endpoints, its JWKS and userinfo over a real socket, signing with RS256."""

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyrtan import Registry, RecordedTransport
from pyrtan.hub import Hub
from pyrtan.oidc import (
    CALLBACK_PATH, START_PATH, OidcError, OidcUsers, b64url_decode, b64url_encode, check_claims, parse_abilities,
    parse_group, rsa_verify, safe_next, split_jwt,
)
from pyrtan.service import Server, Service, http_json
from conftest import EXAMPLES, FIXTURES

# A 1024-bit RSA key for the provider, made once with openssl (never used for anything else).
N = 0xb5d8f48f64abe5172cf0bacfde8368d147d059aab181832031674e1b1996f0057d79586aa81467d67851bd3b61781f193c19219a0e8330d18bed2010fa3c04ad5472cac34b94c90063d8a86ffa937ab25d4746d3eceeae3c0ef35f0acf601379c9d08e99ca62f2fce982be5f1ebfb75a8bc77c5d494d663d2fc93911b64102ef
E = 65537
D = 0x8786b105a966beef1dcc2fbe3a3cbfc2b17de345e6d2fa31e4fff3e31898383c2f3e5f99e5b88786fcd7166c0a016753fc495294a95a36e9051aedba1974df9a38be10b5437c36888002cd7497d16c5af8b46c49e6f7e1ecac09fdd393e18fdf0bf5e1d23629757e27f84c8e1e04d2e7b2777906b28a6f5b58f93d0f989ed3c1
SIZE = 128
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
CLIENT, SECRET, REDIRECT = "pyrtan", "shh-secret", "http://hub.test"


def _sign(message: bytes, flip: bool = False) -> bytes:
    info = SHA256_INFO + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (SIZE - len(info) - 3) + b"\x00" + info
    sig = pow(int.from_bytes(em, "big"), D, N).to_bytes(SIZE, "big")
    return bytes([sig[0] ^ 1]) + sig[1:] if flip else sig


def _jwt(claims: dict, kid: str = "k1", alg: str = "RS256", flip: bool = False) -> str:
    head = b64url_encode(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = b64url_encode(json.dumps(claims).encode())
    signing = f"{head}.{body}".encode("ascii")
    sig = _sign(signing, flip) if alg.startswith("RS") else b"not-an-rsa-signature"
    return f"{head}.{body}." + b64url_encode(sig)


# ---------------------------------------------------------------- a small provider


class FakeProvider:
    """Discovery, /authorize, /token, /jwks and /userinfo; who signs in is ``person``."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.person = ("alice", ["family"])    # (preferred_username, groups) of whoever signs in next
        self.userinfo_only = False              # groups only at /userinfo, not in the ID token
        self.alg = "RS256"
        self.kid = "k1"
        self.flip = False                       # a bad signature
        self.aud = None                         # another audience
        self.bad_nonce = False
        self.issuer_claim = None                # another issuer in the token
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, str] = {}        # access token -> sub
        self.calls: list[str] = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, payload, extra=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
                provider.calls.append(path)
                if path == f"{prefix}/.well-known/openid-configuration":
                    return self._json(200, {"issuer": provider.issuer, "authorization_endpoint": f"{provider.base}/authorize?tenant=x", "token_endpoint": f"{provider.base}/token", "jwks_uri": f"{provider.base}/jwks", "userinfo_endpoint": f"{provider.base}/userinfo"})
                if path == "/authorize":
                    assert q["response_type"] == "code" and q["client_id"] == CLIENT and q["code_challenge_method"] == "S256" and q["tenant"] == "x"
                    code = "code-" + str(len(provider.codes))
                    provider.codes[code] = {"nonce": q["nonce"], "challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "person": provider.person}
                    return self._json(302, {"status": "see-other"}, {"Location": q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})})
                if path == "/jwks":
                    n = b64url_encode(N.to_bytes(SIZE, "big")); e = b64url_encode(E.to_bytes(3, "big"))
                    return self._json(200, {"keys": [{"kty": "EC", "kid": "ec", "crv": "P-256", "x": "x", "y": "y"}, {"kty": "RSA", "kid": provider.kid, "use": "sig", "alg": "RS256", "n": n, "e": e}]})
                if path == "/userinfo":
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    sub = provider.tokens.get(token)
                    if not sub:
                        return self._json(401, {"error": "invalid_token"})
                    name, groups = provider.person
                    return self._json(200, {"sub": sub, "preferred_username": name, "groups": groups, "email": f"{name}@example.org"})
                self._json(404, {"error": "not here"})

            def do_POST(self):
                provider.calls.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
                if self.path != "/token":
                    return self._json(404, {"error": "not here"})
                auth = self.headers.get("Authorization") or ""
                expected = "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
                if auth != expected:
                    return self._json(401, {"error": "invalid_client", "error_description": "who are you"})
                kept = provider.codes.pop(form.get("code", ""), None)
                if form.get("grant_type") != "authorization_code" or kept is None or kept["redirect_uri"] != form.get("redirect_uri"):
                    return self._json(400, {"error": "invalid_grant", "error_description": "no such code"})
                if b64url_encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != kept["challenge"]:
                    return self._json(400, {"error": "invalid_grant", "error_description": "the verifier does not match"})
                name, groups = kept["person"]
                sub = f"sub-{name}"
                now = int(time.time())
                claims = {"iss": provider.issuer_claim or provider.issuer, "sub": sub, "aud": provider.aud or CLIENT, "exp": now + 300, "iat": now, "nonce": "wrong" if provider.bad_nonce else kept["nonce"], "preferred_username": name}
                if not provider.userinfo_only:
                    claims["groups"] = groups
                access = "access-" + sub + "-" + str(now)
                provider.tokens[access] = sub
                self._json(200, {"access_token": access, "token_type": "Bearer", "id_token": _jwt(claims, provider.kid, provider.alg, provider.flip)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.issuer = self.base + prefix
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def provider():
    p = FakeProvider("/realms/home")
    yield p
    p.stop()


class _Stay(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_Stay)


def _get(url: str, headers: dict | None = None):
    """GET without following a redirect: (status, headers, JSON body)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with _opener.open(req) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


def _users(p: FakeProvider, **kw) -> OidcUsers:
    kw.setdefault("groups", {"family": ["ask", "chat"], "ops": ["operate"]})
    kw.setdefault("may", ["ask"])
    kw.setdefault("redirect", REDIRECT)
    return OidcUsers(p.issuer, CLIENT, SECRET, **kw)


def _sign_in(url: str, p: FakeProvider, next_path: str | None = None) -> tuple[int, dict, dict]:
    """The whole flow as a browser would walk it; the callback's answer."""
    status, headers, _ = _get(f"{url}{START_PATH}" + (f"?next={urllib.parse.quote(next_path, safe='')}" if next_path else ""))
    assert status == 302, status
    status, headers, _ = _get(headers["Location"])
    assert status == 302 and headers["Location"].startswith(REDIRECT + CALLBACK_PATH + "?")
    back = headers["Location"].partition("?")[2]
    return _get(f"{url}{CALLBACK_PATH}?{back}")


# ---------------------------------------------------------------- the arithmetic and the claims


def test_rsa_verify_checks_a_pkcs1_signature():
    message = b"hello"
    assert rsa_verify(N, E, _sign(message), message, "sha256")
    assert not rsa_verify(N, E, _sign(message, flip=True), message, "sha256")
    assert not rsa_verify(N, E, _sign(message), b"hullo", "sha256")
    assert not rsa_verify(N, E, _sign(message), message, "sha384")
    assert not rsa_verify(N, E, _sign(message)[1:], message, "sha256")  # the wrong length
    assert not rsa_verify(N, E, b"\xff" * SIZE, message, "sha256")      # not below the modulus


def test_base64url_and_jwt_splitting():
    assert b64url_decode(b64url_encode(b"\x00\xff\xfe")) == b"\x00\xff\xfe"
    with pytest.raises(ValueError):
        b64url_decode("not+base64/url")
    header, claims, sig, signed = split_jwt(_jwt({"sub": "x"}))
    assert header["alg"] == "RS256" and claims == {"sub": "x"} and rsa_verify(N, E, sig, signed, "sha256")
    for bad in ("", "a.b", "a.b.c", _jwt({"sub": "x"}) + ".d"):
        with pytest.raises(OidcError):
            split_jwt(bad)


def test_the_claims_core_asks_for():
    now = 1_700_000_000
    good = {"iss": "https://auth.example.org/", "sub": "s", "aud": CLIENT, "exp": now + 10, "iat": now - 10, "nonce": "n"}
    check_claims(good, "https://auth.example.org", CLIENT, "n", now)       # a trailing slash is the same issuer
    check_claims({**good, "aud": [CLIENT, "other"], "azp": CLIENT}, "https://auth.example.org", CLIENT, "n", now)
    refused = [
        ({**good, "iss": "https://evil.example"}, "not https://auth"),
        ({**good, "aud": "other"}, "another client"),
        ({**good, "aud": [CLIENT, "other"]}, "authorized party"),
        ({**good, "azp": "other"}, "authorized party"),
        ({**good, "exp": now - 120}, "expired"),
        ({**good, "exp": "soon"}, "expired"),
        ({**good, "iat": now + 120}, "future"),
        ({**good, "nonce": "m"}, "nonce"),
        ({k: v for k, v in good.items() if k != "sub"}, "subject"),
    ]
    for claims, why in refused:
        with pytest.raises(OidcError, match=why):
            check_claims(claims, "https://auth.example.org", CLIENT, "n", now)
    check_claims({**good, "exp": now - 30}, "https://auth.example.org", CLIENT, "n", now)  # a minute's skew
    check_claims({k: v for k, v in good.items() if k != "nonce"}, "https://auth.example.org", CLIENT, None, now)


def test_groups_abilities_and_next_are_parsed():
    assert parse_group("admins=operate,ask") == ("admins", ["ask", "operate"])
    assert parse_group("cn=ops,ou=groups=chat") == ("cn=ops,ou=groups", ["chat"])
    for bad in ("admins", "=ask", "admins=fly"):
        with pytest.raises(OidcError):
            parse_group(bad)
    assert parse_abilities(None) is None and parse_abilities("none") == [] and parse_abilities("run,ask") == ["ask", "run"]
    assert safe_next("/console") == "/console" and safe_next("/?hub=x") == "/?hub=x"
    for bad in ("https://evil.example", "//evil.example", "/a\\b", "console", "/a#b", None, "/\n"):
        assert safe_next(bad) == "/"


def test_construction_refuses_what_it_cannot_use(provider):
    with pytest.raises(OidcError, match="issuer"):
        OidcUsers("auth.example.org", CLIENT, redirect=REDIRECT)
    with pytest.raises(OidcError, match="client-id"):
        OidcUsers(provider.issuer, "", redirect=REDIRECT)
    with pytest.raises(OidcError, match="redirect"):
        OidcUsers(provider.issuer, CLIENT, redirect="hub.test")
    users = OidcUsers("https://auth.example.org/realms/home/", CLIENT, redirect="https://hub.test/", scopes=["profile"])
    assert users.may == {"ask"} and users.scopes == ["openid", "profile"] and users.provider == "auth.example.org"
    assert users.redirect_uri == "https://hub.test/auth/oidc/callback" and users.secure
    assert "shh" not in repr(OidcUsers(provider.issuer, CLIENT, "shh", redirect=REDIRECT))
    assert OidcUsers("http://auth.lan:9091", CLIENT, redirect=REDIRECT, groups={"a": ["ask"]}).may == frozenset()
    assert not OidcUsers("http://auth.lan:9091", CLIENT, redirect=REDIRECT).secure


# ---------------------------------------------------------------- the door


@pytest.fixture
def door(provider):
    reg = Registry.from_paths([EXAMPLES])
    svc = Service(reg, name="door", transport=RecordedTransport.from_file(FIXTURES / "examples.json"), token="s3cret")
    users = _users(provider)
    server = Server(svc.route, quiet=True, token="s3cret", oidc=users).start()
    server.users = users
    yield server
    server.stop()


def test_a_service_sends_people_to_the_provider_and_takes_them_back(door, provider):
    url = door.url
    status, headers, body = _get(f"{url}/capabilities")
    assert status == 401 and START_PATH in body["say"] and "127.0.0.1" in body["say"] and "/login" not in body["say"]
    assert headers["WWW-Authenticate"] == 'Bearer realm="pyrtan"'
    # /api says how to start, with no token.
    _, _, api = _get(f"{url}/api")
    assert api["oidc"] == {"start": START_PATH, "provider": "127.0.0.1"} and START_PATH in api["endpoints"] and "/logout" in api["endpoints"] and "login" not in api
    # The first leg: to the provider, with everything the code flow with PKCE needs.
    status, headers, body = _get(f"{url}{START_PATH}?next=/console")
    assert status == 302 and body["status"] == "see-other" and headers["Cache-Control"] == "no-store"
    where = urllib.parse.urlsplit(headers["Location"])
    q = {k: v[0] for k, v in urllib.parse.parse_qs(where.query).items()}
    assert f"{where.scheme}://{where.netloc}{where.path}" == f"{provider.base}/authorize" and q["tenant"] == "x"
    assert q["response_type"] == "code" and q["client_id"] == CLIENT and q["redirect_uri"] == REDIRECT + CALLBACK_PATH
    assert q["scope"] == "openid profile email" and q["code_challenge_method"] == "S256" and len(q["state"]) > 30 and len(q["nonce"]) > 30
    # The provider sends the browser back; the second leg opens a session and returns to `next`.
    status, headers, _ = _get(headers["Location"])
    back = headers["Location"].partition("?")[2]
    status, headers, body = _get(f"{url}{CALLBACK_PATH}?{back}")
    assert status == 302 and body["status"] == "ok" and body["user"] == "alice" and body["may"] == ["ask", "chat"]
    assert headers["Location"].startswith("/console#session=pyu_")
    token = urllib.parse.unquote(headers["Location"].partition("#session=")[2])
    assert provider.calls.count("/userinfo") == 0  # the groups were in the ID token
    # The session is a bearer, checked like an LDAP one: /gaps is operate's.
    assert http_json("GET", f"{url}/capabilities", token=token)[0] == 200
    status, out = http_json("GET", f"{url}/gaps", token=token)
    assert status == 403 and out["reason"] == "not-granted" and "alice may not operate" in out["say"]
    assert "trees" not in http_json("GET", f"{url}/health", token=token)[1]
    _, api = http_json("GET", f"{url}/api", token=token)
    assert api["user"] == {"user": "alice", "sub": "sub-alice", "issuer": provider.issuer, "may": ["ask", "chat"]}
    status, out = http_json("POST", f"{url}/intent", {"text": "weather in Oslo"}, token=token)
    assert status == 200 and out["status"] == "done"
    assert len(door.users) == 1
    # The callback is one-shot: the same code and state again is nobody.
    status, headers, body = _get(f"{url}{CALLBACK_PATH}?{back}")
    assert status == 302 and body["reason"] == "no-such-sign-in" and headers["Location"].startswith("/#signin-failed=")
    # /logout ends it.
    assert http_json("POST", f"{url}/logout", {}, token=token)[1]["ended"] is True
    assert http_json("GET", f"{url}/capabilities", token=token)[0] == 401
    assert http_json("POST", f"{url}/logout", {}, token=token)[1]["ended"] is False
    assert http_json("POST", f"{url}/logout", {}, token="s3cret")[0] == 400


def test_groups_come_from_userinfo_when_the_id_token_has_none(door, provider):
    provider.userinfo_only = True
    provider.person = ("bob", ["family", "ops"])
    status, headers, body = _sign_in(door.url, provider)
    assert status == 302 and body["user"] == "bob" and body["may"] == ["ask", "chat", "operate"]
    assert provider.calls.count("/userinfo") == 1
    token = urllib.parse.unquote(headers["Location"].partition("#session=")[2])
    assert http_json("GET", f"{door.url}/gaps", token=token)[0] == 200
    assert "trees" in http_json("GET", f"{door.url}/health", token=token)[1]


def test_what_the_provider_vouches_for_is_still_checked(door, provider):
    url = door.url
    usual = {"flip": False, "aud": None, "bad_nonce": False, "issuer_claim": None, "alg": "RS256", "kid": "k1"}
    for attr, value, why in (("flip", True, "signature"), ("aud", "someone-else", "another client"), ("bad_nonce", True, "nonce"), ("issuer_claim", "https://evil.example", "not " + provider.issuer), ("alg", "none", "unsigned"), ("kid", "k9", "key set does not hold")):
        setattr(provider, attr, value)
        status, headers, body = _sign_in(url, provider)
        setattr(provider, attr, usual[attr])
        assert status == 302 and body["reason"] == "provider-failed" and why in body["say"], (attr, body)
        assert headers["Location"].startswith("/#signin-failed=") and "session" not in headers["Location"]
    assert len(door.users) == 0
    # Another algorithm came over the server's own connection: taken, as Core allows; the claims are still read.
    provider.alg = "ES256"
    status, _, body = _sign_in(url, provider)
    assert status == 302 and body["status"] == "ok"
    provider.alg = "ES256"; provider.aud = "someone-else"
    assert _sign_in(url, provider)[2]["reason"] == "provider-failed"


def test_the_key_set_is_fetched_again_for_a_key_it_does_not_hold(door, provider):
    assert _sign_in(door.url, provider)[2]["status"] == "ok"
    provider.kid = "k2"
    assert _sign_in(door.url, provider)[2]["reason"] == "provider-failed"  # fetched a minute ago: not again yet
    door.users._jwks_at = 0
    assert _sign_in(door.url, provider)[2]["status"] == "ok"
    assert provider.calls.count("/jwks") == 2


def test_a_person_in_no_group_is_sent_back_with_a_message(provider):
    svc = Service(Registry.from_paths([EXAMPLES]), token="s3cret")
    users = _users(provider, may=[], groups={"ops": ["operate"]}, provider="Keycloak")
    server = Server(svc.route, quiet=True, token="s3cret", oidc=users).start()
    try:
        provider.person = ("carol", [])
        status, headers, body = _sign_in(server.url, provider)
        assert status == 302 and body["reason"] == "not-granted" and "carol signed in, but" in body["say"]
        assert headers["Location"] == "/#signin-failed=" + urllib.parse.quote(body["say"], safe="")
        assert len(users) == 0
        assert _get(f"{server.url}/api")[2]["oidc"]["provider"] == "Keycloak"
        assert "Keycloak" in _get(f"{server.url}/capabilities")[2]["say"]
    finally:
        server.stop()


def test_next_is_a_path_here_and_the_provider_may_refuse(door, provider):
    url = door.url
    for elsewhere in ("https://evil.example/", "//evil.example"):
        status, headers, _ = _sign_in(url, provider, elsewhere)
        assert status == 302 and headers["Location"].startswith("/#session=")
    # The provider said no (the person cancelled): back with its words, and the state spent.
    status, headers, _ = _get(f"{url}{START_PATH}?next=/console")
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["state"][0]
    status, headers, body = _get(f"{url}{CALLBACK_PATH}?error=access_denied&error_description=you+said+no&state={state}")
    assert status == 302 and body["reason"] == "refused" and "you said no" in body["say"] and headers["Location"].startswith("/console#signin-failed=")
    assert _get(f"{url}{CALLBACK_PATH}?code=x&state={state}")[2]["reason"] == "no-such-sign-in"
    assert _get(f"{url}{CALLBACK_PATH}")[2]["reason"] == "no-such-sign-in"
    assert _get(f"{url}{CALLBACK_PATH}?state=never")[2]["reason"] == "no-such-sign-in"


def test_a_provider_that_is_down_says_so():
    gone = FakeProvider(); gone.stop()
    users = OidcUsers(gone.issuer, CLIENT, redirect=REDIRECT)
    svc = Service(Registry.from_paths([EXAMPLES]), token="s3cret")
    server = Server(svc.route, quiet=True, token="s3cret", oidc=users).start()
    try:
        status, headers, body = _get(f"{server.url}{START_PATH}")
        assert status == 302 and body["reason"] == "provider-down" and "could not start" in body["say"] and headers["Location"].startswith("/#signin-failed=")
        with pytest.raises(OidcError, match="did not answer"):
            users.config()
    finally:
        server.stop()


def test_discovery_must_name_the_same_issuer(provider):
    users = OidcUsers(provider.base, CLIENT, redirect=REDIRECT)  # the host, not the realm: the document says another issuer
    with pytest.raises(OidcError, match="not the same provider|no discovery document"):
        users.config()


def test_oidc_needs_a_token(provider):
    with pytest.raises(ValueError, match="token"):
        Server(lambda *a: (200, {}), quiet=True, oidc=_users(provider))


def test_a_hub_takes_the_sign_in_too(provider):
    hub = Hub(poll_every=60, token="s3cret", chat=True)
    server = Server(hub.route, quiet=True, token="s3cret", oidc=_users(provider), keyed=True).start()
    try:
        provider.person = ("alice", ["family"])
        status, headers, body = _sign_in(server.url, provider)
        token = urllib.parse.unquote(headers["Location"].partition("#session=")[2])
        assert http_json("GET", f"{server.url}/capabilities", token=token)[0] == 200
        assert http_json("GET", f"{server.url}/peers", token=token)[0] == 403
        _, api = http_json("GET", f"{server.url}/api", token=token)
        assert api["pyrtan"] == "hub" and api["user"]["user"] == "alice"
    finally:
        server.stop()


def _node_playwright() -> bool:
    import shutil
    import subprocess
    return shutil.which("node") is not None and subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="node with playwright is not installed")
def test_the_console_signs_in(provider):
    """tests/browser/console_oidc.cjs: the page shows the button, walks to the provider and
    back, keeps the session from the fragment, and is signed in."""
    import subprocess
    from pathlib import Path
    svc = Service(Registry.from_paths([EXAMPLES]), name="door", transport=RecordedTransport.from_file(FIXTURES / "examples.json"), token="s3cret")
    users = _users(provider, provider="Fake IdP")
    server = Server(svc.route, quiet=True, token="s3cret", oidc=users).start()
    users.redirect, users.redirect_uri = server.url, server.url + CALLBACK_PATH  # where the provider sends the browser: this server, known only now
    try:
        script = Path(__file__).parent / "browser" / "console_oidc.cjs"
        run = subprocess.run(["node", str(script), server.url], capture_output=True, text=True, timeout=120)
    finally:
        server.stop()
    lines = dict(line.split(" ", 1) for line in run.stdout.splitlines() if " " in line)
    assert run.returncode == 0 and "PAGEERROR" not in lines, run.stdout + run.stderr
    assert "Sign in with Fake IdP" in lines["BUTTON"] and "Fake IdP" in lines["FIELDS"]
    assert lines["URL"].rstrip("/") == server.url and "#" not in lines["URL"]  # the fragment was taken and dropped
    assert "signed in as alice" in lines["WHERE"]
    assert "pyu_" in lines["KEPT"]
    assert "Oslo" in lines["ASKED"]


# ---------------------------------------------------------------- the command


def test_serve_takes_oidc(monkeypatch, provider):
    import pyrtan.cli as cli

    class Started(Exception):
        pass

    def started(*args, oidc=None, **kw):
        raise Started(oidc)

    monkeypatch.setattr(cli, "serve", started)
    monkeypatch.delenv("PYRTAN_TOKEN", raising=False)
    common = ["serve", str(EXAMPLES), "--oidc", provider.issuer, "--oidc-client-id", CLIENT, "--oidc-group", "family=ask,chat", "--oidc-group", "admins=operate"]
    with pytest.raises(SystemExit, match="--token"):
        cli.main(common + ["--oidc-redirect", REDIRECT])
    monkeypatch.setenv("PYRTAN_TOKEN", "t")
    with pytest.raises(SystemExit, match="--oidc-redirect"):
        cli.main(common)
    with pytest.raises(SystemExit, match="--oidc-client-id"):
        cli.main(["serve", str(EXAMPLES), "--oidc", provider.issuer, "--oidc-redirect", REDIRECT])
    with pytest.raises(Started) as got:
        cli.main(common + ["--oidc-redirect", REDIRECT + "/", "--oidc-provider", "Home", "--oidc-scope", "openid groups", "--oidc-claim", "roles"])
    users = got.value.args[0]
    assert isinstance(users, OidcUsers) and users.groups == {"family": ["ask", "chat"], "admins": ["operate"]} and users.may == frozenset()
    assert users.redirect_uri == REDIRECT + CALLBACK_PATH and users.provider == "Home" and users.scopes == ["openid", "groups"] and users.claim == "roles"
    with pytest.raises(Started) as got:  # --advertise is where the browser reaches it, so it will do for the redirect
        cli.main(["serve", str(EXAMPLES), "--oidc", provider.issuer, "--oidc-client-id", CLIENT, "--advertise", "https://hub.example.net"])
    assert got.value.args[0].redirect_uri == "https://hub.example.net" + CALLBACK_PATH and got.value.args[0].may == {"ask"}
    monkeypatch.setenv("PYRTAN_OIDC_CLIENT_SECRET", "from-env")
    monkeypatch.setenv("PYRTAN_OIDC_GROUPS", "a=ask;b=run")
    with pytest.raises(Started) as got:
        cli.main(["serve", str(EXAMPLES), "--oidc", provider.issuer, "--oidc-client-id", CLIENT, "--oidc-redirect", REDIRECT])
    assert got.value.args[0].client_secret == "from-env" and got.value.args[0].groups == {"a": ["ask"], "b": ["run"]}


def test_oidc_auto_says_the_address_it_works_at():
    plain = OidcUsers("https://auth.example.org", CLIENT, redirect="https://hub.example.net")
    assert "auto" not in plain.to_dict()
    auto = OidcUsers("https://auth.example.org", CLIENT, redirect="https://hub.example.net/", auto=True)
    assert auto.to_dict() == {"start": "/auth/oidc", "provider": "auth.example.org", "auto": "https://hub.example.net"}
