import argparse
import os
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from jevq import db
from jevq.config import ACCOUNTS, DB_PATH, REPORT_DIR
from jevq.data import akdata, closes, tencent
from jevq.data.notice_store import sync_notices
from jevq.engine import Engine
from jevq.judge.jev import JevClient
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge
from jevq.judge.notices import NoticeJudge
from jevq.ledger import Ledger
from jevq.report import write_report
from jevq.runlock import hold_run_lock

SESSIONS = [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))]
REPORT_AT = time(15, 5)
NOTICE_RESYNC_AT = time(13, 0)


def in_session(t: time) -> bool:
    return any(a <= t < b for a, b in SESSIONS)


def now() -> datetime:
    """唯一的时钟入口，测试里替换它来模拟一整天。"""
    return datetime.now()


def wait_until(t: time, stop: threading.Event) -> bool:
    """等到时刻 t；被 stop 打断返回 False。"""
    while now().time() < t:
        if stop.wait(5):
            return False
    return True


def seconds_to_next_minute() -> float:
    t = now()
    nxt = (t + timedelta(minutes=1)).replace(second=2, microsecond=0)
    return max(1.0, (nxt - t).total_seconds())


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def held_codes(ledger: Ledger) -> list[str]:
    return sorted({p.code for a in ACCOUNTS for p in ledger.positions(a)})


def run_day(once: bool = False, stop: threading.Event | None = None,
            log: Callable[[str], None] = print, state: dict | None = None) -> Path | None:
    """跑一个交易日。stop 被置位时尽快退出（不写报告）；state 供桌面面板读取运行状态。

    原则（spec §7）：出问题宁可不买，但不能卖不掉——盘前任何一步失败都降级为只卖模式，
    盘中单轮出错只记日志、下一分钟继续。
    """
    stop = stop or threading.Event()
    state = state if state is not None else {}
    with hold_run_lock(Path(DB_PATH).parent / "runner.lock"):
        conn = db.connect(DB_PATH)
        try:
            return _run_day(conn, once, stop, log, state)
        finally:
            conn.close()


