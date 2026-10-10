import argparse
import os
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic

from jevq import accounts as accounts_mod
from jevq import config, db, notify_content
from jevq.config import ACCOUNT_NAMES, DB_PATH, REPORT_DIR
from jevq.data import (akdata, board_events, candidates, closes, concepts, custom_store, hi189, news7x24, own_sectors,
                       catalyst, market_extra, sell_followups, tencent, ztpool)
from jevq.data.features import history_features
from jevq.data.notice_body import prefetch_bodies
from jevq.data.notice_store import sync_notices
from jevq.engine import Engine
from jevq.judge.jev import JevClient
from jevq.judge.market import MarketJudge
from jevq.judge.own_sectors import OwnSectorJudge
from jevq.judge.news import NewsJudge
from jevq.judge.notices import NoticeJudge
from jevq.judge.trend import TrendJudge
from jevq.judge.turn import TurnJudge
from jevq.ledger import Ledger
from jevq.live import store as live_store
from jevq.notify import Notifier, notifier_from_env
from jevq.report import write_report
from jevq.rules.board import in_scan_window
from jevq.runlock import hold_run_lock
from jevq.strategies.board import BoardStrategy
from jevq.strategies.custom import CustomStrategy
from jevq.strategies.rebound import ReboundStrategy
from jevq.strategies.lowbuy import LowbuyStrategy
from jevq.strategies.regime import RegimeStrategy

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


def held_codes(ledger: Ledger, day: date) -> list[str]:
    return sorted({p.code for a in accounts_mod.all_accounts(ledger.conn, day) for p in ledger.positions(a)})


def run_day(once: bool = False, stop: threading.Event | None = None,
            log: Callable[[str], None] = print, state: dict | None = None,
            notifier: Notifier | None = None,
            after_close: Callable[[object, date], None] | None = None) -> Path | None:
    """跑一个交易日。stop 被置位时尽快退出（不写报告）；state 供桌面面板读取运行状态。

    原则（spec §7）：出问题宁可不买，但不能卖不掉——盘前任何一步失败都降级为只卖模式，
    盘中单轮出错只记日志、下一分钟继续。

    after_close(conn, today)：报告写完后的可选回调（daemon 用它触发每日导出）。只在报告
    真正写出时调用；调用方自己决定异常怎么处理——这里不吞它的异常，因为这一步应该看得见。
    """
    stop = stop or threading.Event()
    state = state if state is not None else {}
    notifier = notifier if notifier is not None else notifier_from_env()
    with hold_run_lock(Path(DB_PATH).parent / "runner.lock"):
        conn = db.connect(DB_PATH)
        try:
            return _run_day(conn, once, stop, log, state, notifier, after_close)
        except Exception as e:
            notifier.once("run_day_exception", "⚠️ 模拟盘提醒", f"运行异常：{e!r}")
            raise
        finally:
            conn.close()


