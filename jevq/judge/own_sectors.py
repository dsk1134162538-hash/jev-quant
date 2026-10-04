"""自建板块用到的三个 Jev 问题：产品→板块映射、主营介绍判断、板块强弱。出错一律返回 None（调用方当没问到）。"""
from jevq.data.own_sectors import NONE_OF_THEM, UNUSABLE
from jevq.judge.jev import JevError, score_label, score_probs

NO_SECTOR = "都不是"
UNUSABLE_LABEL = "无法判断（这是收入类型或业务类别，不是具体产品）"
STRENGTH_LEVELS = ["冰点", "偏弱", "正常", "火热"]


class OwnSectorJudge:
    def __init__(self, jev):
        self.jev = jev

    def map_product(self, product: str, company: str, secs: list[dict]) -> tuple[str, float | None] | None:
        """这一行主营产品属于哪个板块。返回（板块 id | NONE_OF_THEM | UNUSABLE, 置信度）。"""
        criteria = {s["name"]: s["product_def"] for s in secs}
        criteria[NO_SECTOR] = "不属于上面任何一个板块"
        criteria[UNUSABLE_LABEL] = "只是收入类型（如技术授权、销售商品、提供劳务）或金融业务类别，看不出具体产品"
        try:
            ans = self.jev.ask({"公司": company, "主营产品行": product},
                               {"sector": {"type": "choice",
                                           "instructions": "A股公司主营构成里的这一行产品，属于下列哪个板块？只看产品本身是什么。",
                                           "criteria": criteria}})["sector"]
            choice = ans["choice"]
        except (JevError, KeyError, TypeError):
            return None
        by_name = {s["name"]: s["id"] for s in secs}
        if choice in by_name:
            return by_name[choice], ans.get("confidence")
        if choice == UNUSABLE_LABEL:
            return UNUSABLE, ans.get("confidence")
        if choice == NO_SECTOR:
            return NONE_OF_THEM, ans.get("confidence")
        return None

    def judge_intro(self, company: str, intro: dict, sector: dict) -> dict | None:
        """没有可用的按产品构成时，读主营介绍：这家公司的核心产品是否属于该板块。"""
        try:
            ans = self.jev.ask({"公司": company, **intro, "板块": sector["name"], "板块产品定义": sector["product_def"]},
                               {"fit": {"type": "choice",
                                        "instructions": "这家公司的主营业务和核心产品，是否主要就是该板块定义的产品？"
                                                        "占比小的、只是沾边或在布局的，选「否」。",
                                        "criteria": {"是": "核心产品主要就是这个板块的产品",
                                                     "否": "不是，或只是占比较小/沾边/布局中",
                                                     "不确定": "介绍太简略，无法判断"}}})["fit"]
            return {"choice": ans["choice"], "confidence": ans.get("confidence"),
                    "probabilities": ans.get("probabilities")}
        except (JevError, KeyError, TypeError):
            return None

    def strength(self, sector: dict, state: dict) -> tuple[str, dict, float | None] | None:
        """板块当天行情强弱（四档，同市场情绪）。"""
        try:
            ans = self.jev.ask({"板块": sector["name"], "板块产品定义": sector["product_def"], **state},
                               {"level": {"type": "score",
                                          "instructions": "这个板块今天的短线行情处在什么状态？"
                                                          "（参考成员涨幅、强势和涨停家数、成交额、连板高度、是否被晚报点名）",
                                          "criteria": STRENGTH_LEVELS}})["level"]
            return score_label(ans), score_probs(ans), ans.get("confidence")
        except (JevError, KeyError, TypeError):
            return None
