import pandas as pd
import pytest

from research.pit_validation import apply_pit_universe, validate_identity_intervals


def identity(code, uid, listed, delisted=""):
    return {
        "STK_CODE": code,
        "STK_UNI_CODE": uid,
        "LIST_DATE": listed,
        "DELIST_DATE": delisted,
        "SEC_MAR_PAR": "上海证券交易所",
        "LIST_SECT_PAR": "主板",
        "LIST_STA_PAR": "上市" if not delisted else "终止上市",
    }


def bar(code, date):
    return {
        "code": code, "date": date, "open": 10.0, "high": 11.0,
        "low": 9.0, "close": 10.5, "volume": 1000,
    }


def test_filters_non_listed_bars_and_resolves_reused_code_by_date():
    universe = pd.DataFrame([
        identity("600018", "old", "2000-07-19", "2006-10-20"),
        identity("600018", "new", "2006-10-26"),
        identity("000001", "bank", "2022-01-10"),
    ])
    bars = pd.DataFrame([
        bar("600018", "2000-07-19"),
        bar("600018", "2006-10-21"),  # 旧主体已退市、新主体未上市
        bar("600018", "2006-10-26"),
        bar("000001", "2022-01-03"),  # 上市前
        bar("000001", "2022-01-10"),
        bar("999999", "2022-01-10"),  # 无历史身份
    ])
    filtered, report = apply_pit_universe(bars, universe)
    assert list(filtered.sort_values("date")["STK_UNI_CODE"]) == ["old", "new", "bank"]
    assert report["input_rows"] == 6
    assert report["valid_listed_rows"] == 3
    assert report["removed_rows"] == 3
    assert report["removed_before_listing_rows"] == 2
    assert report["rows_without_identity"] == 1
    assert report["codes_without_valid_listed_bars"] == ["999999"]
    assert report["listing_delisting_filter_applied"] is True


def test_rejects_overlapping_identity_intervals():
    universe = pd.DataFrame([
        identity("600000", "one", "2000-01-01", "2010-01-01"),
        identity("600000", "two", "2009-01-01"),
    ])
    with pytest.raises(ValueError, match="区间重叠"):
        validate_identity_intervals(universe)


def test_rejects_current_constituent_universe_schema():
    bars = pd.DataFrame([bar("000001", "2022-01-10")])
    legacy = pd.DataFrame([{"code": "000001", "name": "平安银行"}])
    with pytest.raises(ValueError, match="主数据缺少字段"):
        apply_pit_universe(bars, legacy)


def test_unlisted_identities_do_not_create_ambiguous_matches():
    universe = pd.DataFrame([
        identity("300001", "future", ""),
        identity("300001", "active", "2020-01-01"),
    ])
    filtered, report = apply_pit_universe(
        pd.DataFrame([bar("300001", "2022-01-10")]), universe,
    )
    assert filtered["STK_UNI_CODE"].tolist() == ["active"]
    assert report["unique_identity_per_code_date"] is True