def _run_day(conn, once: bool, stop: threading.Event, log: Callable[[str], None],
             state: dict, notifier: Notifier,
             after_close: Callable[[object, date], None] | None = None) -> Path | None:
    ledger = Ledger(conn)
    today = now().date()
    acct_names = accounts_mod.account_names(conn, today)
    sell_only = False
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        if not held_codes(ledger, today):
            raise RuntimeError("缺少环境变量 TYPESAFE_API_KEY")
        log("缺少环境变量 TYPESAFE_API_KEY，但有持仓需要卖出")
        sell_only = True

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
    if trading:
        live_store.set_state(conn, "trading_day", today.isoformat())

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
            # 后台某天没跑 → 那天整天没有收盘价，历史类指标会把缺口两边当成相邻交易日悄悄算错，要按日历补
            gaps = closes.incomplete_trade_days(conn, days, today)
            low_coverage = bool(universe) and len(ret5) / len(universe) < 0.8
            if universe and (low_coverage or gaps):
                if gaps:
                    log(f"收盘价缺口 {len(gaps)} 天：{'、'.join(gaps)}")
                codes = sorted(universe) if gaps else [c for c in universe if c not in ret5]
                # 只为补缺口（覆盖率够）时 09:05 收手，给题材刷新、公告同步留出时间；缺口没补完的明天接着补
                deadline = (now() + timedelta(minutes=10)) if once \
                    else datetime.combine(today, time(9, 25) if low_coverage else time(9, 5))
                filled = closes.backfill_closes(conn, codes, today, akdata.fetch_daily_sina,
                                                deadline=deadline, log=log, trade_days=days)
                log(f"收盘价补数 {filled} 只")
                ret5 = closes.ret5_from_db(conn, universe, today)
                left = closes.incomplete_trade_days(conn, days, today) if gaps else []
                if left:
                    _warn(state, log, notifier, f"收盘价有 {len(left)} 天不完整，历史类条件可能不准；"
                                                "收盘后可运行 python run.py --backfill 补齐")
            if universe and len(ret5) / len(universe) < 0.8:
                n_ok = len(ret5)
                ret5 = {}
                log(f"5 日涨幅覆盖不足（{n_ok}/{len(universe)}），今日只卖不买")
                _set_warning(state, notifier, "5 日涨幅数据不足，今日只卖不买")
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
    recent_limit_ups: set[str] = set()
    board_enabled = False
    if not late:
        state["phase"] = "刷新题材"
        try:
            if concepts.needs_refresh(conn, today):
                deadline = (now() + timedelta(minutes=10)) if once \
                    else datetime.combine(today, time(9, 25))
                concepts.refresh_concepts(conn, today, deadline=deadline, stop=stop, log=log)
        except Exception as e:
            log(f"题材刷新失败：{e!r}")
        try:
            concept_map = concepts.load_concepts(conn)
        except Exception as e:
            log(f"题材读取失败，扫首板今日只卖不买：{e!r}")
            concept_map = {}
        if not concept_map:
            _warn(state, log, notifier, "题材数据缺失，扫首板今日只卖不买")
        board_enabled = bool(concept_map) and bool(ret5)   # 扫首板排名要用 ret5，覆盖率不够也只卖不买
        try:
            prev_prev_day = max((d for d in days if d < prev_day), default=None) if days else None
            prev_prev_closes = closes.prev_closes_from_db(conn, universe, today, prev_day, prev_prev_day)
            if universe and not prev_prev_closes:
                board_enabled = False
                _warn(state, log, notifier, "昨日收盘价缺失，扫首板今日只卖不买")
        except Exception as e:
            log(f"昨收/前日收读取失败，扫首板今日只卖不买：{e!r}")
            prev_prev_closes = {}
            board_enabled = False
        try:
            recent_limit_ups = closes.recent_limit_up_codes(conn, universe, today)
            log(f"近 10 日涨停过的股票 {len(recent_limit_ups)} 只，扫首板排除")
        except Exception as e:
            log(f"近 10 日涨停查询失败，扫首板本项过滤跳过：{e!r}")
            recent_limit_ups = set()

    if config.CATALYST_ENABLED and not late:      # 隔夜消息催化：只记录，不参与交易
        cutoff = datetime.combine(today, config.CATALYST_DEADLINE)
        if now() >= cutoff:
            log(f"隔夜消息催化跳过：已过 {config.CATALYST_DEADLINE:%H:%M}（盘中启动不让交易等它）")
        else:
            state["phase"] = "抓隔夜新闻"
            try:
                budget = min(config.RSS_BUDGET, (cutoff - now()).total_seconds())
                got = catalyst.run(conn, today, datetime.combine(prev_day, time(15, 0)), now(), budget=budget)
                log(f"隔夜消息催化：{got}")
            except Exception as e:
                log(f"隔夜消息催化失败：{e!r}")

    state["phase"] = "等待 09:15"
    if not once and not late and not wait_until(time(9, 15), stop):
        return None

    overseas = None
    if key and not late:
        try:
            overseas_pcts = tencent.fetch_overseas()
            state["overseas_pcts"] = overseas_pcts
            overseas = market.overseas(overseas_pcts)
            log(f"外围 {overseas_pcts} → {overseas}")
        except Exception as e:
            log(f"外围失败：{e!r}")
            overseas = None
    state["overseas"] = overseas

    if not late:
        state["phase"] = "同步公告"
        _sync_notices(conn, today, log)
        _prefetch_bodies(conn, today, universe, log, config.PREFETCH_BUDGET)

    notice_judge, news_judge = NoticeJudge(conn, jev), NewsJudge(conn, jev)
    # 账户集合当天固定（自定义策略新版本次日才生效）：盘前算一次，引擎取行情时连自定义账户的持仓一起拉，
    # 否则不在股票池里的自定义持仓（或只卖日股票池为空时的全部自定义持仓）永远拿不到行情、卖不掉。
    try:
        day_accounts = accounts_mod.all_accounts(conn, today)
    except Exception as e:
        log(f"自定义账户读取失败，只拉内置账户持仓行情：{e!r}")
        day_accounts = config.builtin_accounts()
    flash = news7x24.Flash(log=log)
    engine = Engine(conn, ledger, market, tencent.fetch_quotes, akdata.fetch_pool_stats, universe, prev_day,
                    overseas, accounts_provider=lambda: day_accounts,
                    articles_provider=None if late else lambda t: hi189.refresh(conn, today, prev_day, t))
    # 情绪拐点抄底 + 7x24 快讯：构造后再挂上（测试里的假引擎没有这些参数）
    engine.turn_judge = None if (late or sell_only) else TurnJudge(jev, flash)
    engine.flash = flash
    engine.overseas_pcts = state.get("overseas_pcts") or {}
    if config.ZT_EXTRA_ENABLED and not late:     # 昨日涨停今日表现 → 情绪；涨停原因题材 → 主线点名
        engine.zt_extra = ztpool.safe_yesterday_stats
        engine.zt_reasons = ztpool.safe_reason_sectors
    board = None
    if not late:
        board = BoardStrategy(conn, ledger, notice_judge, news_judge, tencent.fetch_quotes, universe, ret5,
                              concept_map, prev_prev_closes, prev_day, enabled=board_enabled,
                              recent_limit_ups=recent_limit_ups,
                              sectors_provider=lambda: _sectors_of(engine),
                              fetch_day_minutes=tencent.fetch_day_minutes)
        # 「隔夜超期」只管 runner 错过了上一交易日的情况：昨天跑过，说明持仓昨天已经按规则处理过
        # （封死涨停隔夜的票就是规则允许留下的），今天照常按同一套规则判断，不在开盘一刀切卖掉。
        try:
            if ran_on(conn, prev_day):
                board.stale_before = None
        except Exception as e:
            log(f"读取昨日运行记录失败，扫首板保留「隔夜超期」规则：{e!r}")
    rebound = None if late else _load_rebound(conn, ledger, today, universe, ret5, sell_only, notice_judge,
                                              news_judge, engine, prev_day, log, trade_days=days,
                                              trend_judge=TrendJudge(jev))
    if rebound is not None:
        rebound.turn_provider = lambda: bool(getattr(engine, "turn_on", False))
    lowbuy = None if late else _load_lowbuy(conn, ledger, universe, ret5, sell_only, notice_judge, news_judge,
                                            engine, prev_day, log, concept_map=concept_map,
                                            features=getattr(rebound, "features", None), today=today)
    # 情绪轮动（实盘跟它）：只跟各策略主账户的成交，晚启动不跑；只卖日照样跟卖
    regime = None if late else RegimeStrategy(conn, ledger, lambda: engine.sentiment_label,
                                              lambda: bool(getattr(engine, "turn_on", False)),
                                              can_buy=not sell_only)
    if calendar_fallback:
        if board is not None:
            board.stale_before = None
        log("交易日历不可用：停用「隔夜超期」规则")

    customs = [] if late else _load_customs(
        conn, ledger, today, universe, ret5, sell_only, notice_judge, news_judge, concept_map,
        lambda: {"sentiment": engine.sentiment_label, "overseas": overseas}, prev_day, log, trade_days=days)

    if once:
        t = now()
        fills = _run_minute(engine, board, customs, t, conn, log, notifier, state["names"], acct_names,
                            log_each=False, rebound=rebound, lowbuy=lowbuy, regime=regime)
        state.update(sentiment=engine.sentiment_label, prices={c: q.price for c, q in engine.last_quotes.items()})
        if board is not None:
            state["board_watch"] = _board_watch_state(board, state["names"])
        log(f"情绪 {engine.sentiment_label}，成交 {fills}")
        return None

    resynced = False
    last_tick_minute = None
    after_sell = sell_followups.AfterSellTracker()     # 卖飞统计：盘中跟踪每笔卖出之后的最高价
    own_intraday = _own_intraday_recorder(conn, key, universe, ret5, log, sell_only or late)
    while now().time() < REPORT_AT:
        t = now()
        if not resynced and t.time() >= NOTICE_RESYNC_AT:
            resynced = True
            _sync_notices(conn, today, log)
            _prefetch_bodies(conn, today, universe, log, config.PREFETCH_BUDGET_MIDDAY)
        minute_key = t.replace(second=0, microsecond=0)
        if in_session(t.time()) and minute_key != last_tick_minute:
            # 防重复的安全网：正常情况下 _fast_scan 固定在 :02 边界交回控制权，不会撞到同一分钟
            # 两次；万一出于什么原因还是撞上了，这里挡一道，不重复吃这一分钟的 tick。
            last_tick_minute = minute_key
            state["phase"] = "盘中"
            _run_minute(engine, board, customs, t, conn, log, notifier, state["names"], acct_names,
                        rebound=rebound, lowbuy=lowbuy, regime=regime)
            try:        # 卖飞统计：只记录，失败不影响交易
                after_sell.update(conn, today, _fresh_quotes(engine, t))
            except Exception as e:
                log(f"卖飞跟踪失败：{e!r}")
            if getattr(engine, "turn_on", False) and getattr(engine, "turn_detail", None):
                notifier.once("turn", "💡 情绪拐点：开始抄底",
                              f"{engine.turn_detail.get('text', '')}\nJev 判修复拐点 {engine.turn_detail.get('prob')}；"
                              "超跌反弹今天偏弱/冰点也盘中买，实盘跟")
            if own_intraday is not None:
                try:    # 盘中板块情绪：后台线程记录，这里只触发，不等
                    own_intraday.tick(t, _fresh_quotes(engine, t))
                except Exception as e:
                    log(f"盘中板块触发失败：{e!r}")
            if getattr(engine, "halted", False):
                notifier.once("halted", "⚠️ 模拟盘提醒", "行情连续 3 次获取失败；取不到行情的这几分钟各策略都不交易，恢复后照常")
            state.update(sentiment=engine.sentiment_label,
                         prices={c: q.price for c, q in engine.last_quotes.items()})
            state["board_watch"] = _board_watch_state(board, state["names"])
        elif not in_session(t.time()):
            state["phase"] = "休市等待"

        if _should_fast_scan(board, now().time()):
            stopped = _fast_scan(board, stop, log, state, notifier, conn, acct_names,
                                 follow=None if regime is None else (
                                     lambda fills, t: _follow(regime, fills, engine, t, conn, log, notifier,
                                                              state.get("names", {}), acct_names)))
        else:
            stopped = stop.wait(seconds_to_next_minute())
        if stopped:
            log("收到停止指令，未生成当日报告")
            return None

    prices = {c: q.price for c, q in engine.last_quotes.items()}
    names = dict(universe)
    held = held_codes(ledger, today)
    try:
        sold = sell_followups.codes_to_quote(conn, today, prev_day)
    except Exception as e:
        log(f"卖飞统计取代码失败：{e!r}")
        sold = set()
    close_codes = sorted(set(universe) | set(held) | sold)
    quotes: dict = {}
    if close_codes:
        try:
            quotes = tencent.fetch_quotes(close_codes)
            prices.update({c: q.price for c, q in quotes.items()})
            names = {**{c: q.name for c, q in quotes.items()}, **names}
            today_quotes = {c: q for c, q in quotes.items() if q.ts.date() == today}
            saved = closes.save_closes(conn, today, {c: q.price for c, q in today_quotes.items()},
                                       volumes={c: q.volume for c, q in today_quotes.items()})
            log(f"收盘价存库 {saved} 条")
        except Exception as e:
            log(f"收盘价刷新失败，沿用最后快照：{e!r}")
            quotes = {}

    # 卖飞统计：只记录，不参与交易，失败不挡报告。晚启动（没跑盘中）拿不到卖后最高，只记收盘。
    try:
        filled = sell_followups.fill_next_day(conn, prev_day, quotes, today)
        recorded = sell_followups.record_day(conn, today, quotes, after_sell.highs)
        log(f"卖飞统计：回填 {filled} 笔，记录 {recorded} 笔")
    except Exception as e:
        log(f"卖飞统计失败：{e!r}")
    # 封板时段统计（spec §4.5）：只验证经验，不参与交易，任何一步失败都不能挡住报告写入。
    try:
        filled = board_events.fill_next_day(conn, prev_day, quotes, today)
        log(f"封板统计回填 {filled} 条")
    except Exception as e:
        log(f"封板统计回填失败：{e!r}")
    try:
        zt_rows = akdata.fetch_zt_pool(today).to_dict("records")
        recorded = board_events.record_day(conn, today, zt_rows, concept_map, log=log)
        log(f"封板统计记录 {recorded} 只首板")
    except Exception as e:
        log(f"封板统计记录失败：{e!r}")
    own_judge = OwnSectorJudge(jev)
    own_quotes = {c: q for c, q in quotes.items() if c in universe and q.ts.date() == today}
    if own_quotes:      # 自建板块（只记录）：日快照要赶在报告之前；失败只记日志
        snaps = own_sectors.snapshot_step(conn, today, universe, own_quotes, ret5, own_judge, log=log)
        log(f"自建板块日快照 {snaps} 个板块")
    if config.MARKET_EXTRA_ENABLED:     # 人气榜 / 热榜 / 概念资金流 / 龙虎榜：只记录，赶在报告之前
        try:
            log(f"人气与资金快照：{market_extra.snapshot(conn, today, lhb_day=prev_day)}")
        except Exception as e:
            log(f"人气与资金快照失败：{e!r}")
    try:
        path = write_report(conn, ledger, today, prices, overseas, engine.sentiment_label,
                            out_dir=REPORT_DIR, names=names, articles=hi189.cached(conn, today))
    except Exception as e:
        log(f"复盘报告生成失败：{e!r}")
        return None
    finally:
        state["phase"] = "已收盘"
    report_accounts = accounts_mod.all_accounts(conn, today)
    log(f"复盘报告 {path}；" + "，".join(f"{a} {ledger.equity(a, prices):,.2f}" for a in report_accounts))
    _notify_close_summary(notifier, ledger, today, prices, log, conn)
    if universe:        # 自建板块（只记录）：报告之后慢慢刷新成员（有时间预算），下个交易日的快照用
        stats = own_sectors.refresh_step(conn, today, universe, own_judge, budget=config.OWN_SECTORS_BUDGET, log=log)
        log(f"自建板块成员刷新：{stats}")
    try:
        custom_store.purge_when_flat(conn, config.CUSTOM_PURGE_WHEN_FLAT, log)
    except Exception as e:
        conn.rollback()
        log(f"自定义策略清理失败：{e!r}")
    if after_close is not None:
        after_close(conn, today)
    return path


