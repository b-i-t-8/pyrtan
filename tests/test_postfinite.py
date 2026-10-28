"""The Postfinite trees, against recorded answers from its tenant API.

They sign in as an account on the pyrtan tenant (credentials from the
service's env), read one endpoint, and decide on what came back. What
matters as much as the answers: the credentials and the session never
appear in what a run returns.
"""

import json

import pytest

from pyrtan import Registry, RecordedTransport, Runner
from pyrtan.tree import check

from conftest import FIXTURES, ROOT

ENV = {"postfinite_email": "ops@example.org", "postfinite_password": "not-a-real-password"}
SECRETS = ("ops@example.org", "not-a-real-password", "pf-test-session-token")
LOGIN = "https://postfinite.com/api/auth/login"


@pytest.fixture
def postfinite() -> Registry:
    reg = Registry.from_paths([ROOT / "catalog" / "postfinite"])
    assert not reg.errors, reg.errors
    return reg


def run(reg: Registry, text: str, env=ENV, transport=None):
    return Runner(reg, transport or RecordedTransport.from_file(FIXTURES / "postfinite.json"), env=dict(env)).run_intent(text)


def test_they_check_clean_and_reach_only_postfinite(postfinite):
    assert len(postfinite) == 7
    for tree in postfinite.trees():
        assert not [p for p in check(tree) if p.level == "error"], tree.name
        assert tree.namespace == "postfinite" and tree.license == "MIT"
        assert tree.hosts == ["postfinite.com"]
        assert tree.raw["nodes"]["sign-in"]["kind"] == "act"  # a sign-in is never retried


@pytest.mark.parametrize("text, tree, say", [
    ("postfinite", "postfinite-overview", "Postfinite holds 1184 published entities and 18 reference forecast tables"),
    ("what data does postfinite have", "postfinite-datasets", "Postfinite publishes 3 datasets in 4 themes. The first: Grid carbon intensity"),
    ("search postfinite for rotterdam", "postfinite-search", "last air_temperature = 9.4 °C at 2026-10-27T23:00:00.000Z"),
    ("find port of on postfinite", "postfinite-search", "Port of Rotterdam (pf:port:rotterdam), with no readings yet."),
    ("latest readings for pf:grid:gb on postfinite", "postfinite-readings", "pf:grid:gb on Postfinite: 2 metrics, 672 readings."),
    ("latest carbon_intensity for pf:grid:gb on postfinite", "postfinite-reading", "carbon_intensity for pf:grid:gb: 142 gCO2/kWh at"),
    ("kp at pf:space:kp on postfinite", "postfinite-reading", "kp for pf:space:kp: 3.33 at 2026-10-27T21:00:00.000Z."),
    ("carbon_intensity for pf:grid:gb over the last 2 days on postfinite", "postfinite-history", "96 readings in 2 buckets; the latest averaged 142 gCO2/kWh (low 142, high 142)."),
    ("postfinite forecasts", "postfinite-forecasts", "best so far: seasonal_linear on carbon_intensity, 6.4% off."),
])
def test_answers(postfinite, text, tree, say):
    out = run(postfinite, text)
    assert (out.status, out.tree) == ("done", tree), out.say
    assert say in out.say


@pytest.mark.parametrize("text, reason", [
    ("search postfinite for atlantis", "nothing-found"),
    ("readings for pf:nowhere on postfinite", "no-readings"),
    ("wind for pf:grid:gb on postfinite", "no-readings"),
    ("history of solar for pf:grid:gb on postfinite", "no-readings"),
    ("history of carbon_intensity for pf:grid:gb on postfinite", "bad-range"),
])
def test_what_is_not_there_is_said(postfinite, text, reason):
    out = run(postfinite, text)
    assert (out.status, out.reason) == ("failed", reason), out.say


def test_without_credentials_it_says_how_to_give_them(postfinite):
    out = run(postfinite, "postfinite", env={})
    assert (out.status, out.reason) == ("failed", "no-credentials")
    assert "--env postfinite_email=" in out.say
    out = run(postfinite, "postfinite", env={"postfinite_email": "ops@example.org"})
    assert out.reason == "no-credentials"
    assert [s.node for s in out.trace.steps] == ["intent", "credentials", "password", "no-credentials"]


