"""问 Jev：规则（jevq.rules.turn）已经看到恐慌后的修复迹象，这是不是可以抄底的情绪修复拐点？
给它今天的全市场广度轨迹、当前明细、情绪标签、隔夜外围和 7x24 快讯。Jev 没答上 → None（这一分钟不确认，下分钟再问）。"""
from datetime import datetime
from time import monotonic

from jevq import config
from jevq.judge.jev import JevError, score_label, score_probs
from jevq.rules.turn import Breadth, describe

# 知识库（面板「知识库」，Telegram @gsxt233）里讲恐慌修复的几条，原样给 Jev 作判断依据。
# 实测 10-09 13:10 的数据：不给这几条 Jev 判修复拐点只有 0.35，给了约 0.6（用户凭盘感当天抄底赚钱）。
KNOWLEDGE = [
    "只要不是退潮期，全市场红盘家数低于 1000 家，次日情绪修复的概率较大；要买在修复的节点（2025-10-20）",
    "连续冰点后盘中恐慌急杀但动能衰竭，是反弹先手点（常在下午或尾盘）（2025-10-05）",
    "情绪修复节点：跌停降到约 5 家以内、炸板率回落；成交额边际放大（2025-12-10）",
    "低吸看恐慌释放后不再创新低（2026-07-27）",
    "可持续的短期反弹：跌停减少、炸板率回落、成交量温和放大（2026-08-13）",
]
LEVELS = ["修复拐点，可以抄底", "下跌中继或弱反抽，不抄底"]
YES = LEVELS[0]
INSTRUCTIONS = ("A股今天盘中出现过恐慌（红盘家数很少、跌停增多），现在全市场广度明显修复、跌停在减少、成交额在放大。"
                "结合今天的广度变化、隔夜外围、7x24 快讯，判断这是恐慌释放后的情绪修复拐点（可以抄底，"
                "第二天大概率还有冲高），还是下跌中继里的弱反抽（抄底会被继续砸）。参考给出的短线经验。")


def trajectory(series: list[Breadth], step: int = 10) -> list[str]:
    """广度轨迹压成每 step 分钟一行（加上最后一条）。"""
    if not series:
        return []
    picks = [b for b in series if b.ts.minute % step == 0]
    if not picks or picks[-1] is not series[-1]:
        picks.append(series[-1])
    return [f"{b.ts:%H:%M} 红盘 {b.up}/{b.total}（{b.up_ratio * 100:.0f}%）跌停 {b.limit_down} 涨停 {b.limit_up} "
            f"成交额较昨日同期 {b.amount_ratio if b.amount_ratio is not None else '–'}%" for b in picks]


class TurnJudge:
    def __init__(self, jev, flash=None):
        self.jev = jev
        self.flash = flash            # jevq.data.news7x24.Flash；None = 不看快讯
        self.last_trace: dict | None = None

    def confirm(self, series: list[Breadth], detail: dict, now: datetime, sentiment: str | None,
                overseas: dict | None) -> float | None:
        """返回 Jev 判「修复拐点」的概率；没答上返回 None。"""
        news, note = [], ""
        if self.flash is not None:
            try:
                news = self.flash.recent(now)
            except Exception as e:      # noqa: BLE001
                note = f"快讯获取失败：{e!r}"
        state = {"时间": now.strftime("%H:%M"), "当前": describe(detail), "情绪标签": sentiment or "未知",
                 "今日广度": trajectory(series), "隔夜外围%": overseas or {}, "短线经验（知识库）": KNOWLEDGE,
                 "7x24快讯": news}
        self.last_trace = {"inputs": state, "prob": None, "note": note}
        try:
            ans = self.jev.ask(state, {"turn": {"type": "score", "instructions": INSTRUCTIONS, "criteria": LEVELS}},
                               deadline=monotonic() + 10.0)["turn"]
            probs = score_probs(ans)
            label = score_label(ans)
        except (JevError, KeyError, TypeError, ValueError) as e:
            self.last_trace["note"] = "；".join(x for x in (note, f"Jev 没答上：{e!r}") if x)
            return None
        prob = round(float(probs.get(YES, 0.0)), 3)
        self.last_trace.update(prob=prob, label=label, probabilities=probs, confidence=ans.get("confidence"),
                               confirmed=prob >= config.TURN_JEV_MIN)
        return prob