def _sectors_of(engine) -> list | None:
    """主线判断的「点名」板块：文章 + 涨停原因（测试里的假引擎只有 sectors）。"""
    if hasattr(engine, "all_sectors"):
        return engine.all_sectors()
    return getattr(engine, "sectors", None)


def _notify_close_summary(notifier: Notifier, ledger: Ledger, today: date, prices: dict,
                          log: Callable[[str], None], conn=None) -> None:
    """收盘小结：每个账户（内置 + 今天在跑的自定义）一行净值/收益率/今日成交笔数。
    任何问题只记日志，不影响报告已写入的事实。"""
    try:
        notifier.send(f"收盘 {today.isoformat()}", notify_content.close_summary_content(ledger, today, prices, conn))
    except Exception as e:
        log(f"收盘小结推送失败：{e!r}")


def _notify_fills(notifier: Notifier, conn, fills: list, names: dict,
                  log: Callable[[str], None], account_names: dict | None = None) -> None:
    """成交推送：只推各策略的主账户（board_main / rebound_main / c{n}_main），影子账户不推。
    任何问题只记日志，不影响交易。正文明细的构建都在 jevq.notify_content 里，这里只是薄薄一层：
    拼标题、挑账户、兜底记日志。"""
    account_names = account_names or ACCOUNT_NAMES
    for f in fills:
        if not f.account.endswith("_main"):
            continue
        try:
            stock_name = names.get(f.code, f.code)
            account_label = account_names.get(f.account, f.account)
            if f.side == "buy":
                title = f"【{account_label} 买入】{stock_name} @{f.price}"
                content = notify_content.buy_push_content(conn, f, stock_name)
            else:
                title = f"【{account_label} 卖出】{stock_name} @{f.price}"
                content = notify_content.sell_push_content(conn, f, stock_name)
            notifier.send(title, content)
        except Exception as e:
            log(f"成交推送失败：{e!r}")


