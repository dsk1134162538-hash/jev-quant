"""推送正文的内容构建：把 Jev 留痕（decisions.trace）翻译成人看得懂的推送文字。

和发送机制（jevq.notify.Notifier）分开：这里全是纯函数，不碰网络、不碰 Notifier，
方便单独测试格式对不对。任何字段缺失都只影响输出内容，绝不抛异常——调用方（run.py）
拿到的永远是一段能发的文字，最差也就是缺了某一段明细。
"""
import json
from collections import Counter
from datetime import date, datetime

from jevq import accounts as accounts_mod
from jevq.config import ACCOUNT_NAMES, ACCOUNT_STRATEGY, ACCOUNTS, INITIAL_CASH, NOTICE_LOOKBACK_DAYS
from jevq.ledger import Ledger
from jevq.models import Fill


def fill_basic_line(f: Fill, stock_name: str) -> str:
    return f"{f.ts:%H:%M} {f.code} {stock_name} {f.shares}股 @{f.price}｜{f.reason}"


def sell_pnl(conn, f: Fill) -> tuple[float, float, int] | None:
    """卖出这笔的 (盈亏%, 盈亏金额, 持有天数)。优先用买入那笔的成本价+费用对卖出净得算金额；
    找不到对应买入成交（比如非本程序建的仓）就返回 None（调用方退化为只显示基本信息）。"""
    row = conn.execute(
        "SELECT price, fee, ts FROM fills WHERE account=? AND code=? AND side='buy' AND ts<? "
        "ORDER BY ts DESC LIMIT 1",
        (f.account, f.code, f.ts.isoformat())).fetchone()
    if not row or not row[0]:
        return None
    buy_price, buy_fee, buy_ts = row
    cost = buy_price * f.shares + (buy_fee or 0.0)
    proceeds = f.price * f.shares - f.fee
    pct = round((proceeds / cost - 1) * 100, 2) if cost else round((f.price / buy_price - 1) * 100, 2)
    amount = round(proceeds - cost, 2)
    held_days = (f.ts.date() - datetime.fromisoformat(buy_ts).date()).days
    return pct, amount, held_days


def _strategy_of(account: str) -> str | None:
    """账户所属策略：内置查表；自定义账户 c{n}_main / c{n}_shadow 的策略就是 c{n}。"""
    if account in ACCOUNT_STRATEGY:
        return ACCOUNT_STRATEGY[account]
    sid, _, kind = account.rpartition("_")
    return sid if kind in ("main", "shadow") and accounts_mod._is_custom_strategy(sid) else None


def load_buy_trace(conn, f: Fill) -> dict | None:
    """找到这笔主账户买入对应的决策留痕：同代码、同策略、当天，按 id 倒序第一条
    trace.outcome.bought 为真的记录（板块策略会在同一 trace 对象上追加「后来封死了」之类的信息，
    所以要拿最新一条，不是最早那条买入时的快照）。"""
    strategy = _strategy_of(f.account)
    rows = conn.execute(
        "SELECT trace FROM decisions WHERE code=? AND strategy=? AND substr(ts, 1, 10)=? "
        "AND trace IS NOT NULL ORDER BY id DESC",
        (f.code, strategy, f.ts.date().isoformat())).fetchall()
    for (raw,) in rows:
        try:
            trace = json.loads(raw)
        except Exception:
            continue
        if isinstance(trace, dict) and trace.get("outcome", {}).get("bought") is True:
            return trace
    return None


def fmt_probs(probs: dict | None, top: int | None = None) -> str:
    if not probs:
        return ""
    items = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    if top:
        items = items[:top]
    return "/".join(f"{k}{v:.2f}" for k, v in items)


def fmt_judge_label(label, trace: dict | None) -> str:
    if not trace:
        return "未知"
    probs = fmt_probs(trace.get("probabilities"))
    return f"{label}（{probs}）" if probs else f"{label}"


def buy_push_notices_line(notices: dict | list) -> list[str]:
    if isinstance(notices, list):          # 2026-09-26 之前的旧 trace
        lines = [f"▍Jev 公告排雷（近 {NOTICE_LOOKBACK_DAYS} 天 {len(notices)} 条，全部放行）"]
        for item in notices[:5]:
            date_str = (item.get("date") or "")[5:10]
            title = (item.get("title") or "")[:20]
            if item.get("source") == "keyword":
                lines.append(f"· {date_str} {title} → 标题关键词判定，未调用 Jev")
            else:
                probs = fmt_probs(item.get("probabilities"), top=2)
                line = f"· {date_str} {title} → {item.get('label')}"
                lines.append(f"{line} {probs}" if probs else line)
        return lines
    suspects, others = notices.get("suspects") or [], notices.get("others") or 0
    if not suspects:
        return [f"▍Jev 公告排雷（近 {NOTICE_LOOKBACK_DAYS} 天 {others} 条公告，无疑似雷）"]
    lines = [f"▍Jev 公告排雷（近 {NOTICE_LOOKBACK_DAYS} 天：疑似 {len(suspects)} 条，其余 {others} 条无关）"]
    for s in suspects[:3]:                 # trace 里已按风险分降序
        read = "已读正文" if s.get("body_read") else "仅标题"
        lines.append(f"· {(s.get('date') or '')[5:10]} {(s.get('title') or '')[:20]} → "
                     f"{s.get('category')}·{s.get('level')} {float(s.get('score') or 0):.2f}（{read}）放行")
    return lines                           # 主账户买入时 trace 必然是放行的


