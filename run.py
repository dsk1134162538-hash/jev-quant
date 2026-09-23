import argparse
import os
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from jevq import db
from jevq.config import ACCOUNTS, DB_PATH, REPORT_DIR
from jevq.data import akdata, board_events, closes, concepts, tencent
from jevq.data.notice_store import sync_notices
from jevq.engine import Engine
from jevq.judge.jev import JevClient
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge
from jevq.judge.notices import NoticeJudge
from jevq.ledger import Ledger
from jevq.report import write_report
from jevq.rules.board import in_scan_window
from jevq.runlock import hold_run_lock
from jevq.strategies.board import BoardStrategy

SESSIONS = [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))]
REPORT_AT = time(15, 5)
NOTICE_RESYNC_AT = time(13, 0)
FAST_INTERVAL = 3.0     # 扫首板节拍：观察池非空时每隔这么多秒扫一次


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


def _seconds_to_boundary(t: datetime) -> float:
    nxt = (t + timedelta(minutes=1)).replace(second=2, microsecond=0)
    return max(1.0, (nxt - t).total_seconds())


def seconds_to_next_minute() -> float:
    return _seconds_to_boundary(now())


def _remaining_this_minute(t: datetime) -> float:
    return max(1.0, 60 - t.second - t.microsecond / 1e6)


def next_wake(now: datetime, has_watch: bool, fast_interval: float = FAST_INTERVAL) -> float:
    """算这一刻该等多久再醒：没有观察池就直接睡到下一分钟整点；有就按 fast_interval 秒节拍醒，
    但不会睡过头，最多睡到下一分钟边界（纯函数，不摸时钟，方便单测）。"""
    remain = _remaining_this_minute(now)
    return min(fast_interval, remain) if has_watch else remain


