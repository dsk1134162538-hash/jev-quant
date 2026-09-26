"""一次性对照：最近 N 个自然日里因公告被否决的主账户候选，用两段式新流程重判，打印「旧 → 新」。
用法：.venv/Scripts/python scripts/notice_risk_replay.py [天数，默认 7]
需要环境变量 TYPESAFE_API_KEY。会真实调用 Jev、真实抓正文；结果写入 notice_risk/notice_bodies 缓存（上线后直接复用）。"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevq import db  # noqa: E402
from jevq.config import DB_PATH  # noqa: E402
from jevq.judge.jev import JevClient  # noqa: E402
from jevq.judge.notices import NoticeJudge  # noqa: E402

OLD_NOTICE_REASONS = ("新增减持", "新增质押", "回购", "诉讼仲裁", "拿不准")


def main(days: int = 7):
    conn = db.connect(DB_PATH)
    judge = NoticeJudge(conn, JevClient(os.environ["TYPESAFE_API_KEY"], conn=conn))
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT substr(ts, 1, 10), code, detail FROM decisions "
                        "WHERE ts >= ? AND verdict='reject' ORDER BY ts", (since,)).fetchall()
    seen = set()
    print("| 日期 | 代码 | 旧结论 | 新结论 | 最高疑似 |")
    print("|---|---|---|---|---|")
    for day, code, old in rows:
        if (day, code) in seen or not old.startswith(OLD_NOTICE_REASONS):
            continue
        seen.add((day, code))
        verdict, new = judge.check(code, date.fromisoformat(day))
        top = (judge.last_trace.get("suspects") or [{}])[0]
        extra = (f"{top.get('category')}·{top.get('level')} {top.get('score')} "
                 f"{'已读正文' if top.get('body_read') else '仅标题'}") if top else ""
        print(f"| {day} | {code} | {old[:40]} | {verdict}：{new[:40]} | {extra} |")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