def _set_warning(state: dict, notifier: Notifier, msg: str) -> None:
    """追加到面板顶部的告警（多条用「；」连起来），并推一次微信（同一条内容当天只推一次）。"""
    state["warning"] = f"{state['warning']}；{msg}" if state.get("warning") else msg
    notifier.once(msg, "⚠️ 模拟盘提醒", msg)


def _warn(state: dict, log: Callable[[str], None], notifier: Notifier, msg: str) -> None:
    """记日志，并追加到面板顶部的告警。"""
    log(msg)
    _set_warning(state, notifier, msg)


def _run_minute(engine: Engine, board: BoardStrategy | None, customs: list, t: datetime, conn,
                log: Callable[[str], None], notifier: Notifier, names: dict, acct_names: dict | None,
                log_each: bool = True, rebound: ReboundStrategy | None = None,
                lowbuy: LowbuyStrategy | None = None, regime: RegimeStrategy | None = None) -> list:
    """一分钟一轮：引擎先取行情，再让所有策略卖完（扫首板 → 超跌反弹 → 各自定义，全程不碰 Jev），推送卖出，
    再按「扫首板 → 引擎刷新情绪 → 超跌反弹 → 各自定义」跑买入阶段（超跌反弹、自定义读的是这一分钟刷新后的情绪）。
    买入阶段共用一个 Jev 时间预算（TICK_JEV_BUDGET，卖出之后才开始计时；情绪刷新不受它限制）：过了时限各策略
    不再判断新候选，没判到的下一分钟再判——慢 Jev 永远拖不住卖出。

    逐策略隔离：某策略卖出阶段出错 → 回滚、记日志，这一分钟它不进买入阶段（同旧 tick「出错这一轮作废」）；
    其它策略不受影响。返回本分钟全部成交（卖出在前）。"""
    fails: set = set()

    def safe(key, label: str, fn) -> list:
        try:
            return fn()
        except Exception as e:
            conn.rollback()
            log(f"{label}：{e!r}")
            fails.add(key)
            return []

    def emit(fills: list):
        try:
            live_store.add_intents(conn, fills, names)
        except Exception as e:
            conn.rollback()
            log(f"实盘意图写入失败：{e!r}")
        if log_each:
            _log_fills(fills, log)
        _notify_fills(notifier, conn, fills, names, log, acct_names)

    sells = safe("engine", "本轮出错", lambda: engine.sell_step(t))
    quotes = _fresh_quotes(engine, t)
    if board is not None:
        sells += safe("board", "扫首板本轮出错", lambda: board.sell_step(quotes, t))
    if rebound is not None:
        sells += safe("rebound", "超跌反弹本轮出错", lambda: rebound.sell_step(quotes, t))
    if lowbuy is not None:
        sells += safe("lowbuy", "低吸本轮出错", lambda: lowbuy.sell_step(quotes, t))
    for sid, strat in customs:
        sells += safe(sid, f"自定义策略 {sid} 本轮出错", lambda s=strat: s.sell_step(quotes, t))
    emit(sells)

    deadline = monotonic() + config.TICK_JEV_BUDGET
    buys: list = []
    if board is not None and "board" not in fails:        # 扫板窗口 09:30–10:30 时间最紧，先判
        buys += safe("board", "扫首板本轮出错", lambda: board.buy_step(quotes, t, deadline))
    if "engine" not in fails:
        buys += safe("engine", "本轮出错", lambda: engine.buy_step(t))
    if rebound is not None and "rebound" not in fails:
        buys += safe("rebound", "超跌反弹本轮出错", lambda: rebound.buy_step(quotes, t, deadline))
    if lowbuy is not None and "lowbuy" not in fails:
        buys += safe("lowbuy", "低吸本轮出错", lambda: lowbuy.buy_step(quotes, t, deadline))
    for sid, strat in customs:
        if sid not in fails:
            buys += safe(sid, f"自定义策略 {sid} 本轮出错", lambda s=strat: s.buy_step(quotes, t, deadline))
    emit(buys)
    follows: list = []
    if regime is not None:      # 情绪轮动跟这一分钟各策略主账户的成交（实盘跟它）
        quote_of = lambda c: quotes.get(c) or getattr(engine, "last_quotes", {}).get(c)      # noqa: E731
        follows = safe("regime", "情绪轮动本轮出错",
                       lambda: regime.mirror(sells + buys, quote_of, t) + regime.reconcile(quote_of, t))
        emit(follows)
    _publish_candidates(conn, t, board, rebound, quotes, log)
    return sells + buys + follows


