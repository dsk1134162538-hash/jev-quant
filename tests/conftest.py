import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch, tmp_path):
    """每个测试都在独立的临时目录里跑，避免任何用到相对路径（"data/...", "reports/..." 等）
    的代码意外写进仓库真实的 data/ 或 reports/ 目录。"""
    monkeypatch.chdir(tmp_path)