def _tick_wait(seconds: float, stop: threading.Event) -> bool:
    """扫板 3 秒节拍专用的等待钩子，和 seconds_to_next_minute 分开，方便测试用假时钟顶替。"""
    return stop.wait(seconds)


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def ran_on(conn, day: date) -> bool:
    """那天 runner 有没有跑过盘中：turnover 表每分钟取数成功都会写一行，有一行就算跑过。"""
    return conn.execute("SELECT 1 FROM turnover WHERE ts LIKE ? LIMIT 1",
                        (f"{day.isoformat()}%",)).fetchone() is not None


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

    # 题材刷新/读取放在盘前准备阶段（09:15 等待之前），并给一个硬 deadline：
    # 放在等待之后做会顶着 09:30 开盘时间，之前这里没有 deadline 也没接 stop，
    # 一旦刷新变慢就会把第一笔 09:30 的 tick 往后拖。晚启动（late）不选股票、
    # 不判断，也就不需要题材数据，直接跳过，BoardStrategy 也不构造。
    concept_map: dict[str, list[str]] = {}
    prev_prev_closes: dict = {}
    board_enabled = False
    if not late:
        state["phase"] = "刷新题材"
        try:
            if concepts.needs_refresh(conn, today):
                deadline = datetime.combine(today, time(9, 25))
                concepts.refresh_concepts(conn, today, deadline=deadline, stop=stop, log=log)
        except Exception as e:
            log(f"题材刷新失败：{e!r}")
        try:
            concept_map = concepts.load_concepts(conn)
        except Exception as e:
            log(f"题材读取失败，扫首板今日只卖不买：{e!r}")
            concept_map = {}
        if not concept_map:
            _warn(state, log, "题材数据缺失，扫首板今日只卖不买")
        board_enabled = bool(concept_map) and bool(ret5)   # 扫首板排名要用 ret5，覆盖率不够也只卖不买
        try:
            prev_prev_day = max((d for d in days if d < prev_day), default=None) if days else None
            prev_prev_closes = closes.prev_closes_from_db(conn, universe, today, prev_day, prev_prev_day)
            if universe and not prev_prev_closes:
                board_enabled = False
                _warn(state, log, "昨日收盘价缺失，扫首板今日只卖不买")
        except Exception as e:
            log(f"昨收/前日收读取失败，扫首板今日只卖不买：{e!r}")
            prev_prev_closes = {}
            board_enabled = False

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

    notice_judge, news_judge = NoticeJudge(conn, jev), NewsJudge(conn, jev)
    engine = Engine(conn, ledger, notice_judge, news_judge, market,
                    tencent.fetch_quotes, akdata.fetch_pool_stats, universe, ret5, prev_day, overseas,
                    overseas_trace=market.overseas_trace)
    board = None
    if not late:
        board = BoardStrategy(conn, ledger, notice_judge, news_judge, tencent.fetch_quotes, universe, ret5,
                              concept_map, prev_prev_closes, prev_day, enabled=board_enabled)
        # 「隔夜超期」只管 runner 错过了上一交易日的情况：昨天跑过，说明持仓昨天已经按规则处理过
        # （封死涨停隔夜的票就是规则允许留下的），今天照常按同一套规则判断，不在开盘一刀切卖掉。
        try:
            if ran_on(conn, prev_day):
                board.stale_before = None
        except Exception as e:
            log(f"读取昨日运行记录失败，扫首板保留「隔夜超期」规则：{e!r}")
    if calendar_fallback:
        engine.stale_before = None
        if board is not None:
            board.stale_before = None
        log("交易日历不可用：停用「隔夜超期」规则")

    if once:
        t = now()
        fills = _safe_tick(engine, conn, t, log)
        board_fills = _safe_board_minute(board, _fresh_quotes(engine, t), t, conn, log) if board is not None else []
        state.update(sentiment=engine.sentiment_label, prices={c: q.price for c, q in engine.last_quotes.items()})
        if board is not None:
            state["board_watch"] = _board_watch_state(board, state["names"])
        log(f"情绪 {engine.sentiment_label}，成交 {fills + board_fills}")
        return None

    resynced = False
    last_tick_minute = None
    while now().time() < REPORT_AT:
        t = now()
        if not resynced and t.time() >= NOTICE_RESYNC_AT:
            resynced = True
            _sync_notices(conn, today, log)
        minute_key = t.replace(second=0, microsecond=0)
        if in_session(t.time()) and minute_key != last_tick_minute:
            # 防重复的安全网：正常情况下 _fast_scan 固定在 :02 边界交回控制权，不会撞到同一分钟
            # 两次；万一出于什么原因还是撞上了，这里挡一道，不重复吃这一分钟的 tick。
            last_tick_minute = minute_key
            state["phase"] = "盘中"
            _log_fills(_safe_tick(engine, conn, t, log), log)
            _log_fills(_safe_board_minute(board, _fresh_quotes(engine, t), t, conn, log), log)
            state.update(sentiment=engine.sentiment_label,
                         prices={c: q.price for c, q in engine.last_quotes.items()})
            state["board_watch"] = _board_watch_state(board, state["names"])
        elif not in_session(t.time()):
            state["phase"] = "休市等待"

        if _should_fast_scan(board, now().time()):
            stopped = _fast_scan(board, stop, log, state)
        else:
            stopped = stop.wait(seconds_to_next_minute())
        if stopped:
            log("收到停止指令，未生成当日报告")
            return None

    prices = {c: q.price for c, q in engine.last_quotes.items()}
    names = dict(universe)
    held = held_codes(ledger)
    close_codes = sorted(set(universe) | set(held))
    quotes: dict = {}
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
            quotes = {}

    # 封板时段统计（spec §4.5）：只验证经验，不参与交易，任何一步失败都不能挡住报告写入。
    try:
        filled = board_events.fill_next_day(conn, prev_day, quotes, today)
        log(f"封板统计回填 {filled} 条")
    except Exception as e:
        log(f"封板统计回填失败：{e!r}")
    try:
        zt_rows = akdata.fetch_zt_pool(today).to_dict("records")
        recorded = board_events.record_day(conn, today, zt_rows, concept_map)
        log(f"封板统计记录 {recorded} 只首板")
    except Exception as e:
        log(f"封板统计记录失败：{e!r}")
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


