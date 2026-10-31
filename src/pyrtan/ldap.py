"""LDAP users: people who sign in to a service or hub with the account they
already have, on top of its token.

The token stays what it was: the operator's, and the swarm's, the one
credential every service and hub in a swarm shares. It is not something to
hand to every person in an office. With ``--ldap URL`` a service or hub also
takes a person's directory account, and what that person may do here is
decided by the directory's groups, in the same four abilities a key is
granted (``_meta/identity.md`` §7): ``ask`` (the trees), ``chat`` (the
lobby, bots included), ``run`` (be a trusted member) and ``operate``
(everything the token allows).

A person presents the account in one of two ways:

- ``Authorization: Basic`` with their user name and password, on any
  request. The bind is remembered for a minute, so a client asking twice
  a second does not bind twice a second.
- ``POST /login`` ``{"user": ..., "password": ...}``, which answers with a
  session token (``pyu_…``) to send as ``Authorization: Bearer`` until it
  expires (twelve hours by default) or ``POST /logout``. This is what the
  console and ``pyrtan login`` use: the password is sent once and never
  kept.

**Finding the person.** Either a DN template, ``--ldap-user-dn
"uid={user},ou=people,dc=example,dc=org"``, bound directly; or a search,
``--ldap-base DN`` with ``--ldap-filter`` (default ``(uid={user})``),
anonymously or as ``--ldap-bind-dn`` with ``$PYRTAN_LDAP_BIND_PASSWORD``,
which must find exactly one entry, whose DN is then bound with the
person's password. ``{user}`` is escaped for where it lands (RFC 4514 in a
DN, RFC 4515 in a filter), so a user name cannot widen a search.

**Groups.** ``--ldap-group DN=ABILITIES`` (repeatable) gives a group's
members those abilities. A person is a member when the group entry names
them in ``member``, ``uniqueMember`` (their DN) or ``memberUid`` (their
user name): groupOfNames, groupOfUniqueNames, posixGroup and Active
Directory's groups alike. Nested groups are not followed unless
``--ldap-nested`` says so; then a person in a group that is itself a member
of a listed group (to any depth) is a member too, by asking Active
Directory with its transitive matching rule, ``LDAP_MATCHING_RULE_IN_CHAIN``
(``member:1.2.840.113556.1.4.1941:=``). The directory follows the chain in
one search per group, cycles included; a directory that does not know the
rule matches nothing by it, so direct members are still asked for first,
the same way, and keep what they had. A primary group (``primaryGroupID``,
usually Domain Users) is not in ``member`` and is never matched. A person in no
listed group may what ``--ldap-may`` says: ``ask`` when no group is listed,
nothing otherwise, so listing a group is how a hub says "only these
people". Someone who binds and may nothing is refused at the door like a
wrong password. Groups are read at sign-in; a session keeps what it was
given until it expires.

**What this refuses.**

- An empty password. Many directories take a simple bind with a DN and no
  password as an *unauthenticated* bind and answer success: that is
  nobody, not the person named.
- LDAP without a token. LDAP users are on top of a token, never instead of
  one: the swarm's own requests (registering, polling, forwarding) carry
  the token, and a hub with no token is open to everyone anyway.
- Guessing. Five wrong passwords for one user name within a minute and
  that name is not tried against the directory again for a minute, so a
  stranger cannot lock someone's directory account out through this door.
- A dependency. This is LDAPv3 as far as a simple bind and a search need
  it (RFC 4511), in the standard library, over ``ssl`` for ``ldaps://``
  and StartTLS. No SASL, no referrals chased, no paged results.

Passwords cross the network to the directory: use ``ldaps://`` or
``--ldap-starttls`` unless the directory is on this machine, and put the
hub behind a proxy that does TLS before people type passwords into it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import socket
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .identity import ABILITIES

DEFAULT_FILTER = "(uid={user})"
SESSION_TTL = 12 * 3600.0       # how long a /login session lasts
CACHE_TTL = 60.0                # how long a Basic bind is remembered
FAILURES = 5                    # wrong passwords for one name ...
FAILURE_WINDOW = 60.0           # ... within this long, and the name rests this long
IN_CHAIN = "1.2.840.113556.1.4.1941"  # Active Directory's LDAP_MATCHING_RULE_IN_CHAIN: membership through nested groups
MAX_SESSIONS = 10_000
MAX_MESSAGE = 1 << 20           # the most read of one LDAP message
SESSION_PREFIX = "pyu_"
STARTTLS_OID = b"1.3.6.1.4.1.1466.20037"
_ATTR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.;-]*$")

# LDAP result codes worth naming.
SUCCESS = 0
NO_SUCH_OBJECT = 32
INVALID_CREDENTIALS = 49


class LdapError(Exception):
    """The directory could not be asked, or answered something this cannot read."""


# ---------------------------------------------------------------- BER


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _length(len(content)) + content


def _int(n: int, tag: int = 0x02) -> bytes:
    return _tlv(tag, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _octets(value: str | bytes, tag: int = 0x04) -> bytes:
    return _tlv(tag, value.encode("utf-8") if isinstance(value, str) else value)


def _seq(*parts: bytes, tag: int = 0x30) -> bytes:
    return _tlv(tag, b"".join(parts))


def _read(data: bytes, i: int = 0) -> tuple[int, bytes, int]:
    """One TLV at ``i``: (tag, content, the index after it)."""
    if i + 2 > len(data):
        raise LdapError("a truncated message")
    tag, first = data[i], data[i + 1]
    i += 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or i + count > len(data):
            raise LdapError("a length this cannot read")
        n = int.from_bytes(data[i:i + count], "big")
        i += count
    else:
        n = first
    if i + n > len(data):
        raise LdapError("a truncated message")
    return tag, data[i:i + n], i + n


def _items(content: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(content):
        tag, value, i = _read(content, i)
        out.append((tag, value))
    return out


def _number(content: bytes) -> int:
    return int.from_bytes(content, "big", signed=True) if content else 0


# ---------------------------------------------------------------- escaping and filters


def escape_filter(value: str) -> str:
    """``value`` as an assertion value in a search filter (RFC 4515): it matches itself and nothing wider."""
    out = []
    for ch in value:
        out.append(f"\\{ord(ch):02x}" if ch in "\\*()\0" else ch)
    return "".join(out)


def escape_dn(value: str) -> str:
    """``value`` as an attribute value in a DN (RFC 4514): it names itself, never another RDN."""
    out = []
    for i, ch in enumerate(value):
        if ch in ',+"\\<>;=' or (ch == "#" and i == 0) or (ch == " " and (i == 0 or i == len(value) - 1)):
            out.append("\\" + ch)
        elif ch == "\0":
            out.append("\\00")
        else:
            out.append(ch)
    return "".join(out)


def _unescape(text: str) -> bytes:
    raw = text.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x5C:  # backslash: two hex digits follow
            pair = raw[i + 1:i + 3]
            try:
                if len(pair) != 2:
                    raise ValueError
                out.append(int(pair, 16))
            except ValueError:
                raise LdapError(f"a bad escape in the filter value {text!r}") from None
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def encode_filter(text: str) -> bytes:
    """A search filter in its string form (RFC 4515) as BER: ``&``, ``|``, ``!``,
    equality, presence, substrings, ``>=``, ``<=``, ``~=`` and extensible matches
    (``attr:rule:=``, ``attr:dn:=``, ``:rule:=``)."""
    text = text.strip()
    if not text.startswith("("):
        text = f"({text})"
    encoded, i = _filter(text, 0)
    if i != len(text):
        raise LdapError(f"the filter {text!r} has more after its end")
    return encoded


def _filter(s: str, i: int) -> tuple[bytes, int]:
    if i >= len(s) or s[i] != "(":
        raise LdapError(f"the filter {s!r} needs '(' at {i}")
    i += 1
    c = s[i] if i < len(s) else ""
    if c in ("&", "|"):
        i += 1
        parts = []
        while i < len(s) and s[i] == "(":
            part, i = _filter(s, i)
            parts.append(part)
        if not parts:
            raise LdapError(f"the filter {s!r} has an empty {c}")
        encoded = _seq(*parts, tag=0xA0 if c == "&" else 0xA1)
    elif c == "!":
        part, i = _filter(s, i + 1)
        encoded = _tlv(0xA2, part)
    else:
        end = s.find(")", i)
        if end < 0:
            raise LdapError(f"the filter {s!r} is not closed")
        encoded = _item(s[i:end])
        i = end
    if i >= len(s) or s[i] != ")":
        raise LdapError(f"the filter {s!r} needs ')' at {i}")
    return encoded, i + 1


def _item(text: str) -> bytes:
    attr, eq, value = text.partition("=")
    if not eq:
        raise LdapError(f"{text!r} is not attribute=value")
    op = attr[-1:] if attr[-1:] in (">", "<", "~", ":") else ""
    attr = attr[:-1] if op else attr
    if op == ":":
        return _extensible(attr, value)
    if not _ATTR.match(attr):
        raise LdapError(f"{attr!r} is not an attribute name")
    if op:
        return _seq(_octets(attr), _octets(_unescape(value)), tag={">": 0xA5, "<": 0xA6, "~": 0xA8}[op])
    if value == "*":
        return _octets(attr, tag=0x87)
    if "*" in value:
        pieces = value.split("*")
        subs = []
        if pieces[0]:
            subs.append(_octets(_unescape(pieces[0]), tag=0x80))
        subs.extend(_octets(_unescape(p), tag=0x81) for p in pieces[1:-1] if p)
        if pieces[-1]:
            subs.append(_octets(_unescape(pieces[-1]), tag=0x82))
        return _seq(_octets(attr), _seq(*subs), tag=0xA4)
    return _seq(_octets(attr), _octets(_unescape(value)), tag=0xA3)


def _extensible(left: str, value: str) -> bytes:
    """``attr[:dn][:rule]:=value`` or ``[:dn]:rule:=value`` (``left`` without the last
    colon) as a MatchingRuleAssertion: [9] {rule [1], type [2], value [3], dnAttributes [4]}."""
    attr, *rest = left.split(":")
    dn = bool(rest) and rest[0].lower() == "dn"
    if dn:
        rest = rest[1:]
    if len(rest) > 1 or (attr and not _ATTR.match(attr)) or (rest and not _ATTR.match(rest[0])):
        raise LdapError(f"{left + ':=' + value!r} is not an extensible match")
    rule = rest[0] if rest else None
    if not attr and not rule:
        raise LdapError(f"{left + ':=' + value!r} names neither an attribute nor a matching rule")
    parts = ([_octets(rule, tag=0x81)] if rule else []) + ([_octets(attr, tag=0x82)] if attr else []) + [_octets(_unescape(value), tag=0x83)]
    if dn:
        parts.append(_tlv(0x84, b"\xff"))
    return _seq(*parts, tag=0xA9)


# ---------------------------------------------------------------- the wire


@dataclass
class Result:
    code: int
    matched: str
    message: str


def _result(content: bytes) -> Result:
    items = _items(content)
    if len(items) < 3 or items[0][0] != 0x0A:
        raise LdapError("a result this cannot read")
    return Result(_number(items[0][1]), items[1][1].decode("utf-8", "replace"), items[2][1].decode("utf-8", "replace"))


class Connection:
    """One connection to a directory: simple binds and searches, one at a time.

    ``url`` is ``ldap://host[:389]`` or ``ldaps://host[:636]``; ``starttls``
    upgrades an ``ldap://`` connection before anything is sent; ``ca`` is a
    CA bundle to check the directory's certificate against (the system's
    otherwise)."""

    def __init__(self, url: str, *, starttls: bool = False, ca: str | None = None, timeout: float = 10.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("ldap", "ldaps") or not parts.hostname:
            raise LdapError(f"{url!r} is not an ldap:// or ldaps:// URL")
        if parts.scheme == "ldaps" and starttls:
            raise LdapError("ldaps:// is TLS already; StartTLS is for ldap://")
        self.host = parts.hostname
        self.port = parts.port or (636 if parts.scheme == "ldaps" else 389)
        self._id = 0
        self._buffer = b""
        try:
            self.sock: socket.socket = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError as e:
            raise LdapError(f"cannot reach {self.host}:{self.port}: {e}") from None
        try:
            if parts.scheme == "ldaps":
                self._tls(ca)
            elif starttls:
                msgid = self._send(_seq(_octets(STARTTLS_OID, tag=0x80), tag=0x77))
                tag, content = self._receive(msgid)
                result = _result(content) if tag == 0x78 else None
                if result is None or result.code != SUCCESS:
                    raise LdapError(f"{self.host} refused StartTLS" + (f": {result.message or result.code}" if result else ""))
                self._tls(ca)
        except BaseException:
            self.sock.close()
            raise

    def _tls(self, ca: str | None) -> None:
        context = ssl.create_default_context(cafile=ca)
        try:
            self.sock = context.wrap_socket(self.sock, server_hostname=self.host)
        except (ssl.SSLError, OSError) as e:
            raise LdapError(f"TLS with {self.host} failed: {e}") from None

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._send(_tlv(0x42, b""))  # unbind
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, op: bytes) -> int:
        self._id += 1
        try:
            self.sock.sendall(_seq(_int(self._id), op))
        except OSError as e:
            raise LdapError(f"{self.host} went away: {e}") from None
        return self._id

    def _fill(self, n: int) -> None:
        while len(self._buffer) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                raise LdapError(f"{self.host} did not answer: {e}") from None
            if not chunk:
                raise LdapError(f"{self.host} closed the connection")
            self._buffer += chunk

    def _message(self) -> tuple[int, int, bytes]:
        """The next message: (message id, the operation's tag, its content)."""
        self._fill(2)
        first = self._buffer[1]
        head = 2 + (first & 0x7F if first & 0x80 else 0)
        self._fill(head)
        size = int.from_bytes(self._buffer[2:head], "big") if first & 0x80 else first
        if self._buffer[0] != 0x30 or size > MAX_MESSAGE:
            raise LdapError(f"{self.host} sent something that is not an LDAP message")
        self._fill(head + size)
        _, body, end = _read(self._buffer, 0)
        self._buffer = self._buffer[end:]
        items = _items(body)
        if len(items) < 2 or items[0][0] != 0x02:
            raise LdapError(f"{self.host} sent an LDAP message this cannot read")
        return _number(items[0][1]), items[1][0], items[1][1]

    def _receive(self, msgid: int) -> tuple[int, bytes]:
        while True:
            got, tag, content = self._message()
            if got == 0:  # a notice of disconnection
                raise LdapError(f"{self.host} is closing the connection")
            if got == msgid:
                return tag, content

    def bind(self, dn: str, password: str) -> Result:
        """A simple bind; the result says whether it held (``code`` 0)."""
        msgid = self._send(_seq(_int(3), _octets(dn), _octets(password, tag=0x80), tag=0x60))
        tag, content = self._receive(msgid)
        if tag != 0x61:
            raise LdapError(f"{self.host} answered a bind with something else")
        return _result(content)

    def search(self, base: str, filter_text: str, attributes: list[str] | None = None, *, scope: int = 2, limit: int = 0) -> tuple[Result, list[tuple[str, dict[str, list[str]]]]]:
        """Entries under ``base`` (scope 0 the base itself, 1 one level, 2 the subtree)
        that match the filter: their DNs and the attributes asked for."""
        request = _seq(
            _octets(base), _int(scope, 0x0A), _int(0, 0x0A), _int(limit), _int(10), _tlv(0x01, b"\x00"),
            encode_filter(filter_text), _seq(*(_octets(a) for a in attributes or ["1.1"])),
            tag=0x63,
        )
        msgid = self._send(request)
        entries: list[tuple[str, dict[str, list[str]]]] = []
        while True:
            tag, content = self._receive(msgid)
            if tag == 0x64:
                items = _items(content)
                attrs: dict[str, list[str]] = {}
                for _, pair in _items(items[1][1]) if len(items) > 1 else []:
                    kind, values = _items(pair)[:2]
                    attrs[kind[1].decode("utf-8", "replace").lower()] = [v.decode("utf-8", "replace") for _, v in _items(values[1])]
                entries.append((items[0][1].decode("utf-8", "replace"), attrs))
            elif tag == 0x65:
                return _result(content), entries
            elif tag == 0x73:
                continue  # a referral elsewhere: not followed
            else:
                raise LdapError(f"{self.host} answered a search with something else")


# ---------------------------------------------------------------- users


@dataclass(frozen=True)
class User:
    """A person the directory vouched for, and what they may do here."""
    name: str
    dn: str
    may: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {"user": self.name, "dn": self.dn, "may": [a for a in ABILITIES if a in self.may]}


def parse_group(spec: str) -> tuple[str, list[str]]:
    """``cn=ops,ou=groups,dc=example,dc=org=ask,operate`` as (group DN, abilities)."""
    from .grants import parse_may
    from .identity import IdentityError
    dn, eq, may = spec.rpartition("=")
    if not eq or not dn or "=" not in dn:
        raise LdapError(f"{spec!r}: write GROUP-DN=ABILITIES, e.g. cn=ops,ou=groups,dc=example,dc=org=ask,operate")
    try:
        return dn.strip(), parse_may(may)
    except IdentityError as e:
        raise LdapError(f"{spec!r}: {e}") from None


def parse_abilities(text: str | None) -> list[str] | None:
    """``--ldap-may``: abilities, ``none`` for none, or None (the default) when not given."""
    from .grants import parse_may
    from .identity import IdentityError
    if text is None:
        return None
    if text.strip().lower() in ("", "none"):
        return []
    try:
        return parse_may(text)
    except IdentityError as e:
        raise LdapError(str(e)) from None


class LdapUsers:
    """Who may sign in, by a directory, and the sessions they hold."""

    def __init__(
        self,
        url: str,
        *,
        user_dn: str | None = None,
        base: str | None = None,
        filter: str = DEFAULT_FILTER,
        bind_dn: str | None = None,
        bind_password: str | None = None,
        groups: dict[str, list[str]] | None = None,
        may: list[str] | None = None,
        nested: bool = False,
        starttls: bool = False,
        ca: str | None = None,
        timeout: float = 10.0,
        session_ttl: float = SESSION_TTL,
        cache_ttl: float = CACHE_TTL,
        connect: Callable[[], Connection] | None = None,
    ) -> None:
        if not user_dn and not base:
            raise LdapError("say how to find a person: --ldap-user-dn 'uid={user},ou=people,…', or --ldap-base DN to search")
        if user_dn and "{user}" not in user_dn:
            raise LdapError("--ldap-user-dn needs {user} where the user name goes")
        if base and "{user}" not in filter:
            raise LdapError("--ldap-filter needs {user} where the user name goes")
        if bind_dn and not bind_password:
            raise LdapError("--ldap-bind-dn needs its password: set $PYRTAN_LDAP_BIND_PASSWORD")
        if urlsplit(url).scheme not in ("ldap", "ldaps") or not urlsplit(url).hostname:
            raise LdapError(f"{url!r} is not an ldap:// or ldaps:// URL")
        if url.startswith("ldaps://") and starttls:
            raise LdapError("ldaps:// is TLS already; --ldap-starttls is for ldap://")
        encode_filter(filter.replace("{user}", "x"))  # a bad filter is refused at start, not at the first sign-in
        self.url = url
        self.user_dn = user_dn
        self.base = base
        self.filter = filter
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.groups = dict(groups or {})
        self.nested = nested  # follow nested groups by Active Directory's in-chain rule
        self.may = frozenset(may if may is not None else ([] if self.groups else ["ask"]))
        self.session_ttl = session_ttl
        self.cache_ttl = cache_ttl
        self._connect = connect or (lambda: Connection(url, starttls=starttls, ca=ca, timeout=timeout))
        self.secure = url.startswith("ldaps://") or starttls or (urlsplit(url).hostname or "") in ("localhost", "127.0.0.1", "::1")
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._cache: dict[bytes, tuple[User, float]] = {}
        self._sessions: dict[bytes, tuple[User, float]] = {}
        self._failures: dict[str, deque[float]] = {}

    def __repr__(self) -> str:  # never the bind password
        return f"LdapUsers({self.url!r}, groups={len(self.groups)})"

    # -------------------------------------------------------- signing in

    def authenticate(self, user: str, password: str) -> User | None:
        """The person, if the directory takes this name and password; None if it does
        not (or the name is resting after too many wrong passwords). LdapError when
        the directory cannot be asked: that is not a wrong password."""
        if not isinstance(user, str) or not isinstance(password, str):
            return None
        user = user.strip()
        if not user or not password or len(user) > 256 or len(password) > 1024 or any(ord(c) < 0x20 for c in user):
            return None  # an empty password is an unauthenticated bind: nobody
        key = hmac.new(self._key, user.encode("utf-8") + b"\0" + password.encode("utf-8"), hashlib.sha256).digest()
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
            if self._resting(user.lower(), now):
                return None
        found = self._ask_directory(user, password)
        with self._lock:
            if found is None:
                self._failures.setdefault(user.lower(), deque()).append(now)
            else:
                self._failures.pop(user.lower(), None)
                if len(self._cache) > MAX_SESSIONS:
                    self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
                self._cache[key] = (found, now + self.cache_ttl)
        return found

    def _resting(self, name: str, now: float) -> bool:
        seen = self._failures.get(name)
        if not seen:
            return False
        while seen and seen[0] < now - FAILURE_WINDOW:
            seen.popleft()
        if not seen:
            self._failures.pop(name, None)
            return False
        return len(seen) >= FAILURES

    def _ask_directory(self, user: str, password: str) -> User | None:
        with self._connect() as conn:
            if self.user_dn:
                dn = self.user_dn.replace("{user}", escape_dn(user))
            else:
                if self.bind_dn:
                    self._service_bind(conn)
                assert self.base is not None
                result, entries = conn.search(self.base, self.filter.replace("{user}", escape_filter(user)), scope=2, limit=2)
                if result.code not in (SUCCESS, 4) or len(entries) != 1:  # 4: size limit, more than one
                    return None
                dn = entries[0][0]
            bound = conn.bind(dn, password)
            if bound.code == INVALID_CREDENTIALS:
                return None
            if bound.code != SUCCESS:
                raise LdapError(f"the directory refused the bind for {user}: {bound.message or bound.code}")
            if self.bind_dn and self.groups:
                self._service_bind(conn)  # groups are read as the service account, which may read them
            may = set(self.may)
            for group, abilities in self.groups.items():
                if not set(abilities) <= may and self._member(conn, group, dn, user):
                    may.update(abilities)
        return User(user, dn, frozenset(may))

    def _service_bind(self, conn: Connection) -> None:
        assert self.bind_dn is not None and self.bind_password is not None
        result = conn.bind(self.bind_dn, self.bind_password)
        if result.code != SUCCESS:
            raise LdapError(f"the directory refused --ldap-bind-dn: {result.message or result.code}")

    def _member(self, conn: Connection, group: str, dn: str, user: str) -> bool:
        """Whether the group entry names this person, directly, or (``nested``) through
        groups that are its members, as Active Directory follows them."""
        who = f"(|(member={escape_filter(dn)})(uniqueMember={escape_filter(dn)})(memberUid={escape_filter(user)}))"
        result, entries = conn.search(group, who, scope=0, limit=1)
        if result.code == SUCCESS and entries:
            return True
        if not self.nested:
            return False
        result, entries = conn.search(group, f"(member:{IN_CHAIN}:={escape_filter(dn)})", scope=0, limit=1)
        return result.code == SUCCESS and bool(entries)

    # -------------------------------------------------------- the Authorization header

    def presented(self, header: str | None) -> bool:
        """Whether a header is this door's: Basic, or a session token."""
        if not header:
            return False
        scheme, _, value = header.strip().partition(" ")
        return scheme.lower() == "basic" or (scheme.lower() == "bearer" and value.strip().startswith(SESSION_PREFIX))

    def check(self, header: str | None) -> User | None:
        """The person an ``Authorization`` header is, or None."""
        if not header:
            return None
        scheme, _, value = header.strip().partition(" ")
        value = value.strip()
        if scheme.lower() == "bearer":
            return self.session(value)
        if scheme.lower() != "basic":
            return None
        try:
            user, colon, password = base64.b64decode(value, validate=True).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        if not colon:
            return None
        found = self.authenticate(user, password)
        return found if found is not None and found.may else None

    # -------------------------------------------------------- sessions

    def _digest(self, token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def session(self, token: str) -> User | None:
        if not token.startswith(SESSION_PREFIX):
            return None
        now = time.time()
        with self._lock:
            held = self._sessions.get(self._digest(token))
            if held is None:
                return None
            if held[1] <= now:
                self._sessions.pop(self._digest(token), None)
                return None
            return held[0]

    def login(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """``POST /login``: a session token for a name and password the directory takes."""
        user, password = body.get("user"), body.get("password")
        if not isinstance(user, str) or not isinstance(password, str) or not user.strip() or not password:
            return 400, {"status": "error", "reason": "bad-request", "say": "Send {\"user\": \"...\", \"password\": \"...\"}."}
        try:
            found = self.authenticate(user, password)
        except LdapError as e:
            return 503, {"status": "error", "reason": "directory-down", "say": f"The directory could not be asked: {e}."}
        if found is None:
            time.sleep(0.2)  # a wrong password costs the guesser a moment
            return 401, {"status": "error", "reason": "unauthorised", "say": "That user name and password are not taken here."}
        if not found.may:
            return 403, {"status": "error", "reason": "not-granted", "say": f"{found.name} signed in, but is in none of the groups this Pyrtan lets in."}
        token = SESSION_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                self._sessions = {k: v for k, v in self._sessions.items() if v[1] > now}
                while len(self._sessions) >= MAX_SESSIONS:
                    self._sessions.pop(min(self._sessions, key=lambda k: self._sessions[k][1]))
            self._sessions[self._digest(token)] = (found, now + self.session_ttl)
        return 200, {"status": "ok", "token": token, "expires": int(now + self.session_ttl), **found.to_dict()}

    def logout(self, header: str | None) -> tuple[int, dict[str, Any]]:
        """``POST /logout``: the session token in the header is no longer taken."""
        scheme, _, value = (header or "").strip().partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not value.startswith(SESSION_PREFIX):
            return 400, {"status": "error", "reason": "bad-request", "say": "Send the session token as Authorization: Bearer pyu_…."}
        with self._lock:
            gone = self._sessions.pop(self._digest(value), None)
        return 200, {"status": "ok", "ended": gone is not None}

    def __len__(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for _, until in self._sessions.values() if until > now)


# What a caller's directory account may do, as the server tells the route: set by the
# server after it checked the account, and never taken from the request.
USER_HEADER = "X-Pyrtan-User"
USER_MAY_HEADER = "X-Pyrtan-User-May"


def user_may(headers: dict[str, str] | None) -> set[str]:
    """The abilities the server found for this request's directory account, if any."""
    for k, v in (headers or {}).items():
        if k.lower() == USER_MAY_HEADER.lower():
            return {a for a in str(v).split(",") if a in ABILITIES}
    return set()
