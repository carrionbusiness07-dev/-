from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def seller_salt(monkeypatch):
    monkeypatch.setenv("SELLER_KEY_SALT", "test-salt")
