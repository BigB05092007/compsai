"""
Pytest configuration: every test runs offline against the bundled fixtures.

Sets the environment before any compsai module is imported so that no test can reach
sec.gov, Yahoo Finance, or the Anthropic API by accident, and so cached responses go to a
throw-away directory instead of data/cache.
"""

import os
import tempfile
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

_TMP_CACHE = tempfile.mkdtemp(prefix="compsai-test-cache-")

os.environ["COMPSAI_OFFLINE"] = "1"
os.environ["COMPSAI_FIXTURE_DIR"] = str(FIXTURE_DIR)
os.environ["COMPSAI_CACHE_DIR"] = _TMP_CACHE
os.environ.setdefault("SEC_USER_AGENT", "CompsAI test@example.com")
# Never let a developer's real key leak into tests; commentary tests use a fake client.
os.environ.pop("ANTHROPIC_API_KEY", None)


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def tmp_cache_dir(tmp_path, monkeypatch) -> Path:
    """A fresh cache directory for tests that exercise caching."""
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("COMPSAI_CACHE_DIR", str(d))
    return d
