"""AI 打首板（dj 2026-11-01）：买点照老扫首板，DeepSeek 管开不开闸；卖出单票不亏损。

卖出（买入次日起）：
1. 扣掉卖出费用后低于持仓成本 → 不卖，进回本模式（不止损、不割肉）
2. 不亏的前提下，老扫首板的卖点照用（竞价不及预期、炸板、冲高回落、尾盘清仓…）→ 卖，快进快出
3. 拿过一个完整交易日还没走的（回本仓）→ 一回本就平，不长期持有

闸门：DeepSeek 最近一次判断是「允许模拟小仓评估」且不超过 AI_BOARD_GATE_MAX_AGE 秒才开；
没有判断、判断过期、读不到、额度用完 → 关闸 = 空仓避险。另外它在这只票上明确给了「观察 / 模拟卖出」也不买。
"""
import json
from datetime import date, datetime
from pathlib import Path

from jevq.config import AI_BOARD_GATE_ACTION, AI_BOARD_GATE_MAX_AGE, MARKET_OPEN
from jevq.ledger import sell_fee
from jevq.models import Position, Quote
from jevq.rules.board_sell import decide_board_sell
from jevq.rules.sell import SellDecision

HOLD = SellDecision(False)


def net_pnl(pos: Position, price: float) -> float:
    """按这个价全部卖出，扣卖出费用后相对含买入费用成本的盈亏（元）。"""
    amount = price * pos.shares
    return round(amount - sell_fee(amount) - pos.cost, 2)


def breakeven_price(pos: Position) -> float:
    """扣费后刚好不亏的最低卖价（分），面板和推送用。"""
    price = round(pos.cost / pos.shares, 2)
    while net_pnl(pos, price) < 0:
        price = round(price + 0.01, 2)
    return price


def decide_noloss_sell(pos: Position, q: Quote, now: datetime, prev_day: date | None) -> SellDecision:
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN:
        return HOLD
    if net_pnl(pos, q.price) < 0:
        return HOLD                                      # 回本模式：亏着不卖
    base = decide_board_sell(pos, q, now, None)          # 不用「隔夜超期」：回本仓本来就会隔夜
    if base.sell:
        return SellDecision(True, f"{base.reason}（不亏）")
    if prev_day is not None and pos.buy_date < prev_day and q.price < q.limit_up:
        return SellDecision(True, "回本平仓")
    return HOLD


def last_judgment(path: Path) -> dict | None:
    """ai_decisions.jsonl 的最后一条（DeepSeek 每轮追加一行）。文件大了只读尾部。"""
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 262144))
        lines = [ln for ln in f.read().decode("utf-8", errors="ignore").splitlines() if ln.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def last_orders(path: Path, now: datetime) -> dict:
    """DeepSeek 最近一轮对各只票给的动作（subscription_answers.json 的 portfolio.orders）；过期或读不到 → {}。"""
    try:
        answers = json.loads(Path(path).read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(answers["snapshotId"])
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if (now - ts).total_seconds() > AI_BOARD_GATE_MAX_AGE or ts.date() != now.date():
        return {}
    return ((answers.get("portfolio") or {}).get("orders")) or {}


def gate(record: dict | None, code: str, now: datetime, orders: dict | None = None) -> tuple[bool, str]:
    """（开不开闸, 说明）。orders = last_orders(...)。"""
    if not record:
        return False, "还没有 DeepSeek 判断，空仓"
    try:
        ts = datetime.fromisoformat(record["ts"])
    except (KeyError, TypeError, ValueError):
        return False, "DeepSeek 判断没有时间，空仓"
    age = (now - ts).total_seconds()
    if age > AI_BOARD_GATE_MAX_AGE or ts.date() != now.date():
        return False, f"DeepSeek 判断已过期（{record['ts'][11:16]}），空仓"
    action = record.get("action")
    if action != AI_BOARD_GATE_ACTION:
        return False, f"DeepSeek {record['ts'][11:16]}：{record.get('regime')} · {action}，空仓避险"
    order = (orders or {}).get(code)
    if order and order.get("action") != "模拟买入":
        return False, f"DeepSeek 对这只给了「{order.get('action')}」：{(order.get('reason') or '')[:40]}"
    return True, f"DeepSeek {record['ts'][11:16]} 开闸：{record.get('regime')}"


# ---- dj 2026-11-02：闸门改规则、次日开盘就卖（108 天回测：开盘就卖优于任何等反弹/拿到收盘/止损的卖法）----

def rule_gate(stats: dict | None) -> tuple[bool, str]:
    """上一交易日收盘前最后一次情绪统计：跌停家数 > 涨停家数 且 前日涨停股昨天红盘率 < 40% → 空仓；其余开闸。
    （历史经验条 3:1 站得住的那条；模型判开闸在验证段没有价值）。没统计不拦。"""
    if not stats:
        return True, "没有上一交易日情绪统计，按规则开闸"
    up, down, red = stats.get("涨停家数"), stats.get("跌停家数"), stats.get("昨日涨停今日红盘率%")
    if up is None or down is None or red is None:
        return True, "上一交易日情绪统计缺项，按规则开闸"
    if down > up and red < 40:
        return False, f"昨日跌停 {down} > 涨停 {up}，前日涨停股红盘率 {red}% < 40%，空仓"
    return True, f"规则开闸（昨日涨停 {up}、跌停 {down}、接力红盘率 {red}%）"


def gate_v2(stats: dict | None, code: str, now: datetime, orders: dict | None = None) -> tuple[bool, str]:
    """规则管开不开闸；DeepSeek 只保留对个股的否决（最近一轮明确给了「观察 / 模拟卖出」就不买）。"""
    ok, why = rule_gate(stats)
    if not ok:
        return False, why
    order = (orders or {}).get(code)
    if order and order.get("action") != "模拟买入":
        return False, f"DeepSeek 对这只给了「{order.get('action')}」：{(order.get('reason') or '')[:40]}"
    return True, why


def decide_open_sell(pos: Position, q: Quote, now: datetime) -> SellDecision:
    """买入次日一开盘就卖，不管高开低开（跌停卖不掉由账本拦下，下一轮再试）。"""
    if pos.buy_date >= now.date() or now.time() < MARKET_OPEN or q.ts.date() < now.date():
        return HOLD
    pct = (q.price / q.pre_close - 1) * 100 if q.pre_close else 0.0
    return SellDecision(True, f"次日开盘卖（{pct:+.2f}%）")
