"""收盘后的人气与资金快照（dj 2026-10-10「多接一点数据」）：东财人气榜、同花顺热榜（带题材标签和几天几板）、
同花顺概念资金流、龙虎榜。存 day_cache（kind='market_extra'，key = 种类），写进复盘报告。**只记录，不参与交易。**

东财 push2 系列接口走系统代理（VPN）会被断开，这里只用不经 push2 的接口：人气榜用 emappdata 只取排名，
资金流用同花顺的版本。单项失败不影响其他项。
"""
import json
from datetime import date

from jevq import config

KIND = "market_extra"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"
EM_HOT_BODY = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38", "marketType": "", "pageNo": 1, "pageSize": 100}


def em_hot(d: date) -> list[dict]:
    import requests
    r = requests.post("https://emappdata.eastmoney.com/stockrank/getAllCurrentList", json=EM_HOT_BODY,
                      headers={"User-Agent": UA}, timeout=config.ZT_EXTRA_TIMEOUT)
    r.raise_for_status()
    return [{"rank": it["rk"], "code": it["sc"][2:]} for it in r.json().get("data") or []]


def ths_hot(d: date) -> list[dict]:
    import requests
    r = requests.get("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
                     params={"stock_type": "a", "type": "day", "list_type": "normal"},
                     headers={"User-Agent": UA}, timeout=config.ZT_EXTRA_TIMEOUT)
    r.raise_for_status()
    out = []
    for it in (r.json().get("data") or {}).get("stock_list") or []:
        tag = it.get("tag") or {}
        out.append({"rank": it.get("order"), "code": it.get("code"), "name": it.get("name") or "",
                    "pct": it.get("rise_and_fall"), "concepts": tag.get("concept_tag") or [],
                    "tag": tag.get("popularity_tag") or ""})
    return out


def concept_flow(d: date) -> list[dict]:
    from jevq.data.akdata import _ak
    import akshare as ak
    df = _ak(ak.stock_fund_flow_concept, symbol="即时")
    return [{"concept": r["行业"], "pct": float(r["行业-涨跌幅"]), "net": float(r["净额"]),   # 净额：亿元
             "leader": r["领涨股"]} for _, r in df.iterrows()]


def lhb(d: date) -> list[dict]:
    from jevq.data.akdata import _ak
    import akshare as ak
    ds = d.strftime("%Y%m%d")
    df = _ak(ak.stock_lhb_detail_em, start_date=ds, end_date=ds)
    return [{"code": r["代码"], "name": r["名称"], "reason": r["解读"], "pct": float(r["涨跌幅"]),
             "net": round(float(r["龙虎榜净买额"]) / 1e4, 1)} for _, r in df.iterrows()]   # 万元


FETCHERS = {"em_hot": em_hot, "ths_hot": ths_hot, "concept_flow": concept_flow, "lhb": lhb}


def snapshot(conn, d: date, lhb_day: date | None = None) -> dict:
    """每项取一次存库，返回 {种类: 条数 | None（失败）}。
    龙虎榜傍晚才公布，收盘后报告那会儿当天的还没有：lhb_day 传上一交易日，存在那一天名下。"""
    got = {}
    for kind, fn in FETCHERS.items():
        day = (lhb_day or d) if kind == "lhb" else d
        try:
            rows = fn(day)
        except Exception as e:
            print(f"[market_extra] {kind} 失败: {e!r}")
            got[kind] = None
            continue
        conn.execute("INSERT OR REPLACE INTO day_cache(date, kind, key, value) VALUES (?, ?, ?, ?)",
                     (day.isoformat(), KIND, kind, json.dumps(rows, ensure_ascii=False)))
        got[kind] = len(rows)
    conn.commit()
    return got


def cached(conn, d: date) -> dict[str, list]:
    return {k: json.loads(v) for k, v in conn.execute(
        "SELECT key, value FROM day_cache WHERE date=? AND kind=?", (d.isoformat(), KIND))}


def _latest_lhb(conn, d: date) -> tuple[str, list] | None:
    row = conn.execute("SELECT date, value FROM day_cache WHERE kind=? AND key='lhb' AND date<=? "
                       "ORDER BY date DESC LIMIT 1", (KIND, d.isoformat())).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def report_lines(conn, d: date, names: dict[str, str] | None = None, lhb_day: date | None = None) -> list[str]:
    data = cached(conn, d)
    lhb_at = _latest_lhb(conn, lhb_day or d)
    if lhb_at:
        data["lhb"] = lhb_at[1]
    if not data:
        return []
    names = names or {}
    lines = ["", "## 人气与资金", "", "收盘后快照，只记录，不参与交易。", ""]
    if rows := data.get("ths_hot"):
        lines += ["**同花顺热榜前 10**：" + "；".join(
            f"{r['name']} {r['pct']:+.1f}%" + (f"｜{r['tag']}" if r["tag"] else "") +
            (f"｜{'、'.join(r['concepts'][:2])}" if r["concepts"] else "") for r in rows[:10] if r.get("pct") is not None), ""]
    if rows := data.get("em_hot"):
        known = {**{r["code"]: r["name"] for r in data.get("ths_hot", []) if r.get("name")}, **names}
        lines += ["**东财人气榜前 10**：" + "、".join(f"{r['code']} {known.get(r['code'], '')}".strip() for r in rows[:10]), ""]
    if rows := data.get("concept_flow"):
        from jevq.data.catalyst import _NOT_TOPIC        # 指数、资金标签类不是题材
        inflow = sorted((r for r in rows if not _NOT_TOPIC.search(r["concept"])), key=lambda r: -r["net"])
        lines += ["**概念资金净流入前 8**（亿元）：" + "；".join(
            f"{r['concept']} {r['net']:+.1f}（{r['pct']:+.1f}%，领涨 {r['leader']}）" for r in inflow[:8]), "",
                  "**净流出前 5**：" + "；".join(f"{r['concept']} {r['net']:+.1f}" for r in inflow[::-1][:5] if r["net"] < 0), ""]
    if rows := data.get("lhb"):
        best: dict[str, dict] = {}
        for r in rows:                   # 同一只票几条上榜理由（日涨幅偏离、三日涨幅……）只留净买最大的一条
            if r["code"] not in best or r["net"] > best[r["code"]]["net"]:
                best[r["code"]] = r
        top = sorted(best.values(), key=lambda r: -r["net"])[:8]
        when = "今日" if lhb_at and lhb_at[0] == d.isoformat() else f"上一交易日（{lhb_at[0][5:]}）"
        lines += [f"**{when}龙虎榜净买入前 8**（万元）：" + "；".join(
            f"{r['name']} {r['net']:+,.0f}（{r['pct']:+.1f}%，{r['reason'][:16]}）" for r in top), ""]
    return lines
