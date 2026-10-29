"""历史时点股票池校验：按交易日期匹配上市身份并过滤非上市期间行情。"""

from __future__ import annotations

import pandas as pd

UNIVERSE_REQUIRED = {
    "STK_CODE", "STK_UNI_CODE", "LIST_DATE", "DELIST_DATE",
    "SEC_MAR_PAR", "LIST_SECT_PAR", "LIST_STA_PAR",
}
PRICE_REQUIRED = {"date", "code", "open", "high", "low", "close", "volume"}


def validate_identity_intervals(universe: pd.DataFrame) -> None:
    missing = UNIVERSE_REQUIRED.difference(universe.columns)
    if missing:
        raise ValueError(f"PIT 主数据缺少字段：{sorted(missing)}")
    if universe.empty or universe["STK_UNI_CODE"].duplicated().any():
        raise ValueError("PIT 主数据为空或统一编码重复")
    for code, group in universe.groupby("STK_CODE"):
        intervals = group[["STK_UNI_CODE", "LIST_DATE", "DELIST_DATE"]].copy()
        intervals["LIST_DATE"] = pd.to_datetime(intervals["LIST_DATE"], errors="coerce")
        intervals["DELIST_DATE"] = pd.to_datetime(intervals["DELIST_DATE"], errors="coerce")
        if intervals["LIST_DATE"].isna().any():
            # 拟上市等尚无上市日的身份没有可匹配行情，不参与区间重叠判断。
            intervals = intervals.dropna(subset=["LIST_DATE"])
        intervals = intervals.sort_values("LIST_DATE")
        previous_end = intervals["DELIST_DATE"].shift(1)
        overlaps = intervals["LIST_DATE"] < previous_end
        if overlaps.any():
            raise ValueError(f"股票代码 {code} 的上市身份日期区间重叠，无法唯一匹配")


def apply_pit_universe(
    stocks: pd.DataFrame, universe: pd.DataFrame
) -> tuple[pd.DataFrame, dict]:
    """返回带统一编码的有效上市行情及可审计覆盖报告。"""
    missing = PRICE_REQUIRED.difference(stocks.columns)
    if missing:
        raise ValueError(f"PIT 行情缺少字段：{sorted(missing)}")
    validate_identity_intervals(universe)
    bars = stocks.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars["code"] = bars["code"].astype(str).str.zfill(6)
    if bars["date"].isna().any() or bars.duplicated(["code", "date"]).any():
        raise ValueError("PIT 行情日期无效或代码日期重复")

    identities = universe.copy()
    identities["LIST_DATE"] = pd.to_datetime(identities["LIST_DATE"], errors="coerce")
    identities["DELIST_DATE"] = pd.to_datetime(identities["DELIST_DATE"], errors="coerce")
    identities = identities.rename(columns={"STK_CODE": "code"})
    identities["code"] = identities["code"].astype(str).str.zfill(6)
    merged = bars.reset_index(names="bar_order").merge(
        identities[["code", "STK_UNI_CODE", "LIST_DATE", "DELIST_DATE"]],
        on="code", how="left", validate="many_to_many",
    )
    unmatched_identity = merged["STK_UNI_CODE"].isna()
    before_listing = merged["LIST_DATE"].isna() | (merged["date"] < merged["LIST_DATE"])
    after_delisting = merged["DELIST_DATE"].notna() & (merged["date"] > merged["DELIST_DATE"])
    active = ~unmatched_identity & ~before_listing & ~after_delisting
    selected = merged.loc[active].copy()
    matched_counts = selected.groupby("bar_order")["STK_UNI_CODE"].transform("size")
    if (matched_counts != 1).any():
        raise ValueError("至少一条行情匹配到多个历史身份")
    selected = selected.sort_values("bar_order")
    filtered = bars.loc[selected["bar_order"]].copy()
    filtered["STK_UNI_CODE"] = selected["STK_UNI_CODE"].to_numpy()
    if filtered.duplicated(["STK_UNI_CODE", "date"]).any():
        raise ValueError("过滤后统一编码与日期重复")

    requested_codes = set(bars["code"])
    covered_codes = set(filtered["code"])
    # 一个有效行情可能同时存在其他身份的未上市候选行，统计剔除原因时只看真正被删除的原始行情。
    removed_bar = ~merged["bar_order"].isin(selected["bar_order"])
    report = {
        "input_rows": int(len(bars)),
        "valid_listed_rows": int(len(filtered)),
        "removed_rows": int(len(bars) - len(filtered)),
        "removed_before_listing_rows": int((removed_bar & before_listing & ~unmatched_identity).sum()),
        "removed_after_delisting_rows": int((removed_bar & after_delisting).sum()),
        "rows_without_identity": int((removed_bar & unmatched_identity).sum()),
        "input_codes": int(len(requested_codes)),
        "covered_codes": int(len(covered_codes)),
        "codes_without_valid_listed_bars": sorted(requested_codes - covered_codes),
        "universe_identities": int(universe["STK_UNI_CODE"].nunique()),
        "matched_identities": int(filtered["STK_UNI_CODE"].nunique()),
        "listing_delisting_filter_applied": True,
        "unique_identity_per_code_date": True,
    }
    return filtered.sort_values(["code", "date"]).reset_index(drop=True), report
