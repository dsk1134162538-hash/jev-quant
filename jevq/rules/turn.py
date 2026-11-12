"""情绪拐点（恐慌后的修复）识别：纯代码，按每分钟的全市场广度判断。用户 2026-10-09 定：
恐慌后一有反弹拉升迹象就快速抄底，直接实盘，信号要明确。规则取自知识库（@gsxt233）里的几条：
「全市场红盘家数低于 1000 家，次日修复概率大」「修复节点：跌停降到约 5 家以内、成交额边际放大」
「低吸看恐慌释放后不再创新低」「连续冰点后盘中恐慌急杀但动能衰竭，是反弹先手点（常在下午或尾盘）」。

全部满足才算拐点（规则只给候选，Jev 再确认，见 jevq.judge.turn）：
1. window   ：TURN_START ~ TURN_END 之间
2. panic    ：今天（TURN_PANIC_FROM 09:35 起，开盘头几分钟不算）红盘占比最低到过 ≤ TURN_PANIC_UP（30%）
   ld_peak  ：而且今天跌停峰值 ≥ TURN_LD_PEAK_MIN（10 家）——确实恐慌过，不是普通弱开（10-14 用户定）
3. low_age  ：那个最低点离现在 ≥ TURN_LOW_AGE 个交易分钟（15，跨午休只算交易时间）——恐慌释放后没再创新低
4. up_min   ：现在红盘占比 ≥ TURN_UP_MIN（35%）
5. up_gain  ：现在比最低点多 ≥ TURN_UP_GAIN（15 个百分点）——广度明显修复
6. limit_down：现在跌停家数 ≤ max(TURN_LD_FLOOR, 今天最多的 × TURN_LD_SHRINK)——亏钱效应收敛
7. volume   ：成交额较昨日同期% 比 TURN_VOL_LOOKBACK（30）个交易分钟前高——放量承接；缺数据不算满足
"""
from dataclasses import dataclass
from datetime import datetime, time

from jevq import config

MORNING = (time(9, 30), time(11, 30))


@dataclass(frozen=True)
class Breadth:
    ts: datetime
    up: int
    down: int
    total: int
    limit_up: int
    limit_down: int
    amount_ratio: float | None      # 成交额较昨日同期%（全股票池）；没有昨日同期数据 = None

    @property
    def up_ratio(self) -> float:
        return self.up / self.total if self.total else 0.0


def trading_index(t: time) -> int:
    """09:30 起第几个交易分钟（午休不算）：09:30→0，11:29→119，13:00→120。"""
    m = t.hour * 60 + t.minute
    if m < 11 * 60 + 30:
        return m - (9 * 60 + 30)
    if m < 13 * 60:
        return 120
    return 120 + m - 13 * 60


def breadth(quotes: dict, universe, now: datetime, amount_ratio: float | None) -> Breadth:
    """股票池内、行情是今天的票：上涨/下跌/涨停/跌停家数。"""
    qs = [q for c, q in quotes.items() if c in universe and q.ts.date() >= now.date() and not q.suspended]
    return Breadth(now.replace(second=0, microsecond=0), sum(q.pct > 0 for q in qs), sum(q.pct < 0 for q in qs),
                   len(qs), sum(bool(q.limit_up) and q.price >= q.limit_up for q in qs),
                   sum(bool(q.limit_down) and q.price <= q.limit_down for q in qs), amount_ratio)


def turn_check(series: list[Breadth], now: datetime) -> tuple[bool, dict]:
    """series：今天到现在的广度（时间升序，最后一条是现在）。返回（是不是拐点, 各项明细）。"""
    today = [b for b in series if b.ts.date() == now.date() and b.total]
    if not today:
        return False, {"checks": {"data": False}}
    cur = today[-1]
    i_now = trading_index(cur.ts.time())
    settled = [b for b in today if b.ts.time() >= config.TURN_PANIC_FROM] or today
    low = min(settled, key=lambda b: (b.up_ratio, -trading_index(b.ts.time())))   # 同为最低取最近的一次
    peak_ld = max(b.limit_down for b in today)
    before = [b for b in today if trading_index(b.ts.time()) <= i_now - config.TURN_VOL_LOOKBACK]
    vol_before = before[-1].amount_ratio if before else None
    ld_line = max(config.TURN_LD_FLOOR, peak_ld * config.TURN_LD_SHRINK)
    checks = {
        "window": config.TURN_START <= now.time() < config.TURN_END,
        "panic": low.up_ratio <= config.TURN_PANIC_UP,
        "ld_peak": peak_ld >= config.TURN_LD_PEAK_MIN,
        "low_age": i_now - trading_index(low.ts.time()) >= config.TURN_LOW_AGE,
        "up_min": cur.up_ratio >= config.TURN_UP_MIN,
        "up_gain": cur.up_ratio - low.up_ratio >= config.TURN_UP_GAIN,
        "limit_down": cur.limit_down <= ld_line,
        "volume": cur.amount_ratio is not None and vol_before is not None and cur.amount_ratio > vol_before,
    }
    detail = {"checks": checks, "up": cur.up, "total": cur.total, "up_ratio": round(cur.up_ratio * 100, 1),
              "low_up_ratio": round(low.up_ratio * 100, 1), "low_at": low.ts.strftime("%H:%M"),
              "limit_down": cur.limit_down, "peak_limit_down": peak_ld, "limit_down_line": round(ld_line, 1),
              "amount_ratio": cur.amount_ratio, "amount_ratio_before": vol_before}
    return all(checks.values()), detail


def describe(detail: dict) -> str:
    """推送/记录用的一句话。"""
    return (f"红盘 {detail['up']}/{detail['total']}（{detail['up_ratio']}%，{detail['low_at']} 最低 "
            f"{detail['low_up_ratio']}%）｜跌停 {detail['limit_down']}（最多 {detail['peak_limit_down']}）｜"
            f"成交额较昨日同期 {detail['amount_ratio']}%（30 分钟前 {detail['amount_ratio_before']}%）")
