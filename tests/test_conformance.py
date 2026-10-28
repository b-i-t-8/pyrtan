"""Both runtimes read the same tree and say the same thing, read the same
sentence the same way, and (the hub in a browser) answer the same requests
the same way a service does.

The vectors are what Python says; node runs pyrtan.js over them. The
vectors file is regenerated here and compared, so it cannot drift from
the examples or the runtime without this test saying so.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "conformance"))

from make_vectors import build  # noqa: E402

VECTORS = ROOT / "tests" / "conformance" / "vectors.json"


def test_vectors_are_current():
    fresh = json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    assert VECTORS.read_text(encoding="utf-8") == fresh, "tests/conformance/vectors.json is stale: run `uv run python tests/conformance/make_vectors.py`"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_runtime_agrees_with_python():
    run = subprocess.run(["node", str(ROOT / "tests" / "conformance" / "run.cjs")], capture_output=True, text=True, cwd=str(ROOT))
    assert run.returncode == 0, run.stdout + run.stderr
    v = json.loads(VECTORS.read_text())
    total = sum(len(v[k]) for k in ("runs", "readings", "nlp", "proposals", "hub"))
    assert f"{total}/{total} agree" in run.stdout


# ---------------------------------------------------------------- identity.js and identity.py

IDENTITY_VECTORS = ROOT / "tests" / "conformance" / "identity.json"
needs_crypto = pytest.mark.skipif(importlib.util.find_spec("cryptography") is None, reason="needs pyrtan[identity]")


@needs_crypto
def test_identity_vectors_are_current():
    from make_identity_vectors import build as build_identity
    fresh = json.dumps(build_identity(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    assert IDENTITY_VECTORS.read_text(encoding="utf-8") == fresh, "tests/conformance/identity.json is stale: run `uv run python tests/conformance/make_identity_vectors.py`"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_console_identity_agrees_with_python():
    run = subprocess.run(["node", str(ROOT / "tests" / "conformance" / "identity.cjs")], capture_output=True, text=True, cwd=str(ROOT))
    assert run.returncode == 0, run.stdout + run.stderr
    assert " agree" in run.stdout and "DIFF" not in run.stdout
