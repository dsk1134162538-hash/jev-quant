"""家里电脑的只读面板：用公司每天收盘后推到 GitHub 的 exports/ 重建一个本地数据库给面板看。

家里没有实盘数据库（data/jevq.sqlite 只在公司电脑），也绝不能在家里开跑实盘，所以：
- build_db：每次打开面板前从 exports/*.csv 整库重建（删掉旧的再建），数据截至公司最近一次收盘同步；
- ReadOnlyApi：和公司同一个面板接口，但开始运行、保存/停用规则一律拦截；试跑照常（只读行情，不下单）。
导出里没有决策留痕和规则条件（仓库公开，本人定不公开），所以决策点进去没有明细，自定义策略只有名字和本金。
"""
import csv
import json
from pathlib import Path

from jevq import db
from jevq.app.api import Api

HOME_DB = "data/home_view.sqlite"
BLOCKED_MSG = "家里只读：不能在家里开跑实盘或保存规则，请在公司电脑操作"


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v):
    return None if v in (None, "") else float(v)


def build_db(exports_dir: str | Path, db_path: str | Path = HOME_DB) -> str | None:
    """整库重建，返回数据截至的日期（净值表最后一天，没有就取成交最后一天；都没有返回 None）。"""
    exports_dir, db_path = Path(exports_dir), Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    conn = db.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO fills(id, account, code, side, price, shares, fee, ts, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            [(int(r["id"]), r["account"], r["code"], r["side"], float(r["price"]), int(r["shares"]),
              float(r["fee"]), r["ts"], r["reason"]) for r in _rows(exports_dir / "fills.csv")])
        conn.executemany("INSERT INTO equity(date, account, value) VALUES (?,?,?)",
                         [(r["date"], r["account"], float(r["value"])) for r in _rows(exports_dir / "equity.csv")])
        conn.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?)",
                         [(r["account"], r["code"], int(r["shares"]), float(r["cost"]), float(r["buy_price"]),
                           r["buy_date"]) for r in _rows(exports_dir / "positions.csv")])
        conn.executemany("INSERT INTO cash(account, amount) VALUES (?,?)",
                         [(r["account"], float(r["amount"])) for r in _rows(exports_dir / "cash.csv")])
        for p in sorted((exports_dir / "decisions").glob("*.csv")):
            conn.executemany(
                "INSERT INTO decisions(ts, strategy, version, code, verdict, detail) VALUES (?,?,?,?,?,?)",
                [(r["ts"], r["strategy"] or None, int(r["version"]) if r["version"] else None, r["code"],
                  r["verdict"], r["detail"]) for r in _rows(p)])
        conn.executemany(
            "INSERT INTO board_events VALUES (?,?,?,?,?,?,?,?,?)",
            [(r["date"], r["code"], r["name"], r["first_seal"], r["bucket"], r["concept"],
              _num(r["next_open_pct"]), _num(r["next_close_pct"]),
              int(r["next_board"]) if r["next_board"] else None) for r in _rows(exports_dir / "board_events.csv")])
        conn.executemany(
            "INSERT INTO sell_followups VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(int(r["fill_id"]), r["account"], r["code"], r["sell_date"], float(r["sell_price"]),
              _num(r["after_high"]), _num(r["day_close"]), r["next_date"] or None, _num(r["next_high"]),
              _num(r["next_close"])) for r in _rows(exports_dir / "sell_followups.csv")])
        # 规则条件不在导出里：只放名字和本金的占位 spec，够面板显示策略账户、算收益率
        conn.executemany(
            "INSERT INTO custom_strategies VALUES (?,?,?,?,?,?,?)",
            [(r["id"], int(r["version"]), r["name"],
              json.dumps({"name": r["name"], "capital": _num(r["capital"]), "select": {"conditions": []},
                          "exits": [], "home_placeholder": True}, ensure_ascii=False),
              r["created"], r["active_from"], int(r["enabled"])) for r in _rows(exports_dir / "custom_strategies.csv")])
        conn.commit()
        row = conn.execute("SELECT MAX(date) FROM equity").fetchone()
        latest = row[0] if row and row[0] else None
        if latest is None:
            row = conn.execute("SELECT MAX(substr(ts, 1, 10)) FROM fills").fetchone()
            latest = row[0] if row and row[0] else None
        return latest
    finally:
        conn.close()


class ReadOnlyApi(Api):
    """家里面板的接口：开始运行、保存/停用规则一律拦截，顶部提示「家里只读、数据截至哪天」。"""

    def __init__(self, db_path: str = HOME_DB, as_of: str | None = None, note: str = "", **kw):
        super().__init__(db_path=db_path, runner=lambda **_: None, **kw)
        self._home_warning = (f"家里只读：数据截至 {as_of} 收盘同步" if as_of else "家里只读：还没有公司的导出数据") \
            + (f"；{note}" if note else "")
        self._state["warning"] = self._home_warning

    def start(self) -> dict:
        return {"ok": False, "msg": BLOCKED_MSG}

    def stop(self) -> dict:
        return {"ok": True, "msg": "家里只读，没有在运行"}

    def status(self) -> dict:
        self._state["warning"] = self._home_warning
        return super().status()

    def rule_create(self, spec: dict) -> dict:
        return {"ok": False, "errors": [BLOCKED_MSG]}

    def rule_update(self, sid: str, spec: dict) -> dict:
        return {"ok": False, "errors": [BLOCKED_MSG]}

    def rule_disable(self, sid: str) -> dict:
        return {"ok": False, "errors": [BLOCKED_MSG]}