def _publish_candidates(conn, t: datetime, board, rebound, quotes: dict, log: Callable[[str], None]):
    """把当前候选快照写进库给面板「候选」页读；只是展示，出错只记日志。行情这一分钟没取到就不动上一份。"""
    if not quotes:
        return
    try:
        candidates.save(conn, t.date(), t,
                        board.candidates_view(quotes) if board is not None else [],
                        rebound.candidates_view(quotes, t) if rebound is not None else None)
    except Exception as e:
        conn.rollback()
        log(f"候选快照写入失败：{e!r}")


def _fresh_quotes(engine: Engine, t: datetime) -> dict:
    """这一分钟引擎取数成功才把行情交给各策略；失败时 last_quotes 还是上一分钟的旧快照，
    拿它去卖会用旧价成交，所以给空字典，各策略这一分钟既不卖也不买（扫首板也不动观察池）。"""
    if getattr(engine, "last_fetch_minute", None) == t.strftime("%Y-%m-%d %H:%M"):
        return engine.last_quotes
    return {}


def _load_customs(conn, ledger: Ledger, today: date, universe: dict, ret5: dict, sell_only: bool,
                  notice_judge, news_judge, concept_map: dict, env_provider, prev_day: date,
                  log: Callable[[str], None], trade_days=None) -> list[tuple[str, CustomStrategy]]:
    """盘前构造今天要驱动的自定义策略：今天生效且启用的（active_for）+ 已停用但还有持仓的（holders，
    恒只卖）。任何一步失败只影响自定义策略本身——内置策略和它们的卖出照常。
    只卖日（缺 key / 日历或股票池失败）或 5 日涨幅覆盖不足（收盘价库不够，历史类字段不可信）时一律只卖。"""
    try:
        specs = custom_store.active_for(conn, today) + custom_store.holders(conn, today)
    except Exception as e:
        log(f"自定义策略读取失败，今日不驱动自定义策略：{e!r}")
        return []
    if not specs:
        return []
    can_buy = not sell_only and bool(universe) and bool(ret5)
    try:
        held = {p.code for s in specs for a in (f"{s['id']}_main", f"{s['id']}_shadow")
                for p in ledger.positions(a)}
        features = history_features(conn, sorted(set(universe) | held), today)
    except Exception as e:
        log(f"自定义策略历史特征计算失败，历史类条件今日不命中：{e!r}")
        features = {}
    out = []
    for s in specs:
        try:
            strat = CustomStrategy(conn, ledger, s["id"], s["version"], s["spec"], notice_judge, news_judge,
                                   dict(universe), features, concept_map, env_provider, prev_day,
                                   can_buy=can_buy and s["can_buy"], trade_days=trade_days)
        except Exception as e:
            conn.rollback()
            log(f"自定义策略 {s['id']} 加载失败：{e!r}")
            continue
        out.append((s["id"], strat))
        log(f"自定义策略 {s['id']} v{s['version']}「{s['name']}」" + ("" if strat.can_buy else "（只卖）"))
    return out