def _run_day(conn, once: bool, stop: threading.Event, log: Callable[[str], None],
             state: dict) -> Path | None:
    ledger = Ledger(conn)
    sell_only = False
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        if not held_codes(ledger):
            raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")
        log("缺少环境变量 TYPESAFE_API_KEY，但有持仓需要卖出")
        sell_only = True

    today = now().date()
    calendar_fallback = False
    try:
        days = akdata.trade_days()
        trading = today in days
        prev_day = max(d for d in days if d < today)
    except Exception as e:
        log(f"交易日历获取失败：{e!r}；按周一至周五判断")
        days, trading, prev_day = None, today.weekday() < 5, prev_weekday(today)
        sell_only = True
        calendar_fallback = True
    if not trading and not once:
        log(f"{today} 非交易日，退出")
        return None

    late = not once and now().time() >= REPORT_AT
    if late and (Path(REPORT_DIR) / f"{today.isoformat()}.md").exists():
        log("今日报告已生成，跳过")
        return None

    jev = JevClient(key or "", conn=conn)
    market = MarketJudge(jev)

    universe, ret5 = {}, {}
    if late:
        log("已过收盘，直接生成复盘报告")
        try:
            universe = akdata.load_universe(today, days)
        except Exception as e:
            log(f"晚启动股票池加载失败，收盘价只存持仓：{e!r}")
    elif not sell_only:
        state["phase"] = "准备股票池"
        try:
            universe = akdata.load_universe(today, days)
            log(f"股票池 {len(universe)} 只")
            ret5 = closes.ret5_from_db(conn, universe, today)
            log(f"5 日涨幅 {len(ret5)} 只")
            if universe and len(ret5) / len(universe) < 0.8:
                missing = [c for c in universe if c not in ret5]
                deadline = (now() + timedelta(minutes=10)) if once \
                    else datetime.combine(today, time(9, 25))
                filled = closes.backfill_closes(conn, missing, today, tencent.fetch_kline,
                                                deadline=deadline, log=log)
                log(f"5 日涨幅补数 {filled} 只")
                ret5 = closes.ret5_from_db(conn, universe, today)
            if universe and len(ret5) / len(universe) < 0.8:
                n_ok = len(ret5)
                ret5 = {}
                log(f"5 日涨幅覆盖不足（{n_ok}/{len(universe)}），今日只卖不买")
                state["warning"] = "5 日涨幅数据不足，今日只卖不买"
        except Exception as e:
            log(f"股票池准备失败：{e!r}")
            universe, ret5 = {}, {}
            sell_only = True
    if sell_only and not late:
        log("只卖模式：今日不开新仓")
    state["names"] = dict(universe)

    state["phase"] = "等待 09:15"
    if not once and not late and not wait_until(time(9, 15), stop):
        return None

    overseas = None
    if key and not late:
        try:
            overseas_pcts = tencent.fetch_overseas()
            overseas = market.overseas(overseas_pcts)
            log(f"外围 {overseas_pcts} → {overseas}")
        except Exception as e:
            log(f"外围失败：{e!r}")
            overseas = None
    state["overseas"] = overseas

    if not late:
        state["phase"] = "同步公告"
        _sync_notices(conn, today, log)

    engine = Engine(conn, ledger, NoticeJudge(conn, jev), NewsJudge(conn, jev), market,
                    tencent.fetch_quotes, akdata.fetch_pool_stats, universe, ret5, prev_day, overseas,
                    overseas_trace=market.overseas_trace)
    if calendar_fallback:
        engine.stale_before = None
        log("交易日历不可用：停用「隔夜超期」规则")

    if once:
        fills = _safe_tick(engine, conn, now(), log)
        state.update(sentiment=engine.sentiment_label, prices={c: q.price for c, q in engine.last_quotes.items()})
        log(f"情绪 {engine.sentiment_label}，成交 {fills}")
        return None

    resynced = False
    while now().time() < REPORT_AT:
        t = now()
        if not resynced and t.time() >= NOTICE_RESYNC_AT:
            resynced = True
            _sync_notices(conn, today, log)
        if in_session(t.time()):
            state["phase"] = "盘中"
            for f in _safe_tick(engine, conn, t, log):
                log(f"{f.ts:%H:%M} {f.account} {'买' if f.side == 'buy' else '卖'} {f.code} "
                    f"{f.price} x{f.shares} {f.reason}")
            state.update(sentiment=engine.sentiment_label,
                         prices={c: q.price for c, q in engine.last_quotes.items()})
        else:
            state["phase"] = "休市等待"
        if stop.wait(seconds_to_next_minute()):
            log("收到停止指令，未生成当日报告")
            return None

    prices = {c: q.price for c, q in engine.last_quotes.items()}
    names = dict(universe)
    held = held_codes(ledger)
    close_codes = sorted(set(universe) | set(held))
    if close_codes:
        try:
            quotes = tencent.fetch_quotes(close_codes)
            prices.update({c: q.price for c, q in quotes.items()})
            names = {**{c: q.name for c, q in quotes.items()}, **names}
            today_prices = {c: q.price for c, q in quotes.items() if q.ts.date() == today}
            saved = closes.save_closes(conn, today, today_prices)
            log(f"收盘价存库 {saved} 条")
        except Exception as e:
            log(f"收盘价刷新失败，沿用最后快照：{e!r}")
    try:
        path = write_report(conn, ledger, today, prices, overseas, engine.sentiment_label,
                            out_dir=REPORT_DIR, names=names)
    except Exception as e:
        log(f"复盘报告生成失败：{e!r}")
        return None
    finally:
        state["phase"] = "已收盘"
    log(f"复盘报告 {path}；" + "，".join(f"{a} {ledger.equity(a, prices):,.2f}" for a in ACCOUNTS))
    return path


def _safe_tick(engine: Engine, conn, t: datetime, log: Callable[[str], None]) -> list:
    """单轮出错只记日志；回滚这一轮没提交的半截写入，下一分钟照常卖。"""
    try:
        return engine.tick(t)
    except Exception as e:
        conn.rollback()
        log(f"本轮出错：{e!r}")
        return []


def _sync_notices(conn, today: date, log: Callable[[str], None]):
    try:
        log(f"公告同步 {sync_notices(conn, today)} 条")
    except Exception as e:
        log(f"公告同步失败：{e!r}")


def run_backfill(log: Callable[[str], None] = print) -> None:
    """只补收盘价历史，不交易、不需要 Jev 凭证。仍拿 runner 锁，避免和交易进程同时写库。"""
    with hold_run_lock(Path(DB_PATH).parent / "runner.lock"):
        conn = db.connect(DB_PATH)
        try:
            today = now().date()
            days = akdata.trade_days()
            universe = akdata.load_universe(today, days)
            log(f"股票池 {len(universe)} 只")
            filled = closes.backfill_closes(conn, sorted(universe), today, tencent.fetch_kline, log=log)
            ret5 = closes.ret5_from_db(conn, universe, today)
            log(f"补数 {filled} 只；5 日涨幅覆盖 {len(ret5)}/{len(universe)}")
        finally:
            conn.close()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="立即跑一轮后退出（冒烟测试）")
    p.add_argument("--backfill", action="store_true", help="只补齐收盘价历史后退出，不交易")
    args = p.parse_args(argv)
    try:
        if args.backfill:
            run_backfill()
        else:
            run_day(once=args.once)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
