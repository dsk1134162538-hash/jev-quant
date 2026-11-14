"""AI 灵活交易（dj 2026-11-03）：ai_loop 里的「交易员」一侧。

关键时点（09:26、10:00、10:30、11:15、13:20、14:00、14:35）各调一次 DeepSeek（WorkBuddy），把市场状态、题材梯队、
所有策略的标的、实时行情、账户和今天前几轮的想法给它，让它自己判断买什么、买多少、什么时候卖，输出订单写进
dashboard/runtime/flex_orders.json；daemon 里的执行器（jevq/strategies/flex.py）按实时行情成交，并强制执行护栏和它自己设的止损。
提示词里有知识库的经验手册、一年回测的校准，但明确允许它突破任何选股规则（标「模式外」）。
这个模块只做「想」，不下单、不碰账户。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dashboard import backtester
from jevq import config

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "dashboard" / "runtime"
DB_PATH = ROOT / "data" / "jevq.sqlite"
SNAPSHOT_PATH = RUNTIME / "subscription_snapshot.json"
THEME_CACHE = RUNTIME / "theme_cache.json"
ORDERS_PATH = RUNTIME / "flex_orders.json"
LOG_PATH = RUNTIME / "flex_log.jsonl"
STATE_PATH = RUNTIME / "flex_state.json"
SLOTS = ("09:26", "10:00", "10:30", "11:15", "13:20", "14:00", "14:35")
SLOT_WINDOW = timedelta(minutes=20)       # 时点过了 20 分钟还没跑就不补了（行情早就变了）
MAX_ORDERS_PER_CYCLE = 3
MAX_PICKS = 6
DEFAULT_PCT = 0.30              # 模型没写仓位时按试错额度（单笔 30% 净值）。dj 2026-11-10「加大交易员的仓位」10%→20%；11-11「票数少、仓位大」→30%。不整单拒（2026-11-09 13:20：5 张单 pct 为空被全拒）
_PCT_KEYS = ("pct", "position", "size", "weight", "ratio", "仓位", "仓位比例", "比例")


def _pct_of(raw: dict) -> float:
    """仓位比例：认 "10%"、15（当百分数）、0.08，以及别的字段名；没写按 DEFAULT_PCT，超上限压到上限。"""
    for key in _PCT_KEYS:
        v = raw.get(key)
        if v is None or v == "":
            continue
        s = str(v).strip()
        num = _number(s.rstrip("%"))
        if num is None or num <= 0:
            continue
        if s.endswith("%") or num > 1:
            num /= 100
        return round(min(num, config.FLEX_MAX_PCT), 4)
    return DEFAULT_PCT
STAGES = ("冰点", "启动", "发酵", "高潮", "分歧", "退潮", "混沌")

# 两位交易员的分工（dj 2026-11-11）：11-10 一号 5 只持仓里 3 只是半路/趋势票，和二号买重了，接力的名额被占满。
# 一号只做题材情绪，盘中异动票连看都看不到（gather_context 里按 Trader.skip_src 去掉），买了也会被丢弃；二号照旧能看到。
FLEX_SCOPE = (
    "【分工——程序强制】你只做题材情绪：打板、接力、弱转强、补涨、龙头回封，标的来自 quotes 里 src 为 高标/策略/扫板/梯队 的票和已有持仓。"
    "盘中放量走强但不在涨停梯队里的票（半路、趋势跟随、板块跟涨、大票异动）归二号趋势交易员，不在你的 quotes 里，下单会被程序丢弃；"
    "没有题材情绪的机会就空仓，不要拿趋势票凑数。\n\n"
)
MOVER_NOTE = (
    "不要只盯昨日首板：quotes 里每只票有 src（持仓/高标/策略/扫板/梯队/异动）。src 为「异动」的是今天盘中放量走强、涨幅 2~8%、还没封板的票，"
    "老题材、大票、低位启动、放量突破、板块跟涨都可以买，不需要是首板或涨停股，买入照样要写止损和错了的信号。\n\n"
)

# 提示词里的护栏数字全部从 config 取：改仓位 / 止盈只改 config，AI 被告知的规则和程序执行的不会两样（2026-11-12 体检）
def _tiers_text() -> str:
    from fractions import Fraction
    parts = [f"{'浮盈到' if i == 0 else '到'} {gain:.0%} {'卖掉' if i == 0 else '再卖'} {Fraction(frac).limit_denominator(10)}"
             for i, (gain, frac) in enumerate(config.FLEX_TAKE_PROFIT)]
    return "，".join(parts)


TIERS_TEXT = _tiers_text()

SYSTEM_PROMPT = (
    "你是 A 股短线灵活交易员，管理一个模拟账户（12 万本金，不下真单）。目标是在可承受的回撤内尽量赚钱；"
    "每天都想赚，但不为赚而乱做，空仓也是一种决策。你可以突破任何选股规则：竞价抢、重仓高把握的一两只，"
    "都允许，但订单里必须标「模式外」并写清楚为什么。不要因为没有规则允许就放弃明显的机会，也不要为了出手而出手：先判断市场在哪个阶段，"
    "再决定做什么、做多大。\n\n"
    "【护栏——程序强制执行，违反的订单会被拒，不是选股规则，是让账户活下去】\n"
    f"1. 单票最多占账户净值 {config.FLEX_MAX_PCT:.0%}，持仓最多 {config.FLEX_MAX_POSITIONS} 只，亏损的持仓不加仓；\n"
    "2. 每笔买入必须同时给：止损位 stop（低于现价、不超过 12% 远）、invalid（错了的信号：什么情况说明这笔判断错了，一句话）、"
    "mode（模式内 / 模式外）、pattern（用的是哪种打法）、ref_price（你看到的现价，成交价偏离超过 2% 会被拒，不追）；\n"
    f"3. 当日净值亏到 {config.FLEX_DAY_LOSS:.0%} 当天不再开新仓；连亏 {config.FLEX_LOSS_STREAK} 笔当天不再开新仓；\n"
    "4. 只做沪深主板（60、00 开头），T+1（今天买的明天才能卖，止损也一样）。止损位由程序每分钟盯着执行，所以要定在你真的认错的地方。\n"
    f"5. 越涨越卖（程序自动执行，不用你下单）：{TIERS_TEXT}；浮盈到过 {config.FLEX_PROTECT_AT:.0%} 以后止损自动上移到成本价以上，"
    f"并跟着最高价走，从最高价回撤 {config.FLEX_TRAIL:.0%} 剩下的全卖。持仓里的 stop 就是现在真正会触发卖出的那条线。你想更早兑现可以自己下卖单，但不要指望把浮盈拿回成本再说。\n"
    f"6. 高位不买：quotes 里每只票带 rise5 / rise20（昨收比 5 / 20 个交易日前涨了多少 %）和 fromLow60（比近 60 日最低高多少 %）。"
    f"rise20 ≥ {config.FLEX_MAX_RISE20:.0f}% 的程序直接拒单；30% 以上就算高位，要买得写清凭什么还能涨。"
    "「回测」字段是回测员写的：历史上同样位置的票次日开盘买进后 5 天的中位收益、胜率、5 天内跌超 10% 的比例——"
    "高位票次日常常还能涨，但 5 天内有将近一半会跌超 10%，T+1 跑不掉。只看今天的盘口会看漏——"
    "2026-11-12 冰山冷热「昨天冲板没封住、今天竞价只高开 0.4%」看着是个好买点，其实一个月已经翻倍，买在了山顶。"
    f"另有 biasMa5（昨收比最近 5 个收盘均值高多少 %，即 MA5 乖离）：≥ {backtester.HIGH_BIAS:.0f}% 回测员会标「短期追高」——"
    "历史上 8~12% 那档 5 天内跌超 10% 的占 29%、12% 以上占 48%，普通票只有 5%。不拒单，但要买得说清为什么这次不一样；"
    "11-12 买进、11-13 开盘全部止损的三只，买前乖离都在 14% 以上，其中 002310 的 20 日只涨了 35%。\n\n"
    "【经验手册（来自知识库，按阶段用）】\n"
    "阶段→做法：冰点末期低吸启动的核心；启动期试错（一到三成仓）；发酵期做龙头、一进二、弱转强；高潮日（龙头 ≥5 板、涨停数见顶）对已有持仓兑现，新开仓减小仓位但不是禁止；"
    "分歧日低吸核心、做补涨；退潮期空仓。大周期向上容错高，向下时反弹只当反抽。\n"
    "龙头：题材里最高板、率先涨停、扛得住分歧（爆量烂板后次日还能高开缩量上板）；2~4 板夹在中间的最容易吃面，要么做最高要么做最低；"
    "龙头只出在主线上。\n"
    "上车点：弱转强（昨日烂板或断板，今日竞价小幅低开或高开 0~6% 后转强，不做一字、不做竞价顶到涨停附近）；"
    "补涨（题材龙头 ≥5 板，同题材首板，竞价高开 0~3% 才买，只做第一个）；分歧转一致的第一次回封；强势股缩量回踩 5 日线。\n"
    "卖出：买入前就想好卖点；预期还在就拿，预期消失就卖；次日不连板就卖；冲高无力先卖一半；跌破分时均价线站不回就走；有浮盈把止损上移到成本。\n"
    "不买：下跌趋势、高位放量滞涨、利好兑现、没换手的暴涨、大股东减持、追补涨高开超 3%、龙头断板后次日不修复。\n\n"
    "【一年回测给你的校准（统计，不是禁令；有盘感可以突破，标「模式外」）】\n"
    "题材涨停 ≥8 家、有二板先锋、梯队完整的一致高潮日，次日竞价追买平均亏 1~2%；题材涨停数回落的分歧日更好；"
    "2 板龙头竞价追买最差（平均亏 2% 以上）；5 板以上龙头有溢价但样本很小；9% 追板买不到快板，能买到的平均亏；"
    "补涨高开 0~3% 小幅为正，高开 3~7% 亏。以上都是历史均值、样本不大，不是禁令；普涨放量、昨日涨停股红盘率很高的日子赚钱效应强，均值会失效。"
    "每笔不要太大，对了拿住，错了按止损认。\n\n"
    "【试错额度与不限市值（dj 2026-11-05：分析了半天一只没买，太严了）】没有哪一条经验是一票否决。上涨占比 ≥75% 且昨日涨停股红盘率 ≥85% 这类赚钱效应强的日子，"
    "空仓是代价最高的选择：每天至少试 1~2 笔（单笔 20%~30% 净值，把握大的可到 40%，止损 ≤3%），挑未涨停、涨幅 <7%、有资金承接的票；"
    "只有跌停扩散、炸板率抬头，或日亏 3% / 连亏 3 笔的护栏触发了，才可以整天不动。不限市值：大盘股小盘股都可以，按资金、题材、承接判断。"
    "某个时点不下单，plan 里必须写出「最想买的两只」是谁、这次为什么放弃，不要只写「观望」。\n"
    "plan 或 next 里说要买/要卖的票，必须同时写进 orders（code 用 quotes 里的 6 位代码），orders 里没有的就不会成交——写了「买通富微电」orders 却是空的，等于没买。\n"
    + FLEX_SCOPE +
    "【轮动警示（2026-11-04 实例）】那天昨天的 3~4 连板全部低开低走（-8%~-11%），涨停榜没有连板，116 只首板里三季报增长占 48 只，"
    "资金从旧题材换去低位科技（先进封装、存储芯片、半导体设备、有色）。看 context.highBoards：verdict 是「高标走弱」时，"
    "接力、补涨这类押旧题材延续的打法不要做，改去找低位首板、有业绩或消息催化、放量的新方向；是「高标强势」时可以考虑接力。"
    "这是参考信号，不是开关，有盘感可以突破，标「模式外」。\n\n"
    "输出要求：先写判断（market：大盘一两句；stage：冰点/启动/发酵/高潮/分歧/退潮/混沌 之一；plan：接下来盯什么），"
    "再给订单（可以为空：没把握就不动）。每轮最多 3 张订单。只能从 context.quotes 里挑买入标的，卖出只能卖 account.positions 里的持仓。"
    "订单的 action 只能写「买入」或「卖出」两个中文词；买入的 pct 写占账户净值的小数（0.2 表示 20%），reason 必须写。\n"
    "最后给 picks：这一轮你看过的票里最值得说的 3~6 只（code 用 quotes 或持仓里的 6 位代码），verdict 写「推荐」或「不推荐」，"
    "why 一句话说为什么（推荐写买点和止损位，不推荐写卡在哪：已封板追不进、高位放量滞涨、题材退潮……）。"
    "dj 会看这份清单决定自己要不要手动买，所以要具体；orders 里的买入自动算推荐，不用重复写。"
)


def _slice(text: str, start: str, end: str | None = None) -> str:
    i = text.index(start)
    return text[i:text.index(end)] if end else text[i:]


# 二号交易员（dj 2026-11-05「再加一个 deepseek 交易员」）：护栏、试错额度、输出要求和一号共用同一份文字（单一来源），
# 只有风格不同——趋势/强势跟随，不打板、不接力。
TREND_PROMPT = (
    "你是 A 股趋势/强势跟随交易员（二号），管理一个模拟账户（12 万本金，不下真单）。和一号灵活交易员不同：你不打板、不接力、不追涨停、"
    "不做首板，也不买一字板；你吃的是趋势——板块有资金推动、个股放量走强（涨幅 2~8%、成交额大）、回踩分时均价线或 5 日线不破后再放量的票，"
    "大小盘不限，持有 1~5 个交易日，趋势在就拿，破位就走。目标是在可承受的回撤内尽量赚钱，每天都想赚，但不为赚而乱做，空仓也是一种决策。"
    "你可以突破任何选股偏好，订单里标「模式外」并写清楚为什么。\n\n"
    + _slice(SYSTEM_PROMPT, "【护栏", "【经验手册") +
    "【趋势打法手册】\n"
    "买点：①放量突破前高或平台（成交额明显放大、量比 >1.5）；②强势股回踩分时均价线或 5 日线企稳后再放量转折；"
    "③板块龙头/中军带动下的跟涨：同板块有 3 只以上同时走强，买领先但没封板的那只；④低位首次放量启动（前面横盘或超跌）。\n"
    "卖点：买入前就想好；趋势没坏就拿；跌破止损、分时均价线站不回、5 日线失守、冲高放量滞涨、板块整体转弱就走；浮盈 5% 以上把止损上移到成本。\n"
    "不买：已涨停或一字（买不到也不追）、涨幅已超 8%、下跌趋势里的反抽、缩量冲高、大股东减持、利好兑现的放量大阴线。\n"
    "思路参考（不是禁令，有盘感可以突破）：大盘普涨放量时，找板块里成交额最大的几只走强票；大盘缩量分化时，只买板块里相对最强且回踩不破均价线的；"
    "大盘走弱、跌停扩散就空仓。\n\n"
    + _slice(SYSTEM_PROMPT, "【试错额度", "【分工") + MOVER_NOTE + _slice(SYSTEM_PROMPT, "输出要求")
)


@dataclass(frozen=True)
class Trader:
    """一位 DeepSeek 交易员：自己的提示词、订单文件、日志、时点记录和账户（执行器里的 strategy id 同名）。"""
    sid: str
    label: str
    system: str
    orders_path: Path
    log_path: Path
    state_path: Path
    style: str = ""                 # 给经理看的一句话：这位擅长什么行情
    skip_src: tuple = ()            # 这些来源的候选不给这位交易员（分工由程序拦，不靠提示词自觉）

    @property
    def account(self) -> str:
        return config.STRATEGIES[self.sid]["accounts"][0]


TRADERS = {
    "aiflex": Trader("aiflex", "AI灵活交易", SYSTEM_PROMPT, ORDERS_PATH, LOG_PATH, STATE_PATH,
                     "题材情绪型：打板、接力、弱转强、补涨，吃连板梯队和龙头溢价", skip_src=("异动",)),
    "aiflex2": Trader("aiflex2", "AI趋势交易员", TREND_PROMPT, RUNTIME / "flex2_orders.json", RUNTIME / "flex2_log.jsonl",
                      RUNTIME / "flex2_state.json", "趋势/强势跟随：不打板，买放量走强、回踩不破的票，持有 1~5 天"),
}


# ---------- 调度 ----------
def _read_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def due_slot(now: datetime, state_path: Path = STATE_PATH) -> str | None:
    """现在该跑哪个时点：时点已到、没过 20 分钟、今天还没跑过；多个满足取最近的。没有返回 None。"""
    if now.weekday() >= 5:
        return None
    state = _read_state(state_path)
    done = set(state.get("done", [])) if state.get("date") == now.date().isoformat() else set()
    due = None
    for slot in SLOTS:
        h, m = map(int, slot.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now <= start + SLOT_WINDOW and slot not in done:
            due = slot
    return due


def mark_done(slot: str, now: datetime, state_path: Path = STATE_PATH) -> None:
    state = _read_state(state_path)
    done = list(state.get("done", [])) if state.get("date") == now.date().isoformat() else []
    if slot not in done:
        done.append(slot)
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(json.dumps({"date": now.date().isoformat(), "done": done}), encoding="utf-8")


# ---------- 提示词与答案 ----------
def answer_schema(ctx: dict) -> dict:
    order = {"type": "object", "additionalProperties": False,
             "required": ["action", "code", "reason"],
             "properties": {"action": {"enum": ["买入", "卖出"]}, "code": {"type": "string"},
                            "pct": {"type": "number", "minimum": 0, "maximum": config.FLEX_MAX_PCT},
                            "stop": {"type": "number"}, "ref_price": {"type": "number"},
                            "invalid": {"type": "string", "maxLength": 80}, "mode": {"enum": ["模式内", "模式外"]},
                            "pattern": {"type": "string", "maxLength": 20}, "reason": {"type": "string", "maxLength": 120}}}
    return {"type": "object", "additionalProperties": False, "required": ["snapshotId", "analysis", "orders"],
            "properties": {"snapshotId": {"const": ctx["snapshotId"]},
                           "analysis": {"type": "object", "additionalProperties": False, "required": ["market", "stage", "plan"],
                                        "properties": {"market": {"type": "string", "maxLength": 300},
                                                       "stage": {"enum": list(STAGES)},
                                                       "plan": {"type": "string", "maxLength": 300}}},
                           "orders": {"type": "array", "maxItems": MAX_ORDERS_PER_CYCLE, "items": order},
                           "next": {"type": "string", "maxLength": 200},
                           "picks": {"type": "array", "maxItems": MAX_PICKS, "items": {
                               "type": "object", "additionalProperties": False, "required": ["code", "verdict", "why"],
                               "properties": {"code": {"type": "string"}, "verdict": {"enum": ["推荐", "不推荐"]},
                                              "why": {"type": "string", "maxLength": 60}}}}}}


def build_prompt(ctx: dict, trader: Trader | None = None) -> tuple[str, str]:
    system = (trader or TRADERS["aiflex"]).system
    prompt = ("模拟账户，不下真单。快照编号必须原样返回。不调用任何工具。\n"
              "实时数据：\n" + json.dumps(ctx, ensure_ascii=False)
              + "\n\n请按系统提示里的输出要求，返回一个 JSON 对象：snapshotId、analysis{market,stage,plan}、orders[]、next。")
    return system, prompt


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _number(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


_BUY_WORDS = {"买入", "买", "buy", "long", "开仓", "加仓"}
_SELL_WORDS = {"卖出", "卖", "sell", "平仓", "清仓", "减仓"}
_ACTION_KEYS = ("action", "side", "type", "direction", "op", "操作", "动作")


def _action_of(raw: dict):
    """2026-11-09 实盘：模型的动作写成 "buy" 或放在别的字段里，校验只认「买入/卖出」，一天 14 张单全丢。这里先认出来再判。"""
    for key in _ACTION_KEYS:
        word = str(raw.get(key) or "").strip().lower()
        if word in _BUY_WORDS:
            return "买入"
        if word in _SELL_WORDS:
            return "卖出"
    words = {str(v).strip().lower() for k, v in raw.items() if k not in ("reason", "invalid", "pattern") and isinstance(v, str)}
    if words & _BUY_WORDS and not words & _SELL_WORDS:          # 字段名不认识，但值明确写着买 / 卖
        return "买入"
    if words & _SELL_WORDS and not words & _BUY_WORDS:
        return "卖出"
    return raw.get("action")


def validate_answer(answer, ctx: dict) -> dict:
    """答案校验：编号必须对；订单只留代码在候选/持仓里的（模型编的代码丢掉）；每轮最多 MAX_ORDERS_PER_CYCLE 张。
    字段合不合规（止损位、模式…）由执行器按护栏判，这里只做类型整理。"""
    if not isinstance(answer, dict):
        raise ValueError("灵活交易的回答不是 JSON 对象")
    if answer.get("snapshotId") != ctx["snapshotId"]:
        raise ValueError("灵活交易的回答与快照编号不一致")
    held = {p["code"] for p in ctx.get("account", {}).get("positions", [])}
    quotes = set(ctx.get("quotes", {}))
    handed = set(ctx.get("handedOff") or [])
    orders, dropped = [], []
    raw_orders = [r for r in (answer.get("orders") or []) if isinstance(r, dict)]
    for raw in raw_orders:
        action, code = _action_of(raw), str(raw.get("code", ""))
        if action == "买入" and code not in quotes:
            dropped.append({"action": action, "code": code, "why": "盘中异动票归趋势交易员，这位不做" if code in handed else "不在候选里"})
        elif action == "卖出" and code not in held:
            dropped.append({"action": action, "code": code, "why": "没有这只持仓"})
        elif action == "买入" and (ctx["quotes"][code].get("rise20") or 0) >= config.FLEX_MAX_RISE20:
            dropped.append({"action": action, "code": code, "why": f"高位不买：近 20 个交易日已涨 {ctx['quotes'][code]['rise20']:.0f}%"})
        elif (action == "买入" and code in quotes) or (action == "卖出" and code in held):
            if len(orders) >= MAX_ORDERS_PER_CYCLE:
                dropped.append({"action": action, "code": code, "why": "超过每轮上限"})
                continue
            orders.append({"id": f"{ctx['snapshotId']}-{len(orders) + 1}", "action": action, "code": code,
                           "pct": _pct_of(raw) if action == "买入" else _number(raw.get("pct")), "stop": _number(raw.get("stop")), "ref_price": _number(raw.get("ref_price")),
                           "invalid": _text(raw.get("invalid"), 80), "mode": raw.get("mode"), "pattern": _text(raw.get("pattern"), 20),
                           "reason": _text(next((raw[k] for k in ("reason", "理由", "why", "原因") if raw.get(k)), ""), 120)})
        else:
            dropped.append({"action": str(action), "code": code, "why": "动作不是买入/卖出",
                            "keys": {k: str(v)[:20] for k, v in raw.items() if k not in ("reason", "invalid")}})
    a = answer.get("analysis") if isinstance(answer.get("analysis"), dict) else {}
    return {"snapshotId": ctx["snapshotId"], "analysis": {"market": _text(a.get("market"), 300), "stage": _text(a.get("stage"), 10),
                                                         "plan": _text(a.get("plan"), 300)},
            "orders": orders, "rawOrders": len(raw_orders), "dropped": dropped, "next": _text(answer.get("next"), 200),
            "picks": _picks(answer, quotes | held, {o["code"] for o in orders if o["action"] == "买入"})}


def _picks(answer: dict, known: set, bought: set) -> list[dict]:
    """推荐 / 不推荐清单（dj 2026-11-09：参考着手动买）：只留候选或持仓里的代码，已经下了买单的不重复，最多 MAX_PICKS 只。"""
    out, seen = [], set(bought)
    for p in answer.get("picks") or []:
        if not isinstance(p, dict):
            continue
        code = str(p.get("code", ""))
        verdict = "推荐" if str(p.get("verdict")) == "推荐" else "不推荐" if str(p.get("verdict")) == "不推荐" else None
        if code in known and code not in seen and verdict:
            seen.add(code)
            out.append({"code": code, "verdict": verdict, "why": _text(p.get("why"), 60)})
    return out[:MAX_PICKS]


def _quote_tag(code: str, ctx: dict) -> str:
    q = ctx.get("quotes", {}).get(code) or {}
    tag = f"{code} {q.get('name', '')}".rstrip()
    if q.get("price"):
        tag += f" {q['price']}"
    if isinstance(q.get("pct"), (int, float)):
        tag += f" {q['pct']:+.1f}%"
    return tag


def picks_text(result: dict, ctx: dict, with_targets: bool) -> list[str]:
    """推送里的三段：推荐（买单 + 推荐）、不推荐、规则策略标的（只附在一号的推送里，不重复推两遍）。"""
    rec = [f"✅ {_quote_tag(o['code'], ctx)}｜{o.get('reason') or ''}" + (f"（止损 {o['stop']}）" if o.get("stop") else "")
           for o in result["orders"] if o["action"] == "买入"]
    rec += [f"✅ {_quote_tag(p['code'], ctx)}｜{p['why']}" for p in result.get("picks", []) if p["verdict"] == "推荐"]
    rej = [f"❌ {_quote_tag(p['code'], ctx)}｜{p['why']}" for p in result.get("picks", []) if p["verdict"] == "不推荐"]
    out = ["【推荐】\n" + ("\n".join(rec) or "无"), "【不推荐】\n" + ("\n".join(rej) or "无")]
    targets = ctx.get("strategyTargets") or []
    if with_targets and targets:
        out.append("【规则策略标的】\n" + "\n".join(f"{t.get('strategy')} {t.get('code')} {t.get('name', '')}｜{t.get('plan', '')}" for t in targets))
    return out


# ---------- 一轮 ----------
def _order_line(o: dict, ctx: dict) -> str:
    name = ctx.get("quotes", {}).get(o["code"], {}).get("name", "")
    if o["action"] == "买入":
        return (f"▶ 买入 {o['code']} {name}｜{o.get('pct') or 0:.0%} 仓｜止损 {o.get('stop')}｜{o.get('mode')}·{o.get('pattern')}\n"
                f"  理由：{o['reason']}\n  错了的信号：{o['invalid']}")
    return f"▶ 卖出 {o['code']} {name}\n  理由：{o['reason']}"


def run_cycle(now: datetime, slot: str, *, gather, ask, orders_path: Path | None = None, log_path: Path | None = None,
              state_path: Path | None = None, notifier=None, clock=None, trader: Trader | None = None) -> dict:
    """gather(now, slot) -> ctx；ask(system, prompt, schema) -> 回答 dict。失败（超时、格式错）直接抛出，不记为已跑，窗口内还会再试。
    trader 选哪位交易员（提示词、订单文件、日志、时点记录各自独立）；不传就是一号。"""
    from dashboard import manager
    trader = trader or TRADERS["aiflex"]
    orders_path, log_path, state_path = orders_path or trader.orders_path, log_path or trader.log_path, state_path or trader.state_path
    ctx = gather(now, slot)
    directive = manager.directive_for(trader.sid, now, manager.DIRECTIVE_PATH)   # 交易台经理今天对我的指令（没有就照旧）
    if directive:
        ctx["manager"] = {k: directive.get(k) for k in ("regime", "buy", "maxPct", "why", "slot")}
    if directive and not directive["buy"] and not ctx.get("account", {}).get("positions"):
        # 经理关了开仓、手上又没票：这一轮没什么可想的，不花一次 DeepSeek
        skipped = f"经理关了开仓（{directive['regime']}），空仓不调用"
        result = {"snapshotId": ctx["snapshotId"], "analysis": {"market": "", "stage": "—", "plan": ""}, "orders": [],
                  "rawOrders": 0, "dropped": [], "next": "", "skipped": skipped}
    else:
        system, prompt = build_prompt(ctx, trader)
        if directive:
            system += (f"\n\n【交易台经理的指令（{directive['slot']}）】今天判断为{directive['regime']}；你今天"
                       + (f"可以开新仓，单笔不超过 {directive['maxPct']:.0%}" if directive["buy"] else "不开新仓，只管持仓（该卖就卖）")
                       + f"。理由：{directive['why']}。买单超出指令会被程序丢弃或压到上限。")
        result = manager.apply_directive(validate_answer(ask(system, prompt, answer_schema(ctx)), ctx), directive)
    result["manager"] = ctx.get("manager")
    result["candidates"] = len(ctx.get("quotes") or {})
    Path(orders_path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(orders_path).with_suffix(".tmp")
    wrote_at = clock() if clock else now          # 订单有效期从写入这一刻算：模型一轮可能要几分钟
    tmp.write_text(json.dumps({"cycleId": ctx["snapshotId"], "ts": wrote_at.isoformat(timespec="seconds"), "orders": result["orders"]},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(orders_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "slot": slot, **result}, ensure_ascii=False) + "\n")
    mark_done(slot, now, state_path)
    if notifier is not None and not result.get("skipped"):
        a = result["analysis"]
        acct = ctx.get("account", {})
        orders = "\n".join(_order_line(o, ctx) for o in result["orders"]) or "本轮不动（没把握就空着）"
        if result["dropped"]:
            orders += "\n⚠ 被丢弃：" + "；".join(f"{d['action']}{d['code']}（{d['why']}）" for d in result["dropped"])
        m = result.get("manager")
        boss = [f"【经理】{m['regime']}｜{'开新仓，单笔≤' + format(m['maxPct'] or 0, '.0%') if m['buy'] else '关新仓，只管持仓'}｜{m['why']}"] if m else []
        sections = [f"{now:%H:%M}｜阶段：{a['stage']}", *boss, *picks_text(result, ctx, trader.sid == "aiflex"),
                    "【判断】" + a["market"], "【计划】" + a["plan"], "【订单】\n" + orders,
                    f"【账户】净值 {acct.get('equity', 0):,.0f}｜现金 {acct.get('cash', 0):,.0f}｜持仓 "
                    f"{len(acct.get('positions', []))} 只｜{acct.get('guards', '')}",
                    "订单由 daemon 按实时行情执行（10 分钟内有效，价格偏离参考价超过 2% 不追），成交另推；模拟账户，不是真实委托。"]
        try:
            notifier.send(f"🧭 {trader.label}｜{slot}｜{a['stage']}", "\n\n".join(sections))
            notifier.flush(timeout=55)
        except Exception:                                          # noqa: BLE001
            pass
    out = {"slot": slot, "orders": len(result["orders"]), "stage": result["analysis"]["stage"]}
    if result.get("skipped"):
        out["skipped"] = result["skipped"]
    return out


def ask_workbuddy(system: str, prompt: str, schema: dict, model: str | None = None, timeout: float = 240.0,
                  runner=subprocess.run) -> dict:
    """同 workbuddy_bridge 的调用方式：提示词走 stdin，JSON Schema 约束输出。"""
    from dashboard import workbuddy_bridge as wb
    cli = Path(os.environ.get("WORKBUDDY_CLI", wb.DEFAULT_CLI))
    command = [wb._node_executable(), str(cli), "--print", "--model", model or wb.DEFAULT_MODEL, "--tools", "", "--max-turns", "1",
               "--no-session-persistence", "--output-format", "text", "--json-schema",
               json.dumps(schema, ensure_ascii=False, separators=(",", ":")), "--system-prompt", system]
    try:
        done = runner(command, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"灵活交易判断超时（{timeout:g} 秒）") from exc
    if done.returncode != 0 or not (done.stdout or "").strip():
        raise RuntimeError(f"灵活交易调用失败（{done.returncode}）：{(done.stderr or done.stdout or '').strip()[-300:]}")
    return wb._parse_answer(done.stdout)


# ---------- 给 DeepSeek 的现场 ----------
MAX_CANDIDATES = 80


def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _account_state(db_path: Path, quotes: dict, day, sid: str = "aiflex") -> dict:
    """账户现状（只读库）：净值、现金、持仓（含买入价、AI 自己设的止损位）、护栏状态。库不在 / 读不出来就当空账户。"""
    import sqlite3
    from jevq.ledger import Ledger
    from jevq.strategies.flex import AiflexStrategy
    capital = config.STRATEGY_CAPITAL.get(sid, config.FLEX_CAPITAL)          # 经理账户是一万，不是交易员的 12 万
    empty = {"equity": capital, "cash": capital, "positions": [], "guards": "护栏正常（空仓）"}
    if not Path(db_path).exists():
        return empty
    try:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return empty
    try:
        ledger = Ledger(conn, init=False)
        strat = AiflexStrategy(conn, ledger, None, sid=sid)
        account = strat.account
        prices = {c: q.price for c, q in quotes.items()}
        equity, cash = ledger.equity(account, prices), ledger.cash(account)
        positions = []
        for p in ledger.positions(account):
            price = prices.get(p.code, p.buy_price)
            positions.append({"code": p.code, "shares": p.shares, "buyDate": p.buy_date.isoformat(), "buyPrice": p.buy_price,
                              "price": price, "pnlPct": round((price / p.buy_price - 1) * 100, 2), "stop": strat.effective_stop(p),
                              "canSellToday": p.buy_date < day})
        row = conn.execute("SELECT value FROM equity WHERE account=? AND date<? ORDER BY date DESC LIMIT 1",
                           (account, day.isoformat())).fetchone()
        prev = row[0] if row else capital
        losses = 0
        for _, pnl in reversed(strat._results()):
            if pnl >= 0:
                break
            losses += 1
        blocked = strat._blocked(equity, day)
        guards = (f"当日 {(equity / prev - 1):+.1%}（亏到 -{config.FLEX_DAY_LOSS:.0%} 停手）；连亏 {losses} 笔（{config.FLEX_LOSS_STREAK} 笔停手）"
                  + (f"；【{blocked}】" if blocked else "；护栏正常"))
        return {"equity": round(equity, 2), "cash": round(cash, 2), "positions": positions, "guards": guards}
    except (sqlite3.Error, ValueError, KeyError, TypeError):      # TypeError：账户还没开户（daemon 没启动过），当空账户
        return empty
    finally:
        conn.close()


def high_board_state(rows: list[dict], quotes: dict) -> dict:
    """昨天 ≥3 连板的主板票今天怎么走（2026-11-04 实例：高标全部低开低走 -8%~-11%，资金换去低位科技）。
    走弱 = 当前涨幅 <0；一半以上走弱就判「高标走弱」，接力/补涨这类押旧题材延续的打法这时候要收手。"""
    from jevq.rules.themes import parse_boards
    picked = sorted(((parse_boards(r.get("high_days")), r["code"]) for r in rows
                     if str(r.get("code", "")).startswith(("60", "00")) and parse_boards(r.get("high_days")) >= 3), reverse=True)
    items = [{"code": c, "name": quotes[c].name, "boards": n, "openPct": quotes[c].open_pct, "pct": quotes[c].pct}
             for n, c in picked if c in quotes][:8]
    weak = sum(1 for i in items if i["pct"] < 0)
    if not picked:
        verdict = "无高标"
    elif not items:
        verdict = "无数据"
    else:
        verdict = "高标走弱" if weak * 2 >= len(items) else "高标强势"
    return {"count": len(picked), "weak": weak, "verdict": verdict, "items": items}


MOVER_MIN_AMOUNT = 2e8          # 成交额 ≥2 亿才算有资金在动
MOVER_PCT = (2.0, 8.0)          # 涨幅 [2%, 8%)：已经走强、还没封板，追得进去
MOVER_BY_AMOUNT = 20
MOVER_BY_PCT = 10


def pick_movers(quotes: dict, now: datetime) -> list[str]:
    """盘中异动：沪深主板、非 ST、今天有行情、涨幅 2~8% 没封板、成交额 ≥2 亿，成交额前 20 + 涨幅前 10。
    不要求是首板或涨停股——候选只来自昨日涨停会让 AI 永远在首板里挑（dj 2026-11-05：别老是盯着首板）。"""
    ok = [(c, q) for c, q in quotes.items()
          if c.startswith(("60", "00")) and "ST" not in q.name.upper() and q.price > 0 and not getattr(q, "suspended", False)
          and q.ts.date() == now.date() and MOVER_PCT[0] <= q.pct < MOVER_PCT[1] and q.price < q.limit_up
          and q.amount >= MOVER_MIN_AMOUNT]
    by_amount = [c for c, q in sorted(ok, key=lambda x: x[1].amount, reverse=True)[:MOVER_BY_AMOUNT]]
    by_pct = [c for c, q in sorted(ok, key=lambda x: x[1].pct, reverse=True)[:MOVER_BY_PCT]]
    return list(dict.fromkeys(by_amount + by_pct))


_market_memo: tuple[float, dict] = (0.0, {})


def _scan_market() -> dict:
    """全市场行情（约 22 秒）。两位交易员背靠背跑时共用 90 秒内的同一份，不扫两遍。"""
    global _market_memo
    if time.monotonic() - _market_memo[0] < 90 and _market_memo[1]:
        return _market_memo[1]
    from dashboard.market_snapshot import MarketSnapshotService
    svc = MarketSnapshotService()
    svc.refresh_now()
    _market_memo = (time.monotonic(), svc.all_quotes())
    return _market_memo[1]


def _positions(db_path: Path, codes: list[str], day) -> dict[str, dict]:
    """每只候选的位置（rise5 / rise20 / fromLow60，jevq.data.closes.position_stats）。读不到就当没有，不影响别的字段。"""
    import sqlite3
    from jevq.data.closes import position_stats
    if not codes:
        return {}
    try:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=3)
        try:
            return position_stats(conn, codes, day)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return {}


def gather_context(now: datetime, slot: str, *, db_path: Path = DB_PATH, snapshot_path: Path = SNAPSHOT_PATH,
                   fetch_quotes=None, fetch_rows=None, fetch_market=None, log_path: Path | None = None,
                   theme_cache: Path = THEME_CACHE, trader: Trader | None = None, skip_src: tuple | None = None) -> dict:
    """市场状态 + 题材梯队（带成员代码）+ 所有策略的标的 + 候选实时行情 + 账户 + 今天前几轮的想法。
    任何一块取不到都只是缺那一块，不整轮失败。trader 决定读哪个账户和哪份日志；不传就是一号。"""
    trader = trader or TRADERS["aiflex"]
    log_path = log_path or trader.log_path
    from dashboard import subscription_bridge as sb
    from jevq.data import tencent, ztpool
    fetch_quotes = fetch_quotes or tencent.fetch_quotes
    fetch_rows = fetch_rows or ztpool.fetch_reasons
    day = now.date()
    snap = _read_json(snapshot_path)
    try:
        fresh = abs((now - datetime.fromisoformat(snap["createdAt"])).total_seconds()) <= 900
    except (KeyError, ValueError):
        fresh = False
    snap = snap if fresh else {}
    pctx = snap.get("portfolioContext") or {}
    breadth = {k: v for k, v in ((snap.get("context") or {}).get("marketBreadth") or {}).items() if isinstance(v, (int, float, str, bool))}
    ladder = sb._theme_ladder(now, fetch_rows, sb._prev_trading_day(db_path, day), path=theme_cache, with_members=True)
    try:
        targets = sb._strategy_targets(now, fetch_quotes)
    except Exception:                                            # noqa: BLE001
        targets = []

    account = _account_state(db_path, {}, day, trader.sid)
    try:
        prev_rows = list(fetch_rows(sb._prev_trading_day(db_path, day)) or [])
    except Exception:                                            # noqa: BLE001
        prev_rows = []
    from jevq.rules.themes import parse_boards
    high_codes = [r["code"] for r in prev_rows if parse_boards(r.get("high_days")) >= 3][:8]
    src: dict[str, str] = {}

    def add(codes_, tag):
        for c in codes_:
            src.setdefault(c, tag)
    add([p["code"] for p in account["positions"]], "持仓")
    add(high_codes, "高标")
    add([t["code"] for t in targets], "策略")
    add([w["code"] for w in pctx.get("boardWatch") or []], "扫板")
    for t in ladder[:6]:
        add([m["code"] for m in t.get("members", [])[:5]], "梯队")
    try:
        add(pick_movers((fetch_market or _scan_market)(), now), "异动")
    except Exception:                                            # noqa: BLE001  全市场扫描失败只是少了异动股
        pass
    skip = trader.skip_src if skip_src is None else skip_src        # 经理和策略席借用这份现场时传 ()：他们要看全市场，分工只管交易员
    handed = [c for c, tag in src.items() if tag in skip and c.startswith(("60", "00"))]
    codes = [c for c, tag in src.items() if c.startswith(("60", "00")) and tag not in skip][:MAX_CANDIDATES]
    try:
        quotes = fetch_quotes(codes) if codes else {}
    except Exception:                                            # noqa: BLE001
        quotes = {}
    boards = {m["code"]: m["boards"] for t in ladder for m in t.get("members", [])}
    positions = _positions(db_path, list(quotes), day)
    payload = {c: {"name": q.name, "price": q.price, "pct": q.pct, "openPct": q.open_pct, "amount亿": round(q.amount / 1e8, 2),
                   "turnover": q.turnover, "volumeRatio": q.volume_ratio, "limitUp": q.limit_up, "sealed": q.price >= q.limit_up,
                   "yesterdayBoards": boards.get(c), "src": src.get(c)} for c, q in quotes.items() if q.price > 0}
    from dashboard import backtester
    payload = backtester.annotate(payload, positions)        # 回测员：前期位置 + 历史同档怎么走
    account = _account_state(db_path, quotes, day, trader.sid)
    notes = []
    try:
        for line in Path(log_path).read_text(encoding="utf-8").splitlines()[-12:]:
            row = json.loads(line)
            if str(row.get("ts", ""))[:10] == day.isoformat():
                notes.append({"slot": row.get("slot"), "stage": (row.get("analysis") or {}).get("stage"),
                              "plan": (row.get("analysis") or {}).get("plan"),
                              "orders": [f"{o['action']}{o['code']}" for o in row.get("orders", [])]})
    except (OSError, ValueError):
        pass
    return {"time": f"{now:%H:%M}", "slot": slot, "snapshotId": f"FLEX-{now:%Y%m%d}-{slot.replace(':', '')}",
            "market": {"breadth": breadth, "sentiment": (pctx.get("memory") or {}).get("sentiment")},
            "highBoards": high_board_state(prev_rows, quotes),
            "themeLadder": ladder, "strategyTargets": targets, "quotes": payload, "account": account, "notes": notes[-6:],
            "handedOff": handed}