def _own_intraday_recorder(conn, key: str | None, universe: dict, ret5: dict, log: Callable[[str], None],
                           skip: bool) -> "own_sectors.IntradayRecorder | None":
    """盘中板块情绪记录器（只记录）：后台线程用自己的库连接和 Jev 客户端。内存库（测试）、没有 Jev 凭证、
    晚启动或只卖日不开。"""
    if skip or not key or not universe or not config.OWN_SECTORS_ENABLED:
        return None
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        return None
    if not path:
        return None
    return own_sectors.IntradayRecorder(lambda: db.connect(path),
                                        lambda c: OwnSectorJudge(JevClient(key, conn=c)), universe, ret5, log)


def _load_rebound(conn, ledger: Ledger, today: date, universe: dict, ret5: dict, sell_only: bool,
                  notice_judge, news_judge, engine: Engine, prev_day: date,
                  log: Callable[[str], None], trade_days=None, trend_judge=None) -> ReboundStrategy | None:
    """盘前构造超跌反弹。只卖日 / 股票池或 5 日涨幅为空 / 连板排除算不出来 → 只卖不买（排除是安全过滤，宁可不买）。
    情绪直接读引擎在买入阶段刷新的结果，不另问 Jev。"""
    enabled = not sell_only and bool(universe) and bool(ret5)
    excluded: set[str] = set()
    if enabled:
        try:
            excluded, n = closes.consecutive_board_codes(conn, universe, today, config.REBOUND_BOARD_LOOKBACK,
                                                         config.REBOUND_MIN_BOARDS)
            need = config.REBOUND_BOARD_LOOKBACK + 1
            log(f"超跌反弹：近 {max(n - 1, 0)} 个交易日出现过 {config.REBOUND_MIN_BOARDS} 连板的 {len(excluded)} 只排除"
                + ("" if n >= need else f"（收盘价库只有 {n} 天，不足 {need} 天，排除可能不全）"))
        except Exception as e:
            log(f"超跌反弹连板排除计算失败，今日只卖不买：{e!r}")
            excluded, enabled = set(), False
    features: dict | None = None      # None = 不看成交量（REBOUND_MIN_SHRINK_DAYS = 0）
    if enabled and config.REBOUND_MIN_SHRINK_DAYS > 0:
        try:
            features = history_features(conn, sorted(universe), today)
            n = config.REBOUND_MIN_SHRINK_DAYS             # 连续缩量 n 天要最近 n 个历史日的量都在（今天的量盘中折算）
            ok = sum(1 for f in features.values() if len(f.get("vols") or []) >= n and all(f["vols"][-n:]))
            log(f"超跌反弹：成交量历史齐全 {ok}/{len(universe)} 只（买入要求连续缩量 ≥ {n} 天，"
                f"≥ {config.REBOUND_PRIORITY_SHRINK_DAYS} 天优先）")
        except Exception as e:
            log(f"超跌反弹成交量历史计算失败，今日只卖不买：{e!r}")
            features, enabled = {}, False
    try:
        strat = ReboundStrategy(conn, ledger, notice_judge, news_judge, dict(universe), ret5, excluded, prev_day,
                                lambda: engine.sentiment_label, enabled=enabled,
                                sentiment_trace_provider=lambda: engine.sentiment_trace, trade_days=trade_days,
                                features=features, trend_judge=trend_judge)
    except Exception as e:
        conn.rollback()
        log(f"超跌反弹加载失败：{e!r}")
        return None
    if not enabled:
        log("超跌反弹：今日只卖不买")
    return strat


