"""测试 Api 的规则相关方法"""
from datetime import date, timedelta

import pytest

from factories import quote
from jevq import db
from jevq.app.api import Api


@pytest.fixture
def tmp_db(tmp_path):
    """创建临时数据库"""
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    conn.close()
    return db_path


def test_rule_fields(tmp_db):
    """rule_fields 返回字段元数据"""
    api = Api(db_path=tmp_db, runner=lambda: None)
    result = api.rule_fields()
    assert result["ok"]
    assert "fields" in result
    fields = result["fields"]
    # 检查基本结构
    assert isinstance(fields, list)
    assert len(fields) > 0
    # 检查必要字段
    pct_field = next((f for f in fields if f["key"] == "pct"), None)
    assert pct_field is not None
    assert pct_field["label"] == "当前涨幅"
    assert pct_field["kind"] == "num"


def test_rule_validate_success(tmp_db):
    """rule_validate 对有效规则返回空错误"""
    api = Api(db_path=tmp_db, runner=lambda: None)
    spec = {
        "name": "测试策略",
        "capital": 30000,
        "select": {
            "mode": "all",
            "conditions": [
                {"field": "pct", "op": "between", "value": [-3, 0]}
            ]
        },
        "entry": {"mode": "all", "conditions": []},
        "exits": [
            {
                "name": "止损",
                "when": {
                    "mode": "all",
                    "conditions": [
                        {"field": "pct", "op": "<=", "value": -7}
                    ]
                }
            }
        ],
        "rank": {"field": "amount", "order": "desc"},
        "jev": {"notice": True, "news": False}
    }
    result = api.rule_validate(spec)
    assert result["ok"]
    assert result["errors"] == []


def test_rule_validate_missing_name(tmp_db):
    """rule_validate 检测缺失策略名"""
    api = Api(db_path=tmp_db, runner=lambda: None)
    spec = {
        "capital": 30000,
        "select": {
            "mode": "all",
            "conditions": [{"field": "pct", "op": "between", "value": [-3, 0]}]
        },
        "exits": [
            {"name": "止损", "when": {"mode": "all", "conditions": [{"field": "pct", "op": "<=", "value": -7}]}}
        ]
    }
    result = api.rule_validate(spec)
    assert result["ok"]
    assert any("策略名称" in e for e in result["errors"])


def test_rule_validate_no_stop_loss(tmp_db):
    """rule_validate 检测缺失止损"""
    api = Api(db_path=tmp_db, runner=lambda: None)
    spec = {
        "name": "无止损",
        "capital": 30000,
        "select": {
            "mode": "all",
            "conditions": [{"field": "pct", "op": "between", "value": [-3, 0]}]
        },
        "exits": [
            {"name": "时间", "when": {"mode": "all", "conditions": [{"field": "time", "op": ">=", "value": "14:50"}]}}
        ]
    }
    result = api.rule_validate(spec)
    assert result["ok"]
    assert any("止损" in e for e in result["errors"])


TODAY = date.today()
TRADE_DAYS = [TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=1)]
UNIVERSE = {"600001": "甲", "600002": "乙", "600003": "丙"}
QUOTES = {
    "600001": quote("600001", pct=-1, amount=5e8, name="甲股份"),
    "600002": quote("600002", pct=-2, amount=9e8),
    "600003": quote("600003", pct=10, amount=1e7),                 # 封死涨停
    "600009": quote("600009", pct=-1, amount=20e8),                # 不在股票池
}
VALID = {
    "name": "试跑", "capital": 30000,
    "select": {"mode": "all", "conditions": [{"field": "pct", "op": "between", "value": [-3, 0]}]},
    "rank": {"field": "amount", "order": "desc"},
    "entry": {"mode": "all", "conditions": [{"field": "amount_rank", "op": "<=", "value": 2}]},
    "exits": [{"when": {"mode": "all", "conditions": [{"field": "pct", "op": "<=", "value": -7}]},
               "reason": "止损"}],
    "jev": {"notices": True, "news": True},
}


def make_api(tmp_db, calls=None, fetch_error=None):
    calls = calls if calls is not None else {}

    def fetch(codes):
        calls["fetch"] = calls.get("fetch", 0) + 1
        if fetch_error:
            raise fetch_error
        return dict(QUOTES)                     # 真实接口也可能多给；股票池过滤必须在 preview 里做

    def load_universe(today, days):
        calls["universe"] = calls.get("universe", 0) + 1
        assert days == TRADE_DAYS
        return dict(UNIVERSE)

    return Api(db_path=tmp_db, runner=lambda: None, fetch_quotes=fetch,
               load_universe=load_universe, trade_days=lambda: TRADE_DAYS)


def test_rule_preview_select_rank_entry_with_runtime_getter(tmp_db):
    result = make_api(tmp_db).rule_preview(VALID)
    assert result["ok"], result
    assert result["total"] == 2                                        # 600009 不在股票池、600003 不满足选股
    assert [(r["code"], r["name"], r["rank"]) for r in result["rows"]] == [("600002", "乙", 1), ("600001", "甲", 2)]
    assert result["rows"][0]["values"] == {"当前涨幅": "-2.00%", "成交额名次": "1名", "成交额": "9.00亿"}


