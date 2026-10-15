"""超跌主账户买入前，Claude 按板块和新闻否决冷门板块（用户 2026-10-15：「冷门板块不要买，要结合新闻消息来选板块」）。

一次给它排名靠前的几只候选：所属板块、板块 5 日涨跌、个股今天/5 日涨跌，加上隔夜消息催化和最近的 7x24 快讯。
它只能否决（keep=false），不能加票；拿不准要保留。没答上（超时、报错、回答不能用）→ None，调用方按规则照买。
新闻没有历史，回测不了；影子账户不过这一关，用来对照。
"""
from collections.abc import Callable
from datetime import datetime

from jevq import config
from jevq.judge.claude_cli import ClaudeCLI

SYSTEM = ("你是 A 股短线交易员，做超跌反弹：大盘普跌时买跌下来的票，拿 1~3 天，赌它跟着板块反弹。"
          "给你几只候选票、它们所属的板块和板块近 5 日涨跌，以及最近的新闻。逐只判断它所在的板块是不是冷门板块："
          "长期没有题材炒作、近期资金和新闻都不关注、大盘反弹时也弹不起来的（例如白酒），否决；"
          "有消息催化、近期有资金关注、反弹弹性大的，保留。只看板块冷热，个股公告风险另有人查，不用管。拿不准就保留。"
          "只输出一行 JSON：{\"items\": [{\"code\": \"六位代码\", \"keep\": true 或 false, \"reason\": \"一句话\"}]}")


def candidate_line(q, ret5: float | None, sectors: list[str], sret5: dict[str, float]) -> str:
    secs = sorted(sectors, key=lambda n: sret5.get(n, 99))[:6]
    shown = "、".join(f"{n}({sret5[n]:+.1f}%)" if n in sret5 else n for n in secs) or "（没有板块归属）"
    r5 = "?" if ret5 is None else f"{ret5:+.2f}%"
    return f"{q.code} {q.name}：今天 {q.pct:+.2f}%，5 日 {r5}；所属板块（括号里是板块 5 日涨跌）：{shown}"


class ClaudeSectorVeto:
    def __init__(self, cli: ClaudeCLI, news_provider: Callable[[datetime], list[str]] | None = None,
                 catalyst_provider: Callable[[datetime], list[str]] | None = None):
        self.cli = cli
        self.news_provider = news_provider or (lambda now: [])
        self.catalyst_provider = catalyst_provider or (lambda now: [])
        self.last_trace: dict | None = None

    def judge(self, lines: dict[str, str], now: datetime, timeout: float | None = None) -> dict[str, tuple[bool, str]] | None:
        """lines：代码 → candidate_line。返回 代码 → (保留?, 理由)；回答里没提到的代码按保留。没答上 → None。"""
        news, catalyst = self._safe(self.news_provider, now), self._safe(self.catalyst_provider, now)
        user = (f"现在 {now:%Y-%m-%d %H:%M}。\n候选：\n" + "\n".join(lines.values())
                + "\n\n隔夜消息催化（题材：相关新闻条数和标题）：\n" + ("\n".join(catalyst) or "（暂无）")
                + "\n\n最近的 7x24 快讯：\n" + ("\n".join(news) or "（暂无）"))
        got = self.cli.ask_json(SYSTEM, user, timeout=timeout or config.CLAUDE_JUDGE_TIMEOUT)
        self.last_trace = {"cli": getattr(self.cli, "last_trace", None), "answer": got,
                           "candidates": list(lines.values()), "news": len(news), "catalyst": len(catalyst)}
        items = got.get("items") if isinstance(got, dict) else None
        if not isinstance(items, list):
            return None
        out = {code: (True, "Claude 没提到，按保留") for code in lines}
        for it in items:
            if not isinstance(it, dict) or str(it.get("code", "")).zfill(6) not in lines:
                continue
            keep = it.get("keep")
            if not isinstance(keep, bool):
                continue
            out[str(it["code"]).zfill(6)] = (keep, str(it.get("reason", ""))[:120])
        return out

    @staticmethod
    def _safe(fn, now) -> list[str]:
        try:
            return [str(x) for x in (fn(now) or [])]
        except Exception as e:
            print(f"[sector_veto] 新闻读取失败: {e!r}")
            return []