def _warn(state: dict, log: Callable[[str], None], msg: str) -> None:
    """记日志，并追加到面板顶部的告警（多条用「；」连起来）。"""
    log(msg)
    state["warning"] = f"{state['warning']}；{msg}" if state.get("warning") else msg


def _safe_tick(engine: Engine, conn, t: datetime, log: Callable[[str], None]) -> list:
    """单轮出错只记日志；回滚这一轮没提交的半截写入，下一分钟照常卖。"""
    try:
        return engine.tick(t)
    except Exception as e:
        conn.rollback()
        log(f"本轮出错：{e!r}")
        return []


def _fresh_quotes(engine: Engine, t: datetime) -> dict:
    """这一分钟低吸 tick 取数成功才把行情交给扫首板；失败时 last_quotes 还是上一分钟的旧快照，
    拿它去卖会用旧价成交，所以给空字典，扫首板这一分钟既不卖也不动观察池。"""
    if getattr(engine, "last_fetch_minute", None) == t.strftime("%Y-%m-%d %H:%M"):
        return engine.last_quotes
    return {}


def _safe_board_minute(board: BoardStrategy, quotes: dict, t: datetime, conn,
                       log: Callable[[str], None]) -> list:
    """扫首板的每分钟一轮（卖出 + 观察池维护）；出错只记日志，不影响低吸 tick 和下一分钟。"""
    try:
        return board.on_minute(quotes, t)
    except Exception as e:
        conn.rollback()
        log(f"扫首板本轮出错：{e!r}")
        return []


def _should_fast_scan(board: BoardStrategy | None, t: time) -> bool:
    """只在扫板窗口内（默认 09:30–10:30）、观察池非空时跑 3 秒快扫；窗口外买不了，照常按分钟节拍走。"""
    return (board is not None and board.has_watch() and in_session(t)
            and in_scan_window(t, board.windows))


def _fast_scan(board: BoardStrategy, stop: threading.Event, log: Callable[[str], None],
               state: dict) -> bool:
    """观察池非空时的 3 秒扫板循环。返回是否被 stop 打断。

    边界在进入时算死一次（deadline = 现在 + seconds_to_next_minute()，带着 :02 缓冲），
    循环全程只跟这个固定 deadline 比。不能在每轮重新用「现在到下一分钟还有多久」算边界——
    on_fast 只要有点真实耗时，"现在"就已经跨过分钟整点，重新算出来的又是一个新的、快满
    60 秒的余量，循环会一直续命下去，把后面好几个整分钟的 tick（低吸和扫首板的卖出都在
    里面）都吞掉。这个 bug 真实复现过：on_fast 模拟 0.3 秒耗时，一小时里 29 分钟被跳过。
    """
    t0 = now()
    deadline = t0 + timedelta(seconds=_seconds_to_boundary(t0))   # 用不带副作用的纯公式算，
    # 不走 seconds_to_next_minute()：那个是外层「没有观察池」分支专用的等待入口，测试会把它
    # 换成一个会顺手拨表的假实现，这里借用会把 deadline 算错。
    while _should_fast_scan(board, now().time()):
        remain = (deadline - now()).total_seconds()
        if remain <= 0:
            return False
        if _tick_wait(min(FAST_INTERVAL, remain), stop):
            return True
        if now() >= deadline:
            return False
        try:
            _log_fills(board.on_fast(now()), log, "%H:%M:%S")
        except Exception as e:
            log(f"扫板出错：{e!r}")
        state["board_watch"] = _board_watch_state(board, state.get("names", {}))
    return False


def _log_fills(fills, log: Callable[[str], None], fmt: str = "%H:%M") -> None:
    for f in fills:
        log(f"{f.ts.strftime(fmt)} {f.account} {'买' if f.side == 'buy' else '卖'} {f.code} "
            f"{f.price} x{f.shares} {f.reason}")


def _board_watch_state(board: BoardStrategy, names: dict) -> list[dict]:
    """给桌面面板用：观察池代码 + 名称。"""
    return [{"code": c, "name": names.get(c, "")} for c in sorted(board.watch)]


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
