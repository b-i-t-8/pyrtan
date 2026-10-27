from pathlib import Path

import pytest

from pyrtan import Registry, RecordedTransport, Runner

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def examples() -> Registry:
    reg = Registry.from_paths([EXAMPLES])
    assert not reg.errors, reg.errors
    return reg


@pytest.fixture
def recorded() -> RecordedTransport:
    return RecordedTransport.from_file(FIXTURES / "examples.json")


@pytest.fixture
def runner(examples: Registry, recorded: RecordedTransport) -> Runner:
    return Runner(examples, recorded)


@pytest.fixture(autouse=True)
def _no_default_network(monkeypatch, tmp_path):
    """No test reads the default network from pyrtan.com; one that wants a
    default network points $PYRTAN_NETWORK at a file of its own."""
    monkeypatch.setenv("PYRTAN_NETWORK", str(tmp_path / "no-default-network.json"))
