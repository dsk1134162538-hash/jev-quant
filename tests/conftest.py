import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch, tmp_path):
    """每个测试都在独立的临时目录里跑，避免任何用到相对路径（"data/...", "reports/..." 等）
    的代码意外写进仓库真实的 data/ 或 reports/ 目录。"""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _no_real_pushplus(monkeypatch):
    """全局隔离：这台机器上真实跑着守护进程，环境变量/注册表里可能有真的 PUSHPLUS_TOKEN。
    测试永远不能真的发推送或联网，三道防线都得上：
      1) 删掉环境变量，
      2) 把 notify 模块的注册表兜底打成永远返回 None，
      3) 把 notify 模块默认会用到的 requests.post 打成一调用就报错——
         这样任何忘了注入假 post 的测试会当场炸掉，而不是悄悄发出真实请求。
    需要验证真实推送行为的测试自己注入假 post（见 tests/test_notify.py）。
    """
    monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
    monkeypatch.setattr("jevq.notify._windows_env_fallback", lambda name: None)

    def _boom(*a, **kw):
        raise AssertionError("real network in tests")

    monkeypatch.setattr("jevq.notify.requests.post", _boom)


@pytest.fixture(autouse=True)
def _no_extra_market_data(monkeypatch):
    """新闻催化、人气与资金、涨停补充数据都要联网：跑 run_day 的测试一律关掉，测这些功能的测试直接调模块函数。"""
    monkeypatch.setattr("jevq.config.CATALYST_ENABLED", False)
    monkeypatch.setattr("jevq.config.MARKET_EXTRA_ENABLED", False)
    monkeypatch.setattr("jevq.config.ZT_EXTRA_ENABLED", False)
    monkeypatch.setattr("jevq.config.TURN_CLAUDE_REQUIRED", False)   # 本机真装着 claude：测试不能真去问它
    monkeypatch.setattr("jevq.config.CLAUDE_BIN", "Z:/no/such/claude.exe")
    monkeypatch.setattr("jevq.config.SENTIMENT_BY_CLAUDE", False)
    monkeypatch.setattr("jevq.config.SECTOR_STRENGTH_BY_CLAUDE", False)
    monkeypatch.setattr("jevq.config.INDUSTRY_REFRESH_ENABLED", False)    # 新浪行业要联网
    monkeypatch.setattr("jevq.config.HOLDERS_REFRESH_ENABLED", False)     # 股东户数要联网
    monkeypatch.setattr("jevq.config.EVENTS_REFRESH_ENABLED", False)      # 财报日、解禁要联网
    monkeypatch.setattr("jevq.config.PREMARKET_CHECK_ENABLED", False)
    monkeypatch.setattr("jevq.config.LEGACY_BUY_ENABLED", True)       # 老规则的测试照旧按会买来测     # 盘前自检要联网、问 Claude
    monkeypatch.setattr("jevq.config.REBOUND_SECTOR_VETO", False)
    monkeypatch.setattr("jevq.config.AI_BOARD_ENABLED", False)        # AI 打首板在 test_ai_board 里单独测
    monkeypatch.setattr("dashboard.workbuddy_bridge._api_key", lambda: "")   # 本机有真 key，测试绝不能花钱调官方 API
