import argparse
import os
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from jevq import db
from jevq.config import ACCOUNTS, DB_PATH
from jevq.data import akdata, tencent
from jevq.data.notice_store import sync_notices
from jevq.engine import Engine
from jevq.judge.jev import JevClient
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge
from jevq.judge.notices import NoticeJudge
from jevq.ledger import Ledger
from jevq.report import write_report

SESSIONS = [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))]
REPORT_AT = time(15, 5)
NOTICE_RESYNC_AT = time(13, 0)


def in_session(t: time) -> bool:
    return any(a <= t < b for a, b in SESSIONS)


def wait_until(t: time, stop: threading.Event) -> bool:
    """等到时刻 t；被 stop 打断返回 False。"""
    while datetime.now().time() < t:
        if stop.wait(5):
            return False
    return True


def seconds_to_next_minute() -> float:
    now = datetime.now()
    nxt = (now + timedelta(minutes=1)).replace(second=2, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


def run_day(once: bool = False, stop: threading.Event | None = None,
            log: Callable[[str], None] = print, state: dict | None = None) -> Path | None:
    """跑一个交易日。stop 被置位时尽快退出（不写报告）；state 供桌面面板读取运行状态。"""
    stop = stop or threading.Event()
    state = state if state is not None else {}
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")

    today = date.today()
    days = akdata.trade_days()
    if today not in days and not once:
        log(f"{today} 非交易日，退出")
        return None
    prev_day = max(d for d in days if d < today)

    conn = db.connect(DB_PATH)
    ledger = Ledger(conn)
    jev = JevClient(key, conn=conn)
    market = MarketJudge(jev)

    state["phase"] = "准备股票池"
    universe = akdata.load_universe(today, days)
    log(f"股票池 {len(universe)} 只")
    state["names"] = dict(universe)
    ret5 = tencent.fetch_5d_returns(sorted(universe), today)
    log(f"5 日涨幅 {len(ret5)} 只")

    state["phase"] = "等待 09:15"
    if not once and not wait_until(time(9, 15), stop):
        return None
    try:
        overseas_pcts = tencent.fetch_overseas()
    except Exception as e:
        log(f"外围失败: {e!r}")
        overseas_pcts = {}
    overseas = market.overseas(overseas_pcts)
    state["overseas"] = overseas
    log(f"外围 {overseas_pcts} → {overseas}")

    state["phase"] = "同步公告"
    log(f"公告同步 {sync_notices(conn, today)} 条")

    engine = Engine(conn, ledger, NoticeJudge(conn, jev), NewsJudge(conn, jev), market,
                    tencent.fetch_quotes, akdata.fetch_pool_stats, universe, ret5, prev_day, overseas,
                    overseas_trace=market.overseas_trace)

    if once:
        fills = engine.tick(datetime.now())
        state.update(sentiment=engine.sentiment_label, prices={c: q.price for c, q in engine.last_quotes.items()})
        log(f"情绪 {engine.sentiment_label}，成交 {fills}")
        return None

    resynced = False
    while datetime.now().time() < REPORT_AT:
        now = datetime.now()
        if not resynced and now.time() >= NOTICE_RESYNC_AT:
            sync_notices(conn, today)
            resynced = True
        if in_session(now.time()):
            state["phase"] = "盘中"
            for f in engine.tick(now):
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
    path = write_report(conn, ledger, today, prices, overseas, engine.sentiment_label, names=universe)
    state["phase"] = "已收盘"
    log(f"复盘报告 {path}；" + "，".join(f"{a} {ledger.equity(a, prices):,.2f}" for a in ACCOUNTS))
    return path


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="立即跑一轮后退出（冒烟测试）")
    args = p.parse_args(argv)
    try:
        run_day(once=args.once)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
