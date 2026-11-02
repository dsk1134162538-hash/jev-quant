"""动态账户列表：内置四个 + 今天在跑（或仍有持仓待清）的自定义策略账户。"""
import sqlite3
from datetime import date

from jevq import config
from jevq.data import custom_store


def _custom_accounts(sid: str) -> tuple[str, str]:
    return f"{sid}_main", f"{sid}_shadow"


def _custom_strategies(conn: sqlite3.Connection, day: date) -> list[dict]:
    return custom_store.active_for(conn, day) + custom_store.holders(conn, day)


def all_accounts(conn: sqlite3.Connection, day: date) -> tuple[str, ...]:
    accounts = list(config.builtin_accounts())
    for s in _custom_strategies(conn, day):
        accounts.extend(_custom_accounts(s["id"]))
    return tuple(accounts)


def account_names(conn: sqlite3.Connection, day: date) -> dict[str, str]:
    names = dict(config.ACCOUNT_NAMES)
    for s in _custom_strategies(conn, day):
        main, shadow = _custom_accounts(s["id"])
        names[main] = f"{s['name']}·主(Jev)"
        names[shadow] = f"{s['name']}·影子"
    return names


def account_strategy(conn: sqlite3.Connection, day: date) -> dict[str, str]:
    strategy = dict(config.ACCOUNT_STRATEGY)
    for s in _custom_strategies(conn, day):
        main, shadow = _custom_accounts(s["id"])
        strategy[main] = s["id"]
        strategy[shadow] = s["id"]
    return strategy


def _is_custom_strategy(strategy: str) -> bool:
    return bool(strategy) and strategy.startswith("c") and strategy[1:].isdigit()


def capital_baseline(conn: sqlite3.Connection, strategy: str) -> float:
    """净值/收益率的基准资金：内置两个策略固定用 INITIAL_CASH；自定义策略的本金在创建（version 1）
    时就冻结了（custom_store.save 拒绝之后改资金），收益率要用它自己的本金算，不能全用
    INITIAL_CASH——不然资金跟内置不一样的自定义策略，账面收益率天生就是错的。"""
    if _is_custom_strategy(strategy):
        cap = custom_store.capital_of(conn, strategy)
        if cap is not None:
            return cap
    return config.STRATEGY_CAPITAL.get(strategy, config.INITIAL_CASH)
