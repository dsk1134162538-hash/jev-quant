"""测试 Api 的规则相关方法"""
import pytest
from datetime import date
from jevq.app.api import Api
from jevq import db
from jevq.data import custom_store


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
                {"field": "pct", "op": "between", "val": [-3, 0]}
            ]
        },
        "entry": {"mode": "all", "conditions": []},
        "exits": [
            {
                "name": "止损",
                "when": {
                    "mode": "all",
                    "conditions": [
                        {"field": "pct", "op": "<=", "val": -7}
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
            "conditions": [{"field": "pct", "op": "between", "val": [-3, 0]}]
        },
        "exits": [
            {"name": "止损", "when": {"mode": "all", "conditions": [{"field": "pct", "op": "<=", "val": -7}]}}
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
            "conditions": [{"field": "pct", "op": "between", "val": [-3, 0]}]
        },
        "exits": [
            {"name": "时间", "when": {"mode": "all", "conditions": [{"field": "time", "op": ">=", "val": "14:50"}]}}
        ]
    }
    result = api.rule_validate(spec)
    assert result["ok"]
    assert any("止损" in e for e in result["errors"])


def test_rule_preview_basic(tmp_db, monkeypatch):
    """rule_preview 返回候选股票（模拟行情）"""
    from jevq.models import Quote

    # 模拟行情
    mock_quotes = {
        "000001": Quote(code="000001", name="平安银行", price=10.5, pre_close=10.0,
                       pct=5.0, open_pct=2.0, high_pct=6.0, low_pct=1.0,
                       amount=500_000_000, turnover=2.5, volume_ratio=1.2,
                       float_mcap=100, total_mcap=150, high=10.6, low=10.1, open=10.2),
        "000002": Quote(code="000002", name="万科A", price=9.0, pre_close=10.0,
                       pct=-10.0, open_pct=-5.0, high_pct=-3.0, low_pct=-10.0,
                       amount=300_000_000, turnover=3.0, volume_ratio=2.0,
                       float_mcap=200, total_mcap=300, high=9.7, low=9.0, open=9.5),
    }

    def mock_fetch(universe):
        return mock_quotes

    # 模拟 history_features
    def mock_history_features(conn, codes, today):
        return {
            "000001": {"ret": {3: 10.0, 5: 15.0}, "limit_up": {3: False}, "boards": 0, "ma": {5: 10.2}},
            "000002": {"ret": {3: -5.0, 5: -8.0}, "limit_up": {3: False}, "boards": 0, "ma": {5: 9.5}},
        }

    from jevq.data import features
    monkeypatch.setattr(features, "history_features", mock_history_features)

    api = Api(db_path=tmp_db, fetch_quotes=mock_fetch, runner=lambda: None)

    spec = {
        "name": "涨幅筛选",
        "capital": 30000,
        "select": {
            "mode": "all",
            "conditions": [
                {"field": "pct", "op": "between", "val": [-3, 10]}
            ]
        },
        "rank": {"field": "amount", "order": "desc"},
        "exits": [
            {"name": "止损", "when": {"mode": "all", "conditions": [{"field": "pct", "op": "<=", "val": -7}]}}
        ]
    }

    result = api.rule_preview(spec)
    assert result["ok"], result.get("error")
    assert result["total"] == 1  # 只有000001满足涨幅条件
    assert len(result["rows"]) == 1
    assert result["rows"][0]["code"] == "000001"
    assert result["rows"][0]["name"] == "平安银行"
