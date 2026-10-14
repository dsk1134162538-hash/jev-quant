"""情绪拐点由 Claude 把关（用户 2026-10-14：必须 Claude 同意才确认）。

规则（jevq.rules.turn）满足后才问；给它今天到现在每 5 分钟的全市场广度和每 10 分钟的涨跌停统计，
问「恐慌后的修复拐点」还是「弱势反抽」。10-14 晚用同样的提示词回放：10-13 13:00（真拐点）0.72，
10-14 10:01（假的，之后一路走弱）0.35；同两个时刻 Jev 给的是 0.83、0.71。
"""
import json
from datetime import datetime

from jevq.judge.claude_cli import ClaudeCLI
from jevq.rules.turn import Breadth

SYSTEM = ("你是 A 股短线情绪分析员。只根据给你的盘中数据判断：此刻是不是「恐慌后的情绪修复拐点」"
          "（当天后面大概率继续修复、次日有冲高），还是普通的弱势盘中反抽（后面大概率回落）。"
          "只输出一行 JSON：{\"turn_prob\": 0到1的数, \"reason\": \"一句话\"}")


def _curve(series: list[Breadth], now: datetime) -> list[str]:
    today = [b for b in series if b.ts.date() == now.date() and b.total and b.ts <= now]
    picked = [b for b in today if b.ts.minute % 5 == 0] + today[-1:]
    seen, out = set(), []
    for b in picked:
        if b.ts in seen:
            continue
        seen.add(b.ts)
        ar = "?" if b.amount_ratio is None else f"{b.amount_ratio:+.1f}%"
        out.append(f"{b.ts:%H:%M} 红盘{b.up_ratio * 100:.0f}% 跌停{b.limit_down} 成交额较昨日同期{ar}")
    return out


def _stats(conn, now: datetime) -> list[str]:
    out = []
    for ts, detail in conn.execute(
            "SELECT ts, detail FROM decisions WHERE verdict='sentiment' AND substr(ts, 1, 10)=? AND ts<=? ORDER BY id",
            (now.date().isoformat(), now.isoformat(timespec="seconds"))):
        s = (json.loads(detail) or {}).get("stats") or {}
        out.append(f"{ts[11:16]} 涨停{s.get('涨停家数')} 跌停{s.get('跌停家数')} 炸板率{s.get('炸板率%')}% "
                   f"最高连板{s.get('最高连板')} 昨日涨停今日均涨{s.get('昨日涨停今日均涨%')}%")
    return out


class ClaudeTurnJudge:
    def __init__(self, cli: ClaudeCLI, conn):
        self.cli, self.conn = cli, conn
        self.last_trace: dict | None = None

    def confirm(self, series: list[Breadth], detail: dict, now: datetime) -> float | None:
        """修复拐点的概率（0~1）；Claude 没答上或答得不能用 → None。"""
        user = (f"日期 {now:%Y-%m-%d}，现在 {now:%H:%M}。\n每 5 分钟全市场广度：\n" + "\n".join(_curve(series, now))
                + "\n\n每 10 分钟涨跌停统计：\n" + ("\n".join(_stats(self.conn, now)) or "（暂无）"))
        got = self.cli.ask_json(SYSTEM, user)
        self.last_trace = {"cli": getattr(self.cli, "last_trace", None), "answer": got}
        try:
            prob = float(got["turn_prob"])
        except (TypeError, KeyError, ValueError):
            return None
        prob = min(1.0, max(0.0, prob))
        self.last_trace.update(prob=prob, reason=str(got.get("reason", ""))[:200])
        return prob
