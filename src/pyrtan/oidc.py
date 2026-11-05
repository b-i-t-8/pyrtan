"""OIDC sign-in: people who sign in to a service or hub with the account they
already have at an OpenID Connect provider, on top of its token.

The token stays what it was: the operator's, and the swarm's. With ``--oidc
ISSUER`` a service or hub also sends a person to the provider (Keycloak,
Authentik, Authelia, Entra, Google, Okta, anything that publishes
``/.well-known/openid-configuration``) and takes them back with a session,
and what that person may do here is decided by a claim the provider puts in
the ID token, usually ``groups``, in the same four abilities a key is
granted (``_meta/identity.md`` §7): ``ask``, ``chat``, ``run`` and
``operate``.

**The flow** is the authorization code flow with PKCE (RFC 7636), the one
OpenID Connect Core names for a web application, and nothing else: no
implicit flow, no tokens in a query string.

- ``GET /auth/oidc[?next=/path]`` makes a ``state`` (the browser's return
  ticket), a ``nonce`` (bound into the ID token) and a PKCE verifier, keeps
  the three for ten minutes, and answers ``302`` to the provider's
  authorization endpoint.
- The provider sends the browser back to ``GET /auth/oidc/callback?code=…
  &state=…`` (the ``--oidc-redirect`` address plus that path, which is
  what the provider must be told). The state is spent, the code is
  exchanged at the token endpoint over the server's own connection
  (``client_secret_basic`` when there is a secret, PKCE either way), and
  the ID token is read: ``iss`` is the issuer, ``aud`` is the client,
  ``exp`` has not passed, ``nonce`` is the one kept. An ``RS256``,
  ``RS384`` or ``RS512`` signature is checked against the provider's JWKS
  in arithmetic the standard library has; any other algorithm is taken on
  the strength of the direct TLS connection the token arrived over, as
  Core §3.1.3.7 allows, and ``none`` is never taken. If the groups claim is
  not in the ID token, the userinfo endpoint is asked.
- The person is then a session, ``pyu_…`` exactly like an LDAP sign-in's
  (:mod:`pyrtan.ldap`), handed to the browser in the fragment of a
  redirect to the console (``/#session=…``): a fragment never reaches a
  server or a log. The console keeps it as it keeps a token and sends it as
  a bearer until it expires (twelve hours by default) or ``POST /logout``.

**Groups.** ``--oidc-group VALUE=ABILITIES`` (repeatable) gives the people
whose claim holds ``VALUE`` those abilities; the claim is ``groups`` unless
``--oidc-claim`` names another (``email`` makes ``--oidc-group
alice@example.org=operate`` a grant to one person). A person with no
listed value may what ``--oidc-may`` says: ``ask`` when no group is
listed, nothing otherwise. Someone the provider vouches for who may
nothing is sent back with a message, not a session. Groups are read at
sign-in; a session keeps what it was given until it expires.

**Single sign-on.** With ``--oidc-auto`` the console does not wait for the
button: opened at the ``--oidc-redirect`` address with no session, or with
one that has expired, it goes to the provider at once, and someone already
signed in there comes back signed in here without doing anything, and the
console's connect box stays out of the way once they are. At any
other address the console still asks. It tries once a minute at most, and
a failed sign-in or forgetting the hub turns it off for that tab until a
sign-in works, so the message stays and the token box can be reached.

**What this refuses.** A state it did not make, or one already spent; an
ID token for another client or from another issuer, one that has expired,
or one whose nonce is not the one kept; a redirect anywhere but a path
on this Pyrtan (``next`` is a path, never another site); OIDC without a
token, for the reason LDAP is refused without one. Discovery and the JWKS
are fetched on first use and kept, the JWKS fetched again when a signature
names a key it does not hold, at most once a minute.

A dependency is not among the things this needs: discovery, the exchange,
the JWKS and the RSA check are ``urllib``, ``json``, ``hashlib`` and
``pow``. The provider should be ``https://`` unless it is on this machine:
the client secret, the code and the tokens cross that connection.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .identity import ABILITIES
from .ldap import SESSION_PREFIX, SESSION_TTL, Sessions, User

START_PATH = "/auth/oidc"
CALLBACK_PATH = "/auth/oidc/callback"
DEFAULT_CLAIM = "groups"
DEFAULT_NAME_CLAIMS = ("preferred_username", "email", "sub")
DEFAULT_SCOPES = ("openid", "profile", "email")
PENDING_TTL = 600.0            # how long a sign-in may take between /auth/oidc and the callback
MAX_PENDING = 1000
JWKS_REFRESH = 60.0            # the JWKS is fetched again for an unknown key at most this often
CLOCK_SKEW = 60.0              # how far a token's exp or iat may be off this clock
MAX_DOC = 1 << 20              # the most read of a discovery, token, JWKS or userinfo answer
# PKCS#1 v1.5 DigestInfo prefixes (RFC 8017 §9.2, note 1).
_DIGEST_INFO = {
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}
_RSA_ALGS = {"RS256": "sha256", "RS384": "sha384", "RS512": "sha512"}


class OidcError(Exception):
    """The provider could not be asked, or answered something this cannot take."""


# ---------------------------------------------------------------- encodings


def b64url_decode(text: str) -> bytes:
    """Base64url without padding, as JWTs and JWKs carry it; ValueError if it is not that."""
    if not isinstance(text, str) or any(c not in "-_=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" for c in text):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as e:
        raise ValueError("not base64url") from e


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def rsa_verify(n: int, e: int, signature: bytes, message: bytes, hash_name: str) -> bool:
    """RSASSA-PKCS1-v1_5 (RFC 8017 §8.2.2): the signature, raised to the public exponent,
    is the padded DigestInfo of the message. Compared in constant time."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size or hash_name not in _DIGEST_INFO:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    recovered = pow(s, e, n).to_bytes(size, "big")
    info = _DIGEST_INFO[hash_name] + hashlib.new(hash_name, message).digest()
    if size < len(info) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(info) - 3) + b"\x00" + info
    return hmac.compare_digest(recovered, expected)


