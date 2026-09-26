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
from jevq.data import akdata, board_events, closes, concepts, custom_store, tencent
from jevq.data.features import history_features
from jevq.data.notice_body import prefetch_bodies
from jevq.data.notice_store import sync_notices
from jevq.engine import Engine
from jevq.judge.jev import JevClient
from jevq.judge.market import MarketJudge
from jevq.judge.news import NewsJudge
from jevq.judge.notices import NoticeJudge
from jevq.ledger import Ledger
from jevq.notify import Notifier, notifier_from_env
from jevq.report import write_report
from jevq.rules.board import in_scan_window
from jevq.runlock import hold_run_lock
from jevq.strategies.board import BoardStrategy
from jevq.strategies.custom import CustomStrategy

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
        _prefetch_bodies(conn, today, universe, log, config.PREFETCH_BUDGET)

    notice_judge, news_judge = NoticeJudge(conn, jev), NewsJudge(conn, jev)
    # 账户集合当天固定（自定义策略新版本次日才生效）：盘前算一次，低吸取行情时连自定义账户的持仓一起拉，
    # 否则不在股票池里的自定义持仓（或只卖日股票池为空时的全部自定义持仓）永远拿不到行情、卖不掉。
    try:
        day_accounts = accounts_mod.all_accounts(conn, today)
    except Exception as e:
        log(f"自定义账户读取失败，只拉内置账户持仓行情：{e!r}")
        day_accounts = config.builtin_accounts()
    engine = Engine(conn, ledger, notice_judge, news_judge, market,
                    tencent.fetch_quotes, akdata.fetch_pool_stats, universe, ret5, prev_day, overseas,
                    overseas_trace=market.overseas_trace, accounts_provider=lambda: day_accounts)
    board = None
    if not late:
        board = BoardStrategy(conn, ledger, notice_judge, news_judge, tencent.fetch_quotes, universe, ret5,
                              concept_map, prev_prev_closes, prev_day, enabled=board_enabled,
                              recent_limit_ups=recent_limit_ups)
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

    customs = [] if late else _load_customs(
        conn, ledger, today, universe, ret5, sell_only, notice_judge, news_judge, concept_map,
        lambda: {"sentiment": engine.sentiment_label, "overseas": overseas}, prev_day, log)

    if once:
        t = now()
        fills = _run_minute(engine, board, customs, t, conn, log, notifier, state["names"], acct_names,
                            log_each=False)
        state.update(sentiment=engine.sentiment_label, prices={c: q.price for c, q in engine.last_quotes.items()})
        if board is not None:
            state["board_watch"] = _board_watch_state(board, state["names"])
        log(f"情绪 {engine.sentiment_label}，成交 {fills}")
        return None

    resynced = False
    last_tick_minute = None
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
            _run_minute(engine, board, customs, t, conn, log, notifier, state["names"], acct_names)
            if getattr(engine, "halted", False):
                notifier.once("halted", "⚠️ 模拟盘提醒", "行情连续失败，今日暂停开新仓，卖出照常")
            state.update(sentiment=engine.sentiment_label,
                         prices={c: q.price for c, q in engine.last_quotes.items()})
            state["board_watch"] = _board_watch_state(board, state["names"])
        elif not in_session(t.time()):
            state["phase"] = "休市等待"

        if _should_fast_scan(board, now().time()):
            stopped = _fast_scan(board, stop, log, state, notifier, conn, acct_names)
        else:
            stopped = stop.wait(seconds_to_next_minute())
        if stopped:
            log("收到停止指令，未生成当日报告")
            return None

    prices = {c: q.price for c, q in engine.last_quotes.items()}
    names = dict(universe)
    held = held_codes(ledger, today)
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
        recorded = board_events.record_day(conn, today, zt_rows, concept_map, log=log)
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
    report_accounts = accounts_mod.all_accounts(conn, today)
    log(f"复盘报告 {path}；" + "，".join(f"{a} {ledger.equity(a, prices):,.2f}" for a in report_accounts))
    _notify_close_summary(notifier, ledger, today, prices, log, conn)
    if after_close is not None:
        after_close(conn, today)
    return path


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
    """成交推送：只推各策略的主账户（main / board_main / c{n}_main），影子账户不推。
    任何问题只记日志，不影响交易。正文明细的构建都在 jevq.notify_content 里，这里只是薄薄一层：
    拼标题、挑账户、兜底记日志。"""
    account_names = account_names or ACCOUNT_NAMES
    for f in fills:
        if f.account != "main" and not f.account.endswith("_main"):
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
                log_each: bool = True) -> list:
    """一分钟一轮：先让所有策略卖完（低吸 → 扫首板 → 各自定义，全程不碰 Jev），推送卖出，
    再按「扫首板 → 低吸 → 各自定义」跑买入阶段。买入阶段共用一个 Jev 时间预算（TICK_JEV_BUDGET，
    卖出之后才开始计时）：过了时限各策略不再判断新候选，没判到的下一分钟再判——慢 Jev 永远拖不住卖出。

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
        if log_each:
            _log_fills(fills, log)
        _notify_fills(notifier, conn, fills, names, log, acct_names)

    sells = safe("engine", "本轮出错", lambda: engine.sell_step(t))
    quotes = _fresh_quotes(engine, t)
    if board is not None:
        sells += safe("board", "扫首板本轮出错", lambda: board.sell_step(quotes, t))
    for sid, strat in customs:
        sells += safe(sid, f"自定义策略 {sid} 本轮出错", lambda s=strat: s.sell_step(quotes, t))
    emit(sells)

    deadline = monotonic() + config.TICK_JEV_BUDGET
    buys: list = []
    if board is not None and "board" not in fails:        # 扫板窗口 09:30–10:30 时间最紧，先判
        buys += safe("board", "扫首板本轮出错", lambda: board.buy_step(quotes, t, deadline))
    if "engine" not in fails:
        buys += safe("engine", "本轮出错", lambda: engine.buy_step(t, deadline))
    for sid, strat in customs:
        if sid not in fails:
            buys += safe(sid, f"自定义策略 {sid} 本轮出错", lambda s=strat: s.buy_step(quotes, t, deadline))
    emit(buys)
    return sells + buys


def _fresh_quotes(engine: Engine, t: datetime) -> dict:
    """这一分钟低吸 tick 取数成功才把行情交给扫首板；失败时 last_quotes 还是上一分钟的旧快照，
    拿它去卖会用旧价成交，所以给空字典，扫首板这一分钟既不卖也不动观察池。"""
    if getattr(engine, "last_fetch_minute", None) == t.strftime("%Y-%m-%d %H:%M"):
        return engine.last_quotes
    return {}


def _load_customs(conn, ledger: Ledger, today: date, universe: dict, ret5: dict, sell_only: bool,
                  notice_judge, news_judge, concept_map: dict, env_provider, prev_day: date,
                  log: Callable[[str], None]) -> list[tuple[str, CustomStrategy]]:
    """盘前构造今天要驱动的自定义策略：今天生效且启用的（active_for）+ 已停用但还有持仓的（holders，
    恒只卖）。任何一步失败只影响自定义策略本身——低吸、扫首板和它们的卖出照常。
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
                                   can_buy=can_buy and s["can_buy"])
        except Exception as e:
            conn.rollback()
            log(f"自定义策略 {s['id']} 加载失败：{e!r}")
            continue
        out.append((s["id"], strat))
        log(f"自定义策略 {s['id']} v{s['version']}「{s['name']}」" + ("" if strat.can_buy else "（只卖）"))
    return out


def _should_fast_scan(board: BoardStrategy | None, t: time) -> bool:
    """只在扫板窗口内（默认 09:30–10:30）、观察池非空时跑 3 秒快扫；窗口外买不了，照常按分钟节拍走。"""
    return (board is not None and board.has_watch() and in_session(t)
            and in_scan_window(t, board.windows))


def _fast_scan(board: BoardStrategy, stop: threading.Event, log: Callable[[str], None],
               state: dict, notifier: Notifier, conn, acct_names: dict | None = None) -> bool:
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
            fast_fills = board.on_fast(now())
            _log_fills(fast_fills, log, "%H:%M:%S")
            _notify_fills(notifier, conn, fast_fills, state.get("names", {}), log, acct_names)
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


def _prefetch_bodies(conn, today: date, universe, log: Callable[[str], None], budget: float):
    """早盘/午间同步公告后预抓疑似公告正文；最多等 budget 秒，失败只记日志，不影响交易。"""
    try:
        log(f"公告正文预抓 {prefetch_bodies(conn, today, list(universe), budget=budget)} 条")
    except Exception as e:
        log(f"公告正文预抓失败：{e!r}")


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
