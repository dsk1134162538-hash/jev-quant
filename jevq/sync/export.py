"""每日导出：把成交/净值/持仓/决策/封板事件写成 UTF-8 CSV，供在家电脑通过 GitHub 查看。

只读——这里从不对 conn 发起写操作。任何异常交给调用方处理（daemon.py 的 after_close 包了
一层 try/except 只记日志，不影响交易本身）。
"""
import csv
from datetime import date
from pathlib import Path


def _write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export_day(conn, day: date, out_dir: str = "exports") -> list[Path]:
    """写出/重写这些文件并返回它们的路径：

    - fills.csv：全部成交，按 id 排序（重写整个文件）
    - equity.csv：全部净值行（重写）
    - positions.csv：当前持仓（重写）
    - decisions/YYYY-MM-DD.csv：当天决策，不含 trace（体积大）
    - board_events.csv：全部封板事件（重写）
    - sell_followups.csv：卖飞统计（重写）
    """
    out = Path(out_dir)
    written = []

    rows = conn.execute(
        "SELECT id, account, code, side, price, shares, fee, ts, reason "
        "FROM fills ORDER BY id").fetchall()
    p = out / "fills.csv"
    _write_csv(p, ["id", "account", "code", "side", "price", "shares", "fee", "ts", "reason"], rows)
    written.append(p)

    rows = conn.execute(
        "SELECT date, account, value FROM equity ORDER BY date, account").fetchall()
    p = out / "equity.csv"
    _write_csv(p, ["date", "account", "value"], rows)
    written.append(p)

    rows = conn.execute(
        "SELECT account, code, shares, cost, buy_price, buy_date FROM positions "
        "ORDER BY account, code").fetchall()
    p = out / "positions.csv"
    _write_csv(p, ["account", "code", "shares", "cost", "buy_price", "buy_date"], rows)
    written.append(p)

    rows = conn.execute(
        "SELECT ts, strategy, version, code, verdict, detail FROM decisions "
        "WHERE ts LIKE ? ORDER BY ts", (f"{day.isoformat()}%",)).fetchall()
    p = out / "decisions" / f"{day.isoformat()}.csv"
    _write_csv(p, ["ts", "strategy", "version", "code", "verdict", "detail"], rows)
    written.append(p)

    rows = conn.execute(
        "SELECT date, code, name, first_seal, bucket, concept, next_open_pct, "
        "next_close_pct, next_board FROM board_events ORDER BY date, code").fetchall()
    p = out / "board_events.csv"
    _write_csv(p, ["date", "code", "name", "first_seal", "bucket", "concept",
                   "next_open_pct", "next_close_pct", "next_board"], rows)
    written.append(p)

    rows = conn.execute(
        "SELECT fill_id, account, code, sell_date, sell_price, after_high, day_close, next_date, next_high, "
        "next_close FROM sell_followups ORDER BY fill_id").fetchall()
    p = out / "sell_followups.csv"
    _write_csv(p, ["fill_id", "account", "code", "sell_date", "sell_price", "after_high", "day_close",
                   "next_date", "next_high", "next_close"], rows)
    written.append(p)

    return written
