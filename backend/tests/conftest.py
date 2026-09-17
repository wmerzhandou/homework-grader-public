from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlmodel import SQLModel

from app import events
from app.config import get_settings
from app.db import _make_engine, reset_engine


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.test")
    get_settings.cache_clear()
    s = get_settings()
    yield s
    get_settings.cache_clear()


@pytest.fixture()
def engine(settings):
    eng = _make_engine(f"sqlite:///{settings.data_dir / 'test.db'}")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(eng)
    reset_engine(eng)
    yield eng
    reset_engine(None)


@pytest.fixture(autouse=True)
def _reset_events():
    events.reset()
    yield
    events.reset()