def jwk_rsa(key: dict[str, Any]) -> tuple[int, int] | None:
    """(n, e) from an RSA JWK, or None if it is not one this can use."""
    if not isinstance(key, dict) or key.get("kty") != "RSA" or key.get("use", "sig") != "sig":
        return None
    try:
        n = int.from_bytes(b64url_decode(str(key["n"])), "big")
        e = int.from_bytes(b64url_decode(str(key["e"])), "big")
    except (KeyError, ValueError, TypeError):
        return None
    return (n, e) if n > 0 and e > 0 else None


def split_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """(header, claims, signature, signed input) of a compact JWS; OidcError if it is not one."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise OidcError("the ID token is not a JWT")
    try:
        header = json.loads(b64url_decode(parts[0]).decode("utf-8"))
        claims = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        signature = b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError) as e:
        raise OidcError(f"the ID token could not be read: {e}") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("the ID token is not a JWT")
    return header, claims, signature, f"{parts[0]}.{parts[1]}".encode("ascii")


def check_claims(claims: dict[str, Any], issuer: str, client_id: str, nonce: str | None, now: float | None = None) -> None:
    """What OpenID Connect Core §3.1.3.7 asks of an ID token's claims; OidcError when one fails."""
    now = time.time() if now is None else now
    if _same_issuer(str(claims.get("iss", "")), issuer) is False:
        raise OidcError(f"the ID token is from {claims.get('iss')!r}, not {issuer}")
    aud = claims.get("aud")
    audience = aud if isinstance(aud, list) else [aud]
    if client_id not in audience:
        raise OidcError("the ID token is for another client")
    if len(audience) > 1 and claims.get("azp") != client_id:
        raise OidcError("the ID token is for several clients and names another as the authorized party")
    if "azp" in claims and claims["azp"] != client_id:
        raise OidcError("the ID token names another client as the authorized party")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + CLOCK_SKEW <= now:
        raise OidcError("the ID token has expired")
    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and iat - CLOCK_SKEW > now:
        raise OidcError("the ID token was issued in the future")
    if nonce is not None and claims.get("nonce") != nonce:
        raise OidcError("the ID token's nonce is not this sign-in's")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise OidcError("the ID token names no subject")


