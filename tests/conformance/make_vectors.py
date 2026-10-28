"""Write tests/conformance/vectors.json: what both runtimes must say.

Every example tree as JSON (the TOML's own table), and for a fixed set of
runs (a sentence, or a tree and inputs) the outcome the Python runtime
produced against the recorded responses in tests/fixtures/examples.json
(or the run's own fixture, with the run's env),
with a fixed clock. The JavaScript runtime (src/pyrtan/js/pyrtan.js) must
produce the same status, say, output, reason, path and gaps.

Since the browser reads sentences too (it is a hub of its own until it
joins one), the vectors also hold how Python reads: every sentence's
reading (the rewrites, each trigger's verdict, the intent step), the
language rules and value parsers one by one, the tree proposed for a
sentence nobody answers, and a sequence of requests to a service and
what it answered, which pyrtan.js's LocalHub must answer the same.
tests/conformance/run.cjs checks it all, and tests/test_conformance.py
keeps this file from drifting.

    uv run python tests/conformance/make_vectors.py
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pyrtan import Registry, RecordedTransport, Runner  # noqa: E402
from pyrtan import nlp  # noqa: E402
from pyrtan.grow import draft_branch, propose  # noqa: E402
from pyrtan.service import Service  # noqa: E402
from pyrtan.tree import build as build_tree  # noqa: E402

CLOCK = _dt.datetime(2026, 10, 25, 12, 0, tzinfo=_dt.timezone.utc)
POSTFINITE = "tests/fixtures/postfinite.json"
POSTFINITE_ENV = {"postfinite_email": "ops@example.org", "postfinite_password": "not-a-real-password"}
CASES: list[dict] = [
    {"intent": "What's the top story on Hacker News?"},
    {"intent": "top 3 stories on hacker news"},
    {"intent": "top 99 stories on hacker news"},
    {"intent": "hacker news item 999"},
    {"intent": "weather in Oslo"},
    {"tree": "weather", "inputs": {"place": "Oslo", "units": "fahrenheit"}},
    {"tree": "weather", "inputs": {"place": "Oslo", "units": "kelvin"}},
    {"intent": "how cold is it in Nowhereville"},
    {"intent": "is Example.com up"},
    {"intent": "is broken.example up"},
    {"intent": "check status of teapot.example"},
    {"tree": "site-status", "inputs": {"host": "http://x"}},
    {"intent": "latest release of b-i-t-8/pyrtan"},
    {"intent": "latest b-i-t-8/nothing release"},
    {"intent": "latest release of b-i-t-8/busy"},
    {"intent": "weather in Paris"},
    {"tree": "hacker-news-item", "inputs": {"id": 101}},
    {"tree": "hacker-news", "inputs": {"count": 2}},
    # The other sources, answered by the same fixture.
    {"intent": "how full is the disk"},
    {"intent": "disk space on /data"},
    {"intent": "disk space on /nowhere"},
    {"intent": "errors in the log"},
    {"intent": "errors in /var/log/quiet.log"},
    {"intent": "errors in /var/log/nothing.log"},
    {"intent": "errors in /etc/passwd"},
    {"intent": "temperature in the kitchen"},
    {"intent": "how warm is the attic"},
    {"intent": "temperature in the garage"},
    {"intent": "temperature in the hall"},
    {"tree": "room-temperature", "inputs": {"room": "a-b"}},
    {"intent": "headline of https://app.example/dashboard"},
    {"intent": "what does https://app.example/empty say"},
    {"intent": "headline of https://app.example/gone"},
    {"intent": "is the modem up"},
    {"intent": "is the modem on /dev/ttyUSB1 up"},
    {"intent": "is the modem on /dev/ttyACM0 up"},
    {"intent": "is the modem on /dev/ttyS0 up"},
    {"intent": "how are you today?"},
    {"intent": "are you a bot"},
    {"intent": "goodbye"},
    # Trees that run in a browser too.
    {"intent": "tell me about Ursula K. Le Guin"},
    {"intent": "wiki Mercury"},
    {"intent": "who is Xyzzyplugh Notaperson"},
    {"intent": "who was Alan Turing"},
    {"intent": "what does serendipity mean"},
    {"intent": "define zzzqx"},
    {"intent": "when is sunset in Oslo"},
    {"intent": "sunrise in Longyearbyen"},
    {"intent": "air quality in Oslo"},
    {"intent": "how is the air in Delhi"},
    {"intent": "latest version of @types/node on npm"},
    {"intent": "npm package nope-not-a-pkg-zz"},
    {"intent": "latest requests on pypi"},
    {"intent": "pip version nope-not-a-pkg-zz"},
    {"intent": "any earthquakes today"},
    {"intent": "where is the iss"},
    {"intent": "search hacker news for sqlite"},
    {"intent": "search hacker news for qqqzzzxxyyy"},
    {"intent": "who wrote The Left Hand of Darkness"},
    {"intent": "find the book qqqzzzxxyyy"},
    {"intent": "next holiday in no"},
    {"intent": "next holiday in XX"},
    {"intent": "tell me a joke"},
    {"intent": "convert 100 usd to eur"},
    {"intent": "convert 1,000 EUR into JPY"},
    {"intent": "usd to xyz rate"},
    {"intent": "exchange rate from usd to usd"},
    {"intent": "synonyms for happy"},
    {"intent": "another word for qqqzzz"},
    {"intent": "weather in Oslo tomorrow"},
    {"intent": "uv index in Sydney"},
    {"intent": "what time is it in La Paz"},
    {"intent": "how high is La Paz"},
    {"intent": "how high is Oslo"},
    {"intent": "where is zip 90210"},
    {"intent": "postcode SW1A in GB"},
    {"intent": "where is zip 00000"},
    {"intent": "is example.com archived"},
    {"intent": "is nothing-here-qqzz.example archived"},
    {"intent": "what rhymes with cat"},
    {"intent": "what rhymes with qqzzx"},
    {"intent": "how many stars does astral-sh/uv have"},
    {"intent": "github repo b-i-t-8/nothing-here"},
    {"intent": "latest version of github.com/gorilla/mux on go"},
    {"intent": "go module github.com/nope/nope-qq"},
    {"intent": "brew formula wget"},
    {"intent": "brew formula nope-qqzz"},
    {"intent": "pokemon pikachu"},
    {"intent": "what type is charizard"},
    {"intent": "pokemon notapokemonqq"},
    {"intent": "what is my ip"},
    {"intent": "give me some advice"},
    {"intent": "tell me a cat fact"},
    {"intent": "when is the next launch"},
    {"intent": "space news"},
    {"intent": "astronomy picture of the day"},
    # Postfinite, signed in with made-up credentials no trace may show, from its own fixture.
    *({"intent": text, "env": POSTFINITE_ENV, "fixture": POSTFINITE} for text in [
        "postfinite", "what data does postfinite have", "search postfinite for rotterdam", "find port of on postfinite",
        "search postfinite for atlantis", "latest readings for pf:grid:gb on postfinite", "readings for pf:nowhere on postfinite",
        "latest carbon_intensity for pf:grid:gb on postfinite", "kp at pf:space:kp on postfinite", "wind for pf:grid:gb on postfinite",
        "carbon_intensity for pf:grid:gb over the last 2 days on postfinite", "history of carbon_intensity for pf:grid:gb on postfinite",
        "history of solar for pf:grid:gb on postfinite", "postfinite forecasts",
    ]),
    {"intent": "how is postfinite doing", "fixture": POSTFINITE},
    {"intent": "postfinite datasets", "env": {"postfinite_email": "ops@example.org"}, "fixture": POSTFINITE},
]


# Trees that exercise what the examples do not: every slot kind, synonyms
# (a word and a phrase), typos, fixed inputs, a tie, and an ask.
def _echo(name: str, triggers: list, inputs: dict, **extra) -> dict:
    return {"name": name, "version": 1, "description": f"echo for {name}", "hosts": [], "start": "reply", "triggers": triggers,
            "inputs": inputs, "nodes": {"reply": {"kind": "done", "say": "ok", "output": {k: f"{{{{ inputs.{k} }}}}" for k in inputs}}}, **extra}


LANGUAGE_TREES: list[dict] = [
    _echo("remind", ["remind me {when:date} at {at:time}", "remind me in {wait:duration}"], {"when": {"type": "str", "default": ""}, "at": {"type": "str", "default": ""}, "wait": {"type": "int", "default": 0}}),
    _echo("count", ["count to {n:number}", "add {x:float} and {y:int}"], {"n": {"type": "float", "default": 0}, "x": {"type": "float", "default": 0.0}, "y": {"type": "int", "default": 0}}),
    _echo("ping", ["ping {host:host}", "fetch {url:url}", "mail {to:email}"], {"host": {"type": "str", "default": ""}, "url": {"type": "str", "default": ""}, "to": {"type": "str", "default": ""}}),
    _echo("library", ["(find|search) books by {author}", "top story on book club"], {"author": {"type": "str", "default": ""}, "count": {"type": "int", "default": 3}},
          language={"synonyms": {"books": ["novels", "paperback books"], "Find": "look up"}, "typos": True}),
    _echo("tie-a", ["shout {x:word}"], {"x": {"type": "str", "default": ""}}),
    _echo("tie-b", ["shout {y:word}"], {"y": {"type": "str", "default": ""}}),
    _echo("fixed", [{"pattern": "the usual", "inputs": {"what": "coffee"}}], {"what": {"type": "str", "default": "tea"}}),
    {"name": "order", "version": 1, "description": "asks, then answers", "hosts": [], "start": "size",
     "triggers": ["order a {drink:word}"], "inputs": {"drink": {"type": "str", "required": True}, "shots": {"type": "int", "default": 1}},
     "nodes": {"size": {"kind": "ask", "prompt": "What size of {{ inputs.drink }}?", "into": "state.size", "next": "shots"},
               "shots": {"kind": "ask", "prompt": "How many shots?", "into": "inputs.shots", "next": "reply"},
               "reply": {"kind": "done", "say": "A {{ state.size }} {{ inputs.drink }}, {{ inputs.shots }} shot(s).", "output": {"size": "{{ state.size }}", "shots": "{{ inputs.shots }}"}}}},
]

READINGS = [
    # the examples, read
    "What's the top story on Hacker News?", "top five stories on hacker news", "hey pyrtan, top 3 stories on hacker news please",
    "weather in Oslo", "what's the forecast for New York", "how cold is it in Oslo", "is Example.com up", "is http://Example.com:8080/x up",
    "is not a host up", "latest release of b-i-t-8/pyrtan", "how full is the disk", "disk space on /data", "the temperature in the attic",
    "headline of https://app.example/dashboard", "is the modem on /dev/ttyUSB1 up", "make me a sandwich", "", "   ?!  ",
    "Could you please tell me the weather in Zürich, thanks!", "TOP 3 STORIES ON HACKER NEWS", "top twenty-one stories on hacker news",
    "weather in Oslo for me", "hello", "pyrtan",
    # the language trees, read
    "remind me tomorrow at 3pm", "remind me next friday at noon", "remind me 25 dec at 9:30 pm", "remind me 3/4 at 10am",
    "remind me 31/12 at 25:00", "remind me in 2 hours", "remind me in an hour and a half", "remind me in 1h30m", "remind me in half an hour",
    "remind me in 2 parsecs", "count to two hundred and five", "count to 1,000.5", "count to 2.0", "count to a dozen", "add 1.5 and 3",
    "ping localhost", "ping 10.0.0.1:22", "ping not_a_host", "fetch https://example.com/a?b=c", "fetch ftp://x", "mail Barry@Example.COM",
    "mail nobody", "find books by Ursula Le Guin", "search novels by Le Guin", "look up paperback books by Le Guin", "finnd books by Le Guin",
    "serch books by Le Guin", "top stories on book club", "shout hello", "the usual", "order a latte", "it's the usual",
]

NLP = [
    ("clean", "Hey, can you please tell me what's up?"), ("clean", "“Curly” quotes — and dashes"), ("clean", "ＦＵＬＬ width"),
    ("clean", "I'm sure it's a dozen"), ("clean", "one hundred and twenty three things"), ("clean", "a hundred and one"),
    ("clean", "Thanks"), ("clean", "well, um, okay pyrtan: weather."), ("clean", "St. Ives. Then Mr. Smith"),
    ("clean", "convert 1,000.5 EUR, then 2, 3"), ("clean", "a,b and 1 ,2 and 3, 4"),
    ("singular", "stories"), ("singular", "boxes"), ("singular", "news"), ("singular", "glass"), ("singular", "cats"), ("singular", "bus"),
    ("plural", "story"), ("plural", "box"), ("plural", "day"), ("plural", "status"),
    ("parse_number", "twenty-one"), ("parse_number", "1,234"), ("parse_number", "-3.25"), ("parse_number", "lots"),
    ("parse_duration", "90 seconds"), ("parse_duration", "2 days and 3 hours"), ("parse_duration", "a week"), ("parse_duration", "1.5 hours and a half"),
    ("parse_duration", "soon"), ("parse_duration", "3 fortnights"),
    ("parse_time", "15:30"), ("parse_time", "3:05 pm"), ("parse_time", "9 o'clock"), ("parse_time", "12am"), ("parse_time", "13pm"),
    ("parse_time", "7"), ("parse_time", "midnight"), ("parse_time", "nine thirty"),
    ("parse_date", "2026-02-29"), ("parse_date", "2028-02-29"), ("parse_date", "25 oct"), ("parse_date", "oct 25, 2027"),
    ("parse_date", "the 1st of march"), ("parse_date", "sept 3"), ("parse_date", "25/10/2026"), ("parse_date", "10/10"),
    ("parse_date", "3/4"), ("parse_date", "today"), ("parse_date", "yesterday"), ("parse_date", "day after tomorrow"),
    ("parse_date", "sunday"), ("parse_date", "this sunday"), ("parse_date", "next sunday"), ("parse_date", "last sunday"),
    ("parse_date", "last mon"), ("parse_date", "in 3 weeks"), ("parse_date", "in 4 months"), ("parse_date", "2 years ago"),
    ("parse_date", "next month"), ("parse_date", "end of month"), ("parse_date", "end of the year"), ("parse_date", "start of month"),
    ("parse_date", "someday"), ("parse_date", "30 feb"),
    ("parse_host", "Example.COM"), ("parse_host", "https://a.b.example:8443/x"), ("parse_host", "not a host"),
    ("parse_url", "http://x.example/y"), ("parse_url", "x.example"), ("parse_email", "A.B@Example.org"), ("parse_email", "a@b"),
]

PROPOSALS = [
    "make me a sandwich", "is example.com up", "top 3 stories on lobsters", "what is 2.5 times 4", "latest release of foo/bar",
    "", "the the the", "{weird} [pattern] (here)", "what's the weather on mars?", "ping 10.0.0.1 and 10.0.0.2", "café opening hours",
    'say "hello" to $USER', "it's raining",
]

# Gaps on the examples, and the branch grow.py's rules draft for each.
DRAFTS = [
    ("weather", {"node": "sky", "on": "code", "observed": 4}),       # the hole between 3 and 45
    ("weather", {"node": "sky", "on": "code", "observed": 100}),     # past the last range
    ("weather", {"node": "sky", "on": "code", "observed": 61}),      # covered already
    ("weather", {"node": "sky", "on": "code", "observed": None}),    # missing
    ("weather", {"node": "sky", "on": "code", "observed": "<dict>"}),  # a report's type
    ("weather", {"node": "sky", "on": "code", "observed": "Sunny Spells"}),
    ("weather", {"node": "geocode", "on": "x", "observed": 1}),     # not a decide
    ("site-status", {"node": "status", "observed": 700}),
    ("site-status", {"node": "status", "observed": True}),
]

HUB = [
    ("GET", "/capabilities", None),
    ("POST", "/intent", {"text": "top 3 stories on hacker news"}),
    ("POST", "/intent", {"text": "weather in Oslo", "trace": False}),
    ("POST", "/intent", {"text": "how cold is it in Nowhereville"}),
    ("POST", "/intent", {"text": "is broken.example up"}),
    ("POST", "/intent", {"text": "is broken.example up"}),
    ("POST", "/intent", {"text": "make me a sandwich"}),
    ("POST", "/intent", {"text": "Make me a sandwich!"}),
    ("POST", "/intent", {"text": "shout hello"}),
    ("POST", "/intent", {"text": "  "}),
    ("POST", "/intent", {}),
    ("POST", "/run", {"tree": "hacker-news", "inputs": {"count": 2}}),
    ("POST", "/run", {"tree": "nope"}),
    ("POST", "/run", {"tree": "weather", "inputs": {"units": "kelvin", "place": "Oslo"}}),
    ("POST", "/intent", {"text": "order a latte"}),
    ("POST", "/resume", {"snapshot": "@last", "answer": "large"}),
    ("POST", "/resume", {"snapshot": "@last", "answer": "2"}),
    ("POST", "/resume", {"snapshot": {"nope": 1}}),
    ("POST", "/intent", {"text": "order a mocha"}),
    ("POST", "/resume", {"snapshot": "@last", "answer": "small"}),
    ("POST", "/resume", {"snapshot": "@last", "answer": "many"}),
    ("POST", "/intent", {"text": "temperature in the hall"}),
    ("POST", "/intent", {"text": "what's the temperature in the hall"}),
    ("GET", "/gaps", None),
    ("GET", "/unanswered", None),
    ("POST", "/suggestions", {"suggestions": [
        {"kind": "branch", "tree": "weather", "node": "sky", "on": "code", "observed": 100, "meaning": {"kind": "fail", "say": "{{ state.name }}: code 100 is not a WMO code.", "reason": "not-wmo"}, "why": "WMO codes stop at 99.", "by": {"agent": "scout", "model": "test"}, "extra": "dropped"},
        {"kind": "trigger", "tree": "weather", "pattern": "is it raining in {place}", "covers": ["is it raining in Oslo"]},
        {"kind": "tree", "text": "top 3 stories on lobsters", "description": "Top stories from lobste.rs.", "hosts_hint": ["lobste.rs"]},
        {"kind": "branch", "tree": "weather", "node": "sky", "observed": 1, "meaning": {"kind": "done", "say": "{{ env.SECRET }}"}},
        {"kind": "trigger", "tree": "weather", "pattern": "{place}", "covers": ["Oslo"]},
        {"kind": "act", "tree": "weather"},
        "not a table",
    ]}),
    ("POST", "/suggestions", {"kind": "trigger", "tree": "weather", "pattern": "is it raining in {place}", "covers": ["is it raining in Oslo", "is it raining in Bergen"]}),
    ("POST", "/suggestions", {"nothing": True}),
    ("GET", "/suggestions", None),
    ("GET", "/health", None),
    ("GET", "/watches", None),
    ("GET", "/nowhere", None),
]


def _nlp_case(fn: str, text: str) -> dict:
    try:
        if fn == "parse_date":
            value = nlp.parse_date(text, CLOCK)
        else:
            value = getattr(nlp, fn)(text)
    except nlp.ParseError as e:
        return {"fn": fn, "text": text, "error": str(e)}
    if fn == "clean":
        value = list(value)
    return {"fn": fn, "text": text, "value": value}


def _hub_exchanges(reg: Registry, fixture: Path) -> list[dict]:
    """What a service with these trees answers, request by request. A
    snapshot of "@last" is the one the previous answer carried."""
    service = Service(reg, name="this browser", transport=RecordedTransport.from_file(fixture))
    service.runner.clock = lambda: CLOCK
    out, last = [], None
    for method, path, body in HUB:
        if body is not None and body.get("snapshot") == "@last":
            body = {**body, "snapshot": last}
        status, payload = service.route(method, path, json.loads(json.dumps(body)) if body is not None else None)
        if isinstance(payload, dict) and isinstance(payload.get("snapshot"), dict):
            last = payload["snapshot"]
        out.append({"method": method, "path": path, "body": body, "status": status, "expect": _stable(_untimed(json.loads(json.dumps(payload))))})
    return out


def _untimed(d):
    """Drop when a gap or a sentence was seen: the counts and order stay."""
    if isinstance(d, dict) and "uptime_s" in d:
        d.pop("uptime_s")
        d.pop("egress", None)  # a page has no egress policy to report
    for key in ("gaps", "unanswered", "suggestions"):
        for e in d.get(key, []) if isinstance(d, dict) and isinstance(d.get(key), list) else []:
            for k in ("first_seen", "last_seen", "first", "last"):
                e.pop(k, None)
    return d


def _stable(d: dict) -> dict:
    """Drop the wall-clock fields (when the trace started, how long each step took)."""
    trace = d.get("trace")
    if isinstance(trace, dict):
        trace.pop("started", None)
        for step in trace.get("steps", []):
            step.pop("elapsed_ms", None)
    return d


def build() -> dict:
    reg = Registry.from_paths([ROOT / "examples", ROOT / "catalog"])
    assert not reg.errors, reg.errors
    fixture = ROOT / "tests" / "fixtures" / "examples.json"
    runs = []
    for case in CASES:
        # A run may bring its own fixture and env (the operator's credentials, say).
        runner = Runner(reg, RecordedTransport.from_file(ROOT / case["fixture"] if "fixture" in case else fixture), env=dict(case.get("env", {})), clock=lambda: CLOCK)
        extra = {k: case[k] for k in ("env", "fixture") if k in case}
        if "intent" in case:
            reading = reg.read(case["intent"], CLOCK)
            match = reading.winner
            assert match is not None, case
            outcome = runner.run_intent(case["intent"])
            runs.append({"intent": case["intent"], "tree": match.tree, "inputs": match.inputs, **extra, "expect": _stable(outcome.to_dict())})
        else:
            outcome = runner.run_tree(case["tree"], case["inputs"])
            runs.append({"tree": case["tree"], "inputs": case["inputs"], **extra, "expect": _stable(outcome.to_dict())})
    both = Registry.from_paths([ROOT / "examples", ROOT / "catalog"])
    for raw in LANGUAGE_TREES:
        both.add(build_tree(raw))
    readings = [{"text": text, "expect": {**(r := both.read(text, CLOCK)).to_dict(), "step": r.step()}} for text in READINGS]
    return {
        "pyrtan-conformance": 1,
        "clock": CLOCK.isoformat(),
        "fixture": "tests/fixtures/examples.json",
        "trees": {t.name: t.raw for t in reg.trees()},
        "runs": runs,
        "language_trees": LANGUAGE_TREES,
        "readings": readings,
        "nlp": [_nlp_case(fn, text) for fn, text in NLP],
        "proposals": [{"text": text, "expect": propose(text).to_dict()} for text in PROPOSALS],
        "hub": _hub_exchanges(both, fixture),
        "drafts": [{"tree": t, "gap": g, "expect": _draft(reg, t, g)} for t, g in DRAFTS],
    }


def _draft(reg: Registry, tree: str, gap: dict) -> dict | None:
    d = draft_branch(reg.get(tree), gap)
    return None if d is None else {k: v for k, v in d.to_dict().items() if k != "node_text"}


def main() -> int:
    out = ROOT / "tests" / "conformance" / "vectors.json"
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(CASES)} runs, {len(READINGS)} readings, {len(NLP)} rules, {len(PROPOSALS)} proposals, {len(HUB)} requests, {len(DRAFTS)} drafts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
