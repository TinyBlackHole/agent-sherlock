import pytest


@pytest.fixture(autouse=True)
def isolate_sherlock_config(monkeypatch, tmp_path):
    """Keep tests independent from the user's real Sherlock configuration."""
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(tmp_path / "config"))