def test_rule_preview_sealed_uses_quote_limit_up(tmp_db):
    spec = dict(VALID, select={"mode": "all", "conditions": [{"field": "sealed", "op": "is", "value": True}]},
                entry={"mode": "all", "conditions": []})
    result = make_api(tmp_db).rule_preview(spec)
    assert [r["code"] for r in result["rows"]] == ["600003"]


def test_rule_preview_uses_history_features_from_closes(tmp_db):
    conn = db.connect(tmp_db)
    conn.executemany("INSERT INTO closes(date, code, close) VALUES (?, ?, ?)",
                     [((TODAY - timedelta(days=k)).isoformat(), "600001", p)
                      for k, p in zip((4, 3, 2, 1), (10.0, 10.0, 10.0, 11.0))])
    conn.commit()
    conn.close()
    spec = dict(VALID, select={"mode": "all", "conditions": [
        {"field": "ret_n", "param": {"n": 3}, "op": ">", "value": 5}]}, entry={"mode": "all", "conditions": []})
    result = make_api(tmp_db).rule_preview(spec)
    assert result["total"] == 1
    assert result["rows"][0]["values"]["近N日涨幅(N=3)"] == "10.00%"


def test_rule_preview_reads_latest_sentiment_when_panel_not_running(tmp_db):
    """后台 daemon 在交易、面板自己没跑：面板状态里没有情绪/外围，从决策表最近一条情绪记录读。"""
    conn = db.connect(tmp_db)
    conn.executemany("INSERT INTO decisions(ts, code, verdict, detail) VALUES (?, '', 'sentiment', ?)", [
        ("2026-09-24T10:00:00", '{"stats": {}, "label": "冰点", "overseas": "偏弱"}'),
        ("2026-09-25T14:50:00", '{"stats": {}, "label": "火热", "overseas": "偏强"}')])
    conn.commit()
    conn.close()
    spec = dict(VALID, select={"mode": "all", "conditions": [
        {"field": "sentiment", "op": "in", "value": ["火热"]},
        {"field": "overseas", "op": "in", "value": ["偏强"]}]}, entry={"mode": "all", "conditions": []})
    assert make_api(tmp_db).rule_preview(spec)["total"] == 3


def test_rule_preview_env_empty_without_sentiment_rows(tmp_db):
    spec = dict(VALID, select={"mode": "all", "conditions": [
        {"field": "sentiment", "op": "in", "value": ["火热", "正常"]}]}, entry={"mode": "all", "conditions": []})
    assert make_api(tmp_db).rule_preview(spec)["total"] == 0


def test_rule_preview_caches_market_data_for_60_seconds(tmp_db):
    calls = {}
    api = make_api(tmp_db, calls)
    api.rule_preview(VALID)
    api.rule_preview(VALID)
    assert calls == {"fetch": 1, "universe": 1}


@pytest.mark.parametrize("spec", ["garbage", None, ["x"]])
def test_rule_preview_non_dict_returns_errors(tmp_db, spec):
    result = make_api(tmp_db).rule_preview(spec)
    assert result["ok"] is False and result["errors"]


def test_rule_preview_malformed_condition_matches_nothing(tmp_db):
    spec = {"select": {"conditions": [{"field": "pct", "op": "between", "value": 5}]}}
    assert make_api(tmp_db).rule_preview(spec) == {"ok": True, "total": 0, "rows": []}


def test_rule_preview_fetch_failure_returns_errors(tmp_db):
    result = make_api(tmp_db, fetch_error=RuntimeError("net down")).rule_preview(VALID)
    assert result["ok"] is False and "net down" in result["errors"][0]


def test_rule_create_uses_injected_calendar_and_is_listed_before_active(tmp_db):
    api = make_api(tmp_db)
    created = api.rule_create(VALID)
    assert created == {"ok": True, "id": "c1", "version": 1}
    listed = api.rules_list()
    assert listed["ok"]
    [rule] = listed["rules"]
    assert (rule["id"], rule["name"], rule["version"], rule["enabled"], rule["running"]) == ("c1", "试跑", 1, True, False)
    assert rule["active_from"] == (TODAY + timedelta(days=1)).isoformat()


def test_rule_create_bad_spec_returns_errors(tmp_db):
    result = make_api(tmp_db).rule_create({"name": ""})
    assert result["ok"] is False and result["errors"]


def test_rule_create_without_capital_freezes_default_capital(tmp_db):
    api = make_api(tmp_db)
    spec = {k: v for k, v in VALID.items() if k != "capital"}
    assert api.rule_create(spec)["ok"]
    conn = db.connect(tmp_db)
    try:
        from jevq.data import custom_store
        assert custom_store.capital_of(conn, "c1") == 30000
    finally:
        conn.close()
    changed = api.rule_update("c1", dict(spec, capital=100000))
    assert changed["ok"] is False and "资金" in changed["errors"][0]
