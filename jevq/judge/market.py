from jevq.judge.jev import JevError, score_label

SENTIMENT_LEVELS = ["冰点", "偏弱", "正常", "火热"]
OVERSEAS_LEVELS = ["偏弱", "中性", "偏强"]


class MarketJudge:
    def __init__(self, jev):
        self.jev = jev

    def sentiment(self, stats: dict) -> str | None:
        return self._score(stats, "A股短线情绪当前处于什么状态？（参考涨跌停家数、炸板率、连板高度、成交额和涨跌家数）",
                           SENTIMENT_LEVELS)

    def overseas(self, pcts: dict) -> str | None:
        return self._score(pcts, "这些隔夜外围市场涨跌幅（%）对今天A股开盘的影响偏向？", OVERSEAS_LEVELS)

    def _score(self, state: dict, instructions: str, levels: list[str]) -> str | None:
        if not state:
            return None
        try:
            ans = self.jev.ask(state, {"level": {"type": "score", "instructions": instructions,
                                                 "criteria": levels}})["level"]
        except JevError:
            return None
        return score_label(ans)
