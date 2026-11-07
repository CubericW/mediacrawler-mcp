"""Keep every test away from the real shared state in ~/MediaCrawler/.risk_guard."""
import pytest


@pytest.fixture(autouse=True)
def isolated_guard_dir(tmp_path, monkeypatch):
    guard = tmp_path / "guard"
    monkeypatch.setenv("MEDIACRAWLER_GUARD_DIR", str(guard))
    return guard