@pytest.mark.parametrize("status, body, reason", [
    (401, {"success": False, "error": "Invalid credentials."}, "sign-in-refused"),
    (403, {"success": False, "error": "This account signs in with an identity provider."}, "not-a-password-account"),
    (429, {"success": False, "error": "Too many failed attempts. Try again in 600s."}, "locked-out"),
    (503, {"success": False, "error": "Authentication requires a live database (server is in demo mode)."}, "upstream-down"),
    (418, {}, "unexpected-response"),
])
def test_a_refused_sign_in_stops_before_any_read(postfinite, status, body, reason):
    t = RecordedTransport.from_file(FIXTURES / "postfinite.json")
    t.add("POST", LOGIN, status, body)
    out = run(postfinite, "postfinite datasets", transport=t)
    assert (out.status, out.reason) == ("failed", reason)
    assert "fetch" not in [s.node for s in out.trace.steps]


@pytest.mark.parametrize("status, reason", [(401, "session-refused"), (403, "no-access"), (429, "rate-limited"), (502, "upstream-down"), (409, "unexpected-response")])
def test_a_refused_read_is_named(postfinite, status, reason):
    t = RecordedTransport.from_file(FIXTURES / "postfinite.json")
    t.add("GET", "https://postfinite.com/api/t/forecasts", status, {"success": False, "error": "You are no longer a member of this team."})
    out = run(postfinite, "postfinite forecasts", transport=t)
    assert (out.status, out.reason) == ("failed", reason)


def test_credentials_and_the_session_never_leave_in_a_run(postfinite):
    sentences = ["postfinite", "postfinite datasets", "search postfinite for rotterdam", "latest readings for pf:grid:gb on postfinite",
                 "kp at pf:space:kp on postfinite", "carbon_intensity for pf:grid:gb over the last 2 days on postfinite", "postfinite forecasts",
                 "search postfinite for atlantis"]
    for text in sentences:
        out = run(postfinite, text)
        shown = json.dumps(out.to_dict(), ensure_ascii=False, default=str) + out.trace.render()
        for secret in SECRETS:
            assert secret not in shown, (text, secret)
        decided = [s for s in out.trace.steps if s.node in ("credentials", "password")]
        assert [s.observed for s in decided] == ["<set>", "<set>"]


def test_the_credentials_are_what_is_sent(postfinite):
    sent = []

    class Spy(RecordedTransport):
        def request(self, method, url, headers=None, body=None, timeout=10.0):
            sent.append((method, url, headers, body))
            return super().request(method, url, headers, body, timeout)

    t = Spy.from_file(FIXTURES / "postfinite.json")
    assert run(postfinite, "postfinite", transport=t).status == "done"
    (m1, u1, h1, b1), (m2, u2, h2, _) = sent
    assert (m1, u1, h1["Content-Type"]) == ("POST", LOGIN, "application/json")
    assert json.loads(b1) == {"email": "ops@example.org", "password": "not-a-real-password"}
    assert (m2, u2, h2["Authorization"]) == ("GET", "https://postfinite.com/api/t/home", "Bearer pf-test-session-token")


def test_an_env_value_decided_on_is_never_in_a_gap():
    from pyrtan.tree import load_tree_text

    tree = load_tree_text('''
name = "env-gap"
start = "d"
[[triggers]]
pattern = "env gap"
[nodes.d]
kind = "decide"
on = "env.secret"
branches = [{ when = { eq = "something else" }, then = "ok" }]
[nodes.ok]
kind = "done"
say = "ok"
''')
    reg = Registry()
    reg.add(tree)
    out = Runner(reg, RecordedTransport({}), env={"secret": "hunter2"}).run_intent("env gap")
    assert out.reason == "gap"
    assert out.output["gap"]["observed"] == "<set>"
    assert "hunter2" not in json.dumps(out.to_dict(), default=str) + out.trace.render()
