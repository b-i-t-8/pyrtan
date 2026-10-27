"""Write tests/conformance/vectors.json: what both runtimes must say.

Every example tree as JSON (the TOML's own table), and for a fixed set of
runs (a sentence read by the Python matcher, or a tree and inputs) the
outcome the Python runtime produced against the recorded responses in
tests/fixtures/examples.json, with a fixed clock. The JavaScript runtime
(src/pyrtan/js/pyrtan.js) must produce the same status, say, output,
reason, path and gaps; tests/conformance/run.cjs checks it, and
tests/test_conformance.py keeps this file from drifting.

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

CLOCK = _dt.datetime(2026, 10, 25, 12, 0, tzinfo=_dt.timezone.utc)
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
]


def _stable(d: dict) -> dict:
    """Drop the wall-clock fields (when the trace started, how long each step took)."""
    trace = d.get("trace")
    if isinstance(trace, dict):
        trace.pop("started", None)
        for step in trace.get("steps", []):
            step.pop("elapsed_ms", None)
    return d


def build() -> dict:
    reg = Registry.from_paths([ROOT / "examples"])
    assert not reg.errors, reg.errors
    fixture = ROOT / "tests" / "fixtures" / "examples.json"
    runs = []
    for case in CASES:
        runner = Runner(reg, RecordedTransport.from_file(fixture), clock=lambda: CLOCK)
        if "intent" in case:
            reading = reg.read(case["intent"], CLOCK)
            match = reading.winner
            assert match is not None, case
            outcome = runner.run_tree(match.tree, match.inputs)
            runs.append({"intent": case["intent"], "tree": match.tree, "inputs": match.inputs, "expect": _stable(outcome.to_dict())})
        else:
            outcome = runner.run_tree(case["tree"], case["inputs"])
            runs.append({"tree": case["tree"], "inputs": case["inputs"], "expect": _stable(outcome.to_dict())})
    return {
        "pyrtan-conformance": 1,
        "clock": CLOCK.isoformat(),
        "fixture": "tests/fixtures/examples.json",
        "trees": {t.name: t.raw for t in reg.trees()},
        "runs": runs,
    }


def main() -> int:
    out = ROOT / "tests" / "conformance" / "vectors.json"
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(CASES)} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
