from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def seller_salt(monkeypatch):
    monkeypatch.setenv("SELLER_KEY_SALT", "test-salt")


@pytest.fixture(autouse=True)
def no_photo_hashing(monkeypatch):
    # run_cycle в тестах не ходит за фото в сеть; тесты шага фото включают его сами.
    monkeypatch.setenv("PHOTO_HASHING", "0")