def _should_fast_scan(board: BoardStrategy | None, t: time) -> bool:
    """只在扫板窗口内（默认 09:30–10:30）、观察池非空时跑 3 秒快扫；窗口外买不了，照常按分钟节拍走。"""
    return (board is not None and board.has_watch() and in_session(t)
            and in_scan_window(t, board.windows))


def _fast_scan(board: BoardStrategy, stop: threading.Event, log: Callable[[str], None],
               state: dict, notifier: Notifier, conn, acct_names: dict | None = None,
               follow: Callable[[list, datetime], None] | None = None) -> bool:
    """观察池非空时的 3 秒扫板循环。返回是否被 stop 打断。

    边界在进入时算死一次（deadline = 现在 + seconds_to_next_minute()，带着 :02 缓冲），
    循环全程只跟这个固定 deadline 比。不能在每轮重新用「现在到下一分钟还有多久」算边界——
    on_fast 只要有点真实耗时，"现在"就已经跨过分钟整点，重新算出来的又是一个新的、快满
    60 秒的余量，循环会一直续命下去，把后面好几个整分钟的 tick（各策略的卖出都在
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
            fast_fills = board.on_fast(now())
            _log_fills(fast_fills, log, "%H:%M:%S")
            _notify_fills(notifier, conn, fast_fills, state.get("names", {}), log, acct_names)
            if follow is not None and fast_fills:
                follow(fast_fills, now())
        except Exception as e:
            log(f"扫板出错：{e!r}")
        state["board_watch"] = _board_watch_state(board, state.get("names", {}))
    return False


def _follow(regime: RegimeStrategy, fills: list, engine: Engine, t: datetime, conn, log, notifier, names: dict,
            acct_names: dict | None) -> list:
    """扫板快扫里的成交也让情绪轮动跟（实盘跟它）：写实盘意图、记日志、推送。出错只记日志。"""
    try:
        got = regime.mirror(fills, lambda c: engine.last_quotes.get(c), t)
    except Exception as e:
        conn.rollback()
        log(f"情绪轮动跟扫板出错：{e!r}")
        return []
    if got:
        try:
            live_store.add_intents(conn, got, names)
        except Exception as e:
            conn.rollback()
            log(f"实盘意图写入失败：{e!r}")
        _log_fills(got, log, "%H:%M:%S")
        _notify_fills(notifier, conn, got, names, log, acct_names)
    return got


def _load_lowbuy(conn, ledger: Ledger, universe: dict, ret5: dict, sell_only: bool, notice_judge, news_judge,
                 engine: Engine, prev_day: date, log: Callable[[str], None], concept_map: dict | None = None,
                 features: dict | None = None, today: date | None = None) -> LowbuyStrategy | None:
    """低吸（10-09 重新上线）：只卖日 / 股票池或 5 日涨幅为空 → 只卖不买。缩量回调要成交量历史（超跌反弹
    算好的那份，没有就自己算一遍；算不出来只卖不买），主线要题材表。"""
    enabled = not sell_only and bool(universe) and bool(ret5)
    if enabled and features is None:
        try:
            features = history_features(conn, sorted(universe), today or now().date())
        except Exception as e:
            log(f"低吸成交量历史计算失败，今日只卖不买：{e!r}")
            enabled = False
    try:
        strat = LowbuyStrategy(conn, ledger, notice_judge, news_judge, dict(universe), ret5, prev_day,
                               lambda: engine.sentiment_label, lambda: getattr(engine, "overseas", None),
                               enabled=enabled, features=features if features is not None else {},
                               concept_map=concept_map or {}, sectors_provider=lambda: _sectors_of(engine))
    except Exception as e:
        conn.rollback()
        log(f"低吸加载失败：{e!r}")
        return None
    if not enabled:
        log("低吸：今日只卖不买")
    return strat


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


def _prefetch_bodies(conn, today: date, universe, log: Callable[[str], None], budget: float):
    """早盘/午间同步公告后预抓疑似公告正文；最多等 budget 秒，失败只记日志，不影响交易。"""
    try:
        log(f"公告正文预抓 {prefetch_bodies(conn, today, list(universe), budget=budget)} 条")
    except Exception as e:
        log(f"公告正文预抓失败：{e!r}")


def run_backfill(log: Callable[[str], None] = print) -> None:
    """只补收盘价历史：不足 6 天的、最近 21 个交易日里整天缺失/不完整的日期（后台没跑的那天），
    并给最近 10 个交易日量为空的行补上成交量（新浪日 K；腾讯 K 线接口被 WAF 封了）。
    重复跑会跳过已补齐的代码；不交易、不需要 Jev 凭证。
    仍拿 runner 锁，避免和交易进程同时写库。"""
    with hold_run_lock(Path(DB_PATH).parent / "runner.lock"):
        conn = db.connect(DB_PATH)
        try:
            today = now().date()
            days = akdata.trade_days()
            universe = akdata.load_universe(today, days)
            log(f"股票池 {len(universe)} 只")
            filled = closes.backfill_closes(conn, sorted(universe), today, akdata.fetch_daily_sina, log=log,
                                            fill_volume=True, trade_days=days)
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