def _same_issuer(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def parse_group(spec: str) -> tuple[str, list[str]]:
    """``admins=ask,operate`` as (claim value, abilities)."""
    from .grants import parse_may
    from .identity import IdentityError
    value, eq, may = spec.rpartition("=")
    if not eq or not value.strip():
        raise OidcError(f"--oidc-group wants VALUE=ABILITIES, e.g. admins=ask,operate; not {spec!r}")
    try:
        return value.strip(), parse_may(may)
    except IdentityError as e:
        raise OidcError(f"--oidc-group {spec!r}: {e}") from None


def parse_abilities(text: str | None) -> list[str] | None:
    """``--oidc-may``: abilities, ``none`` for none, or None (the default) when not given."""
    from .grants import parse_may
    from .identity import IdentityError
    if text is None:
        return None
    if text.strip().lower() in ("", "none"):
        return []
    try:
        return parse_may(text)
    except IdentityError as e:
        raise OidcError(str(e)) from None


def safe_next(value: Any) -> str:
    """Where to send the browser after the sign-in: a path on this Pyrtan, never
    another site (``//evil``, ``https://evil``, a backslash a browser would read as a slash)."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 0x20 or c in "#" for c in value):
        return "/"
    return value


# ---------------------------------------------------------------- the provider


class OidcUsers:
    """Who may sign in, by an OpenID Connect provider, and the sessions they hold."""

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str | None = None,
        *,
        redirect: str,
        groups: dict[str, list[str]] | None = None,
        may: list[str] | None = None,
        claim: str = DEFAULT_CLAIM,
        name_claim: str | None = None,
        scopes: list[str] | None = None,
        provider: str | None = None,
        auto: bool = False,
        ca: str | None = None,
        timeout: float = 10.0,
        session_ttl: float = SESSION_TTL,
    ) -> None:
        parts = urllib.parse.urlsplit(issuer)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
            raise OidcError(f"{issuer!r} is not the provider's issuer URL (https://auth.example.org/realms/home)")
        if not isinstance(client_id, str) or not client_id.strip():
            raise OidcError("say which client this Pyrtan is at the provider: --oidc-client-id")
        where = urllib.parse.urlsplit(redirect or "")
        if where.scheme not in ("http", "https") or not where.hostname or where.query or where.fragment:
            raise OidcError("say where the provider sends people back: --oidc-redirect https://hub.example.net (this Pyrtan's address as a browser reaches it)")
        if not claim or not claim.strip():
            raise OidcError("--oidc-claim needs the claim's name")
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id.strip()
        self.client_secret = client_secret or None
        self.redirect = redirect.rstrip("/")
        self.redirect_uri = self.redirect + CALLBACK_PATH
        self.groups = dict(groups or {})
        self.may = frozenset(may if may is not None else ([] if self.groups else ["ask"]))
        self.claim = claim.strip()
        self.name_claims = (name_claim.strip(),) + DEFAULT_NAME_CLAIMS if name_claim and name_claim.strip() else DEFAULT_NAME_CLAIMS
        self.scopes = [s for s in (scopes if scopes is not None else list(DEFAULT_SCOPES)) if s]
        if "openid" not in self.scopes:
            self.scopes.insert(0, "openid")
        self.provider = (provider or "").strip() or (parts.hostname or "")
        self.auto = auto
        self.timeout = timeout
        self.session_ttl = session_ttl
        self.sessions = Sessions(session_ttl)
        loopback = (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
        self.secure = parts.scheme == "https" or loopback
        self._context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, str, str, float]] = {}   # state -> (nonce, verifier, next, until)
        self._config: dict[str, Any] | None = None
        self._jwks: dict[str, tuple[int, int]] = {}
        self._jwks_at = 0.0

    def __repr__(self) -> str:  # never the client secret
        return f"OidcUsers({self.issuer!r}, client={self.client_id!r}, groups={len(self.groups)})"

    def to_dict(self) -> dict[str, Any]:
        """What ``/api`` says: where a browser starts, and what to call the provider."""
        said = {"start": START_PATH, "provider": self.provider}
        if self.auto:
            parts = urllib.parse.urlsplit(self.redirect)
            said["auto"] = f"{parts.scheme}://{parts.netloc}"
        return said

    # -------------------------------------------------------- the provider's documents

    def _http(self, method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json", "User-Agent": "pyrtan", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._context) as resp:
                return resp.status, resp.read(MAX_DOC + 1)
        except urllib.error.HTTPError as e:
            return e.code, e.read(MAX_DOC + 1)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise OidcError(f"{url} did not answer: {getattr(e, 'reason', e)}") from None

    def _json(self, method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        status, raw = self._http(method, url, data, headers)
        if len(raw) > MAX_DOC:
            raise OidcError(f"{url} answered more than this reads")
        try:
            return status, json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OidcError(f"{url} did not answer JSON") from None

    def config(self) -> dict[str, Any]:
        """The provider's discovery document, fetched once (OpenID Connect Discovery §4)."""
        with self._lock:
            if self._config is not None:
                return self._config
        status, doc = self._json("GET", self.issuer + "/.well-known/openid-configuration")
        if status != 200 or not isinstance(doc, dict):
            raise OidcError(f"{self.issuer} has no discovery document ({status})")
        if not _same_issuer(str(doc.get("issuer", "")), self.issuer):
            raise OidcError(f"{self.issuer} says its issuer is {doc.get('issuer')!r}: that is not the same provider")
        for key in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(doc.get(key), str) or not doc[key].startswith(("https://", "http://")):
                raise OidcError(f"{self.issuer} names no {key}")
        with self._lock:
            self._config = doc
        return doc

    def _key(self, kid: str | None, jwks_uri: str | None) -> tuple[int, int] | None:
        """The RSA key a signature names, from the JWKS, fetched when unknown (at most once a minute)."""
        with self._lock:
            known = self._jwks.get(kid or "")
            stale = time.time() - self._jwks_at > JWKS_REFRESH
        if known is not None or not stale or not jwks_uri:
            return known
        status, doc = self._json("GET", jwks_uri)
        if status != 200 or not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
            raise OidcError(f"{jwks_uri} did not answer a key set ({status})")
        keys = {}
        for key in doc["keys"]:
            pair = jwk_rsa(key)
            if pair is not None:
                keys[str(key.get("kid") or "")] = pair
        with self._lock:
            self._jwks = keys
            self._jwks_at = time.time()
        return keys.get(kid or "")

    def verify(self, id_token: str, nonce: str | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """The claims of an ID token this takes: the signature (RS*), then the claims."""
        header, claims, signature, signed = split_jwt(id_token)
        alg = str(header.get("alg") or "")
        if alg.lower() in ("", "none"):
            raise OidcError("the ID token is unsigned")
        if alg in _RSA_ALGS:
            jwks_uri = (config or {}).get("jwks_uri")
            pair = self._key(header.get("kid"), jwks_uri if isinstance(jwks_uri, str) else None)
            if pair is None:
                raise OidcError(f"the ID token is signed with a key ({header.get('kid') or 'no kid'}) the provider's key set does not hold")
            if not rsa_verify(pair[0], pair[1], signature, signed, _RSA_ALGS[alg]):
                raise OidcError("the ID token's signature does not check")
        # Another algorithm (ES256, EdDSA, HS256): the token came over the server's own
        # TLS connection to the token endpoint, which Core §3.1.3.7 lets stand for the signature.
        check_claims(claims, self.issuer, self.client_id, nonce)
        return claims

    # -------------------------------------------------------- the two routes

    def start(self, query: str | dict[str, Any] | None) -> tuple[int, Any, dict[str, str]]:
        """``GET /auth/oidc``: to the provider, with a state, a nonce and a PKCE challenge."""
        params = _query(query)
        try:
            config = self.config()
        except OidcError as e:
            return self._failed(503, "provider-down", f"The sign-in could not start: {e}.")
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = time.time()
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                self._pending = {k: v for k, v in self._pending.items() if v[3] > now}
                while len(self._pending) >= MAX_PENDING:
                    self._pending.pop(min(self._pending, key=lambda k: self._pending[k][3]))
            self._pending[state] = (nonce, verifier, safe_next(params.get("next")), now + PENDING_TTL)
        challenge = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
        wanted = {
            "response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes), "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        endpoint = config["authorization_endpoint"]
        url = endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(wanted)
        return 302, {"status": "see-other", "say": f"Sign in with {self.provider}.", "provider": self.provider}, {"Location": url, "Cache-Control": "no-store"}

    def callback(self, query: str | dict[str, Any] | None) -> tuple[int, Any, dict[str, str]]:
        """``GET /auth/oidc/callback``: the code for a session, and the browser back to the console."""
        params = _query(query)
        state = params.get("state")
        with self._lock:
            kept = self._pending.pop(state, None) if isinstance(state, str) and state else None
        if kept is None or kept[3] <= time.time():
            return self._failed(400, "no-such-sign-in", "This sign-in was not started here, or took longer than ten minutes: start again.")
        nonce, verifier, next_path, _ = kept
        if params.get("error"):
            return self._failed(400, "refused", f"{self.provider} did not sign you in: {params.get('error_description') or params['error']}.", next_path)
        code = params.get("code")
        if not isinstance(code, str) or not code:
            return self._failed(400, "no-code", f"{self.provider} sent no code back.", next_path)
        try:
            user = self._exchange(code, verifier, nonce)
        except OidcError as e:
            return self._failed(502, "provider-failed", f"The sign-in did not finish: {e}.", next_path)
        if not user.may:
            return self._failed(403, "not-granted", f"{user.name} signed in, but is in none of the groups this Pyrtan lets in.", next_path)
        token, expires = self.sessions.open(user)
        return 302, {"status": "ok", "say": f"Signed in as {user.name}.", "expires": expires, **user.to_dict()}, {"Location": f"{next_path}#session={urllib.parse.quote(token, safe='')}", "Cache-Control": "no-store"}

    def _failed(self, status: int, reason: str, say: str, next_path: str = "/") -> tuple[int, Any, dict[str, str]]:
        """Back to the console with what went wrong, where it shows the message; the body says it too."""
        return 302, {"status": "error", "reason": reason, "say": say, "would_be": status}, {"Location": f"{next_path}#signin-failed={urllib.parse.quote(say, safe='')}", "Cache-Control": "no-store"}

    def _exchange(self, code: str, verifier: str, nonce: str) -> User:
        config = self.config()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri, "client_id": self.client_id, "code_verifier": verifier}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client_secret:
            # client_secret_basic (RFC 6749 §2.3.1): the one method every provider must take.
            pair = urllib.parse.quote(self.client_id, safe="") + ":" + urllib.parse.quote(self.client_secret, safe="")
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        status, answer = self._json("POST", config["token_endpoint"], urllib.parse.urlencode(form).encode("ascii"), headers)
        if status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("id_token"), str):
            why = answer.get("error_description") or answer.get("error") if isinstance(answer, dict) else None
            raise OidcError(f"the token endpoint answered {status}" + (f": {why}" if why else ""))
        claims = self.verify(answer["id_token"], nonce, config)
        access = answer.get("access_token")
        if self.claim not in claims and isinstance(config.get("userinfo_endpoint"), str) and isinstance(access, str) and access:
            status, info = self._json("GET", config["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"})
            if status == 200 and isinstance(info, dict) and info.get("sub") == claims["sub"]:
                claims = {**info, **claims, self.claim: info.get(self.claim)} if self.claim in info else {**info, **claims}
        return self.person(claims)

    def person(self, claims: dict[str, Any]) -> User:
        """Who the claims say, and what their groups let them do here."""
        held = claims.get(self.claim)
        values = held if isinstance(held, list) else [held] if isinstance(held, str) else []
        may = set(self.may)
        for value in values:
            may.update(self.groups.get(str(value), []))
        name = next((str(claims[c]) for c in self.name_claims if isinstance(claims.get(c), (str, int)) and str(claims[c]).strip()), str(claims["sub"]))
        return User(name, str(claims["sub"]), frozenset(a for a in ABILITIES if a in may), issuer=self.issuer)

    # -------------------------------------------------------- the Authorization header

    def presented(self, header: str | None) -> bool:
        """Whether a header is this door's: a session token."""
        if not header:
            return False
        scheme, _, value = header.strip().partition(" ")
        return scheme.lower() == "bearer" and value.strip().startswith(SESSION_PREFIX)

    def check(self, header: str | None) -> User | None:
        """The person an ``Authorization`` header is, or None."""
        if not header:
            return None
        scheme, _, value = header.strip().partition(" ")
        return self.sessions.get(value.strip()) if scheme.lower() == "bearer" else None

    def session(self, token: str) -> User | None:
        return self.sessions.get(token)

    def logout(self, header: str | None) -> tuple[int, dict[str, Any]]:
        """``POST /logout``: the session token in the header is no longer taken."""
        scheme, _, value = (header or "").strip().partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not value.startswith(SESSION_PREFIX):
            return 400, {"status": "error", "reason": "bad-request", "say": "Send the session token as Authorization: Bearer pyu_…."}
        return 200, {"status": "ok", "ended": self.sessions.close(value)}

    def __len__(self) -> int:
        return len(self.sessions)


def _query(query: str | dict[str, Any] | None) -> dict[str, str]:
    if isinstance(query, dict):
        return {str(k): str(v) for k, v in query.items()}
    out = {}
    for k, v in urllib.parse.parse_qs(query or "", keep_blank_values=True).items():
        out[k] = v[0] if v else ""
    return out
