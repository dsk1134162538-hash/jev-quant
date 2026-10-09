"""问 Jev：持仓票今天到现在的分时走向（冲高回落 / 高开低走 / 单边下跌 / 震荡 / 探底回升 / 低开高走）。

只在超跌反弹主账户止损确认后、低开反抽触发时问（见 rules.rebound_sell），每次判断最多问一次。
分时拉不到也照样问（只给开高低收）；Jev 没答上 → None，调用方按原规则卖。
"""
from datetime import datetime
from time import monotonic

from jevq.config import REBOUND_TREND_BUDGET, TREND_LEVELS
from jevq.data import tencent
from jevq.judge.jev import JevError, score_label, score_probs
from jevq.models import Position, Quote

INSTRUCTIONS = ("根据这只股票今天到现在的走势（昨收、开盘、最高、最低、现价，以及分时价格和成交量），"
                "判断今天的走向属于哪一种。探底回升、低开高走 指已经止跌并在往上走；"
                "单边下跌、高开低走、冲高回落 指还在往下走；震荡 指来回波动没有方向。")


def _pct(price: float, pre_close: float) -> str:
    return f"{(price / pre_close - 1) * 100:+.2f}%"


def minute_summary(rows: list[tuple[str, float, int]], pre_close: float, step: int = 5,
                   tail: int = 10) -> list[str]:
    """分时压成几十行给 Jev：每 step 分钟一个点（量是这段的增量），最后 tail 分钟逐分钟。"""
    if not rows or pre_close <= 0:
        return []
    keep = {i for i in range(0, len(rows), step)} | set(range(max(len(rows) - tail, 0), len(rows)))
    lines, prev_vol = [], 0
    for i in sorted(keep):
        t, price, vol = rows[i]
        lines.append(f"{t} {price:.2f} {_pct(price, pre_close)} 量 {max(vol - prev_vol, 0)}手")
        prev_vol = vol
    return lines


class TrendJudge:
    def __init__(self, jev, fetch_minutes=tencent.fetch_minutes):
        self.jev = jev
        self.fetch_minutes = fetch_minutes
        self.last_trace: dict | None = None

    def judge(self, pos: Position, q: Quote, now: datetime) -> str | None:
        deadline = monotonic() + REBOUND_TREND_BUDGET
        note = ""
        try:
            rows = self.fetch_minutes(pos.code, timeout=min(5.0, REBOUND_TREND_BUDGET / 2))
        except Exception as e:
            rows, note = [], f"分时获取失败：{e!r}"
        state = {"名称": q.name or pos.code, "时间": now.strftime("%H:%M"),
                 "买入价": pos.buy_price, "持仓盈亏%": round((q.price / pos.buy_price - 1) * 100, 2),
                 "昨收": q.pre_close, "开盘": q.open, "最高": q.high, "最低": q.low, "现价": q.price,
                 "开盘涨幅%": q.open_pct, "最高涨幅%": round((q.high / q.pre_close - 1) * 100, 2),
                 "最低涨幅%": q.low_pct, "涨幅%": q.pct,
                 "分时": minute_summary(rows, q.pre_close)}
        self.last_trace = {"inputs": state, "label": None, "note": note}
        try:
            ans = self.jev.ask(state, {"trend": {"type": "score", "instructions": INSTRUCTIONS,
                                                 "criteria": TREND_LEVELS}}, deadline=deadline)["trend"]
            label = score_label(ans)
        except (JevError, KeyError, TypeError, ValueError) as e:
            self.last_trace["note"] = "；".join(x for x in (note, f"Jev 没答上：{e!r}") if x)
            return None
        self.last_trace.update(label=label, probabilities=score_probs(ans), confidence=ans.get("confidence"))
        return label