def buy_push_news_lines(news: dict | None) -> list[str]:
    items = (news or {}).get("news") or []
    if not items:
        return ["▍Jev 消息面：无新闻，按中性"]
    conf = news.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "未知"
    lines = [f"▍Jev 消息面：{news.get('label')}（{fmt_probs(news.get('probabilities'))}，"
             f"置信度 {conf_str}）"]
    lines += [f"· {n[:30]}" for n in items[:2]]
    lines.append(f"（共 {len(items)} 条）")
    return lines


def buy_push_content(conn, f: Fill, stock_name: str) -> str:
    """成交推送正文（买入）：完整基本行 + Jev 留痕明细。缺留痕/字段一律不抛异常、缺哪块就跳过哪块。"""
    lines = [fill_basic_line(f, stock_name) + f"｜金额 {f.shares * f.price:,.0f}"]
    trace = load_buy_trace(conn, f)
    if not trace:
        return "\n".join(lines)
    strategy = trace.get("strategy")
    cand = trace.get("candidate") or {}
    try:
        lines.append("▍为什么选它")
        if strategy == "board":
            ranks = cand.get("ranks") or {}
            lines.append(f"题材 {cand.get('concept')}（{cand.get('heat')} 只涨超 5%）｜"
                         f"龙头分 {cand.get('score')}（涨幅第{ranks.get('pct')}/成交额第{ranks.get('amount')}/"
                         f"5日第{ranks.get('ret5')}）")
            sealed = "已封死" if trace.get("sealed_at_snapshot") else "未封死"
            lines.append(f"扫板触发 +{cand.get('pct')}%，快照时{sealed}")
        elif strategy == "lowbuy":
            ret5 = cand.get("ret5")
            ret5_str = f"{ret5:+.2f}" if isinstance(ret5, (int, float)) else "未知"
            lines.append(f"跌幅 {cand.get('pct')}%｜近 5 日 {ret5_str}%｜"
                         f"候选排名 {cand.get('rank')}/{cand.get('of')}")
        elif accounts_mod._is_custom_strategy(strategy or ""):
            lines += [f"· {h.get('desc')}（当时 {h.get('value')}）" for h in cand.get("hits") or []]
        else:
            lines.pop()
    except Exception:
        pass
    try:
        notices = trace.get("notices")
        if notices is not None:
            lines += buy_push_notices_line(notices)
    except Exception:
        pass
    try:
        if trace.get("news") is None and accounts_mod._is_custom_strategy(strategy or ""):
            lines.append("▍Jev 消息面：未开启")       # 自定义策略可以关掉消息面判断，别谎称「无新闻」
        else:
            lines += buy_push_news_lines(trace.get("news"))
    except Exception:
        pass
    if strategy == "lowbuy":
        try:
            market = trace.get("market") or {}
            mode_label = "盘中可买" if market.get("mode") == "intraday" else "只在尾盘买"
            sent = fmt_judge_label(market.get("sentiment"), market.get("sentiment_trace"))
            over = fmt_judge_label(market.get("overseas"), market.get("overseas_trace"))
            lines.append(f"▍当时市场：{mode_label}｜情绪 {sent}｜外围 {over}")
        except Exception:
            pass
    return "\n".join(lines)


def sell_push_content(conn, f: Fill, stock_name: str) -> str:
    """成交推送正文（卖出）：完整基本行 + 持有天数 + 盈亏（找不到对应买入记录就只给基本行）。"""
    parts = [fill_basic_line(f, stock_name)]
    info = sell_pnl(conn, f)
    if info is not None:
        pct, amount, held_days = info
        parts.append(f"持有 {held_days} 天")
        parts.append(f"盈亏 {pct:+.2f}%（{amount:+,.0f} 元）")
    return "｜".join(parts)


def close_summary_content(ledger: Ledger, today: date, prices: dict, conn=None) -> str:
    """收盘小结正文：每个账户一行净值/收益率/今日成交笔数。给了 conn 就用动态账户（内置 + 今天在跑
    的自定义策略，名称用「{策略名}·主(Jev)/·影子」，收益率按各自冻结的本金算）；动态账户读不出来
    就退回内置四个，推送不能因为这一步整条丢掉。"""
    counts = Counter(f.account for f in ledger.fills_on(today))
    accounts, names, strategy = ACCOUNTS, ACCOUNT_NAMES, ACCOUNT_STRATEGY
    if conn is not None:
        try:
            accounts = accounts_mod.all_accounts(conn, today)
            names = accounts_mod.account_names(conn, today)
            strategy = accounts_mod.account_strategy(conn, today)
        except Exception:
            accounts, names, strategy = ACCOUNTS, ACCOUNT_NAMES, ACCOUNT_STRATEGY
    lines = []
    for a in accounts:
        value = ledger.equity(a, prices)
        base = accounts_mod.capital_baseline(ledger.conn, strategy.get(a, "")) if conn is not None else INITIAL_CASH
        pct = (value / base - 1) * 100
        lines.append(f"{names.get(a, a)} 净值 {value:,.2f}（{pct:+.2f}%）今日成交 {counts.get(a, 0)} 笔")
    return "\n".join(lines)
