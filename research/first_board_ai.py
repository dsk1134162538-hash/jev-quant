"""打首板 AI 对照实验（dj 2026-11-02「都试一下」）：在 first_board_history 的 108 天样本上，按模拟收益比较
  base   —— 不选：摸 9% 全买
  stat   —— 纯统计打分（训练段分桶均值相加，挑分最高的 3 只，分数不够就空仓）
  v1     —— 模型，中性提示（可空仓，最多点 3 只）
  v2     —— 模型，「闸门默认开」提示（除非明确退潮，必须点 1-3 只）
  v3     —— v1 + 训练段复盘出的经验条（只在验证段跑）
  vote   —— v1 两个模型都点名的票（kimi 太烧积分后只剩 flash + v4-pro）
训练段 = 前 72 天，验证段 = 后 36 天；收益口径：摸 9% 买入、次日开盘卖、扣 0.15% 费用、点名票等权、空仓日 0。
防作弊：日期、代码、名称全抹掉，候选用 S1… 编号。用法：python -m research.first_board_ai [--variants v1,v2,v3]
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

from research import first_board_history as fb

OUT = fb.ART / "ai_runs.jsonl"
REPORT = fb.ART / "report.json"
MODELS = ("deepseek-v4.1-flash", "deepseek-v4-pro")      # kimi-k3.1-pro 太烧积分（dj 2026-11-02），只留跑过的部分做参考
TRAIN_DAYS = 72
MAX_CANDS = 80
FEATURES = ("openGap%", "pos5%", "pos20%", "floatMcap亿", "prevTurnover%", "prevAmount亿", "holders万",
            "同行业今日摸9%家数(盘中逐步可见)")
EDGES = {"openGap%": [-11, -2, 0, 2, 4, 6, 9, 11], "pos5%": [-50, -5, 0, 5, 10, 20, 100],
         "pos20%": [-60, -10, 0, 10, 20, 40, 300], "floatMcap亿": [0, 30, 50, 100, 200, 500, 99999],
         "prevTurnover%": [0, 1, 2, 4, 8, 15, 100], "prevAmount亿": [0, 1, 2, 5, 10, 1000],
         "holders万": [0, 2, 4, 6, 8, 100], "同行业今日摸9%家数(盘中逐步可见)": [1, 2, 3, 5, 8, 100]}

SYSTEM = ("你是 A 股超短线交易员，只做沪深主板首板。规则：盘中个股涨到 9% 时程序扫板买入，次日开盘卖出。"
          "历史上不加挑选地买，平均每笔亏约 0.4%——赚钱全靠挑出能封住、次日有溢价的票。"
          "下面是某个历史交易日 09:26 竞价结束时能看到的数据（日期和个股已匿名），今天会摸到 9% 的票已经列出。"
          "只用给的数据，不要猜是哪天哪只。只输出一个 JSON 对象，不要 Markdown 或其他说明。")
GATE_NEUTRAL = "先判断今天打不打（开闸/空仓）；开闸就从候选里点名最多 3 只最有把握的，空仓就 picks 为空。"
GATE_OPEN = ("默认开闸：除非数据显示明确退潮（例如昨日炸板率很高且昨日涨停股今天竞价普遍低开、跌停扎堆），"
             "否则必须点名 1-3 只；明确退潮才空仓。")


def split(days):
    return days[:TRAIN_DAYS], days[TRAIN_DAYS:]


def anon(data: dict, day: str, seed: int = 0) -> tuple[dict, dict]:
    cands = sorted(data["cands"][day], key=lambda c: -c["prevAmount亿"])[:MAX_CANDS]
    random.Random(f"{day}-{seed}").shuffle(cands)          # 打乱顺序，不让编号泄露成交额排序
    view, key = {}, {}
    for n, c in enumerate(cands, 1):
        sid = f"S{n}"
        key[sid] = c["code"]
        view[sid] = {k: c[k] for k in ("industry", *FEATURES)}
    return {"市场（昨天收盘以前 + 今天竞价）": data["market"][day], "今天会摸到9%的候选": view}, key


def prompt(view: dict, variant: str, lessons: list[str] | None = None) -> str:
    text = (GATE_OPEN if variant == "v2" else GATE_NEUTRAL) + "\n"
    if lessons:
        text += "复盘攒下的经验（训练段统计得出，带验证结果）：\n" + "\n".join(f"- {x}" for x in lessons) + "\n"
    return (text + "数据：" + json.dumps(view, ensure_ascii=False) + "\n输出 {\"gate\": \"开闸\"或\"空仓\", "
            "\"confidence\": 0到1, \"reason\": \"带数字的理由\", \"picks\": [{\"id\": \"S编号\", \"why\": \"简短理由\"}]}，"
            "picks 最多 3 个。")


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["gate", "confidence", "reason", "picks"],
          "properties": {"gate": {"enum": ["开闸", "空仓"]}, "confidence": {"type": "number"},
                         "reason": {"type": "string", "maxLength": 400},
                         "picks": {"type": "array", "maxItems": 3, "items": {
                             "type": "object", "additionalProperties": False, "required": ["id", "why"],
                             "properties": {"id": {"type": "string"}, "why": {"type": "string", "maxLength": 120}}}}}}


def ask(view, variant, model, lessons=None, timeout=420, runner=subprocess.run) -> dict:
    from dashboard import workbuddy_bridge as wb
    cmd = [wb._node_executable(), str(Path(wb.os.environ.get("WORKBUDDY_CLI", wb.DEFAULT_CLI))), "--print", "--model", model, "--tools", "",
           "--max-turns", "1", "--no-session-persistence", "--output-format", "text",
           "--json-schema", json.dumps(SCHEMA, ensure_ascii=False, separators=(",", ":")), "--system-prompt", SYSTEM]
    r = runner(cmd, input=prompt(view, variant, lessons), capture_output=True, text=True, encoding="utf-8",
               errors="replace", timeout=timeout, check=False)
    if r.returncode != 0:
        raise wb.WorkBuddyError(f"返回码 {r.returncode}：{(r.stderr or '')[-200:]}")
    ans = wb._parse_answer(r.stdout)
    ids = set(view["今天会摸到9%的候选"])
    picks = [p.get("id") for p in ans.get("picks") or []]
    if ans.get("gate") not in ("开闸", "空仓") or len(picks) > 3 or any(p not in ids for p in picks):
        raise wb.WorkBuddyError("答案不合格式")
    return ans


def read_runs(path: Path = OUT) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def run_variant(data, days, variant, models=MODELS, lessons=None, workers=10, asker=ask, out=OUT, seed=0) -> None:
    done = {(r["day"], r["model"], r["variant"]) for r in read_runs(out) if r.get("ok")}
    jobs = [(d, m) for d in days for m in models if (d, m, variant) not in done]
    lock = threading.Lock()

    def one(job):
        day, model = job
        view, key = anon(data, day, seed)
        t0 = time.monotonic()
        row = {"day": day, "model": model, "variant": variant}
        for _ in range(2):
            try:
                ans = asker(view, variant, model, lessons)
                picks = [key[p["id"]] for p in ans["picks"]] if ans["gate"] == "开闸" else []
                row.update(ok=True, gate=ans["gate"], picks=picks, reason=ans.get("reason", "")[:300],
                           ret=fb.day_result(data["cands"][day], picks))
                row.pop("error", None)
                break
            except Exception as exc:                          # noqa: BLE001
                row.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:160]}")
        row["seconds"] = round(time.monotonic() - t0, 1)
        with lock, out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(day, model, variant, "ok" if row.get("ok") else row["error"], flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, jobs))


# ---------- 统计基准 ----------

def stat_model(data, train_days) -> dict:
    """每个特征分桶的训练段平均收益（减去总均值），打分 = 各特征桶得分之和。"""
    rows = [c for d in train_days for c in data["cands"][d]]
    base = mean(c["ret_open%"] for c in rows)
    table = {}
    for f, edges in EDGES.items():
        table[f] = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            vals = [c["ret_open%"] for c in rows if c.get(f) is not None and lo <= c[f] < hi]
            # 样本少的桶往总均值收缩，免得几十个样本的桶主导
            w = len(vals) / (len(vals) + 200)
            table[f].append((lo, hi, round(w * ((mean(vals) if vals else base) - base), 3)))
    return {"base": base, "table": table}


def stat_score(model, c) -> float:
    s = 0.0
    for f, buckets in model["table"].items():
        v = c.get(f)
        if v is None:
            continue
        for lo, hi, val in buckets:
            if lo <= v < hi:
                s += val
                break
    return s


def stat_picks(model, cands, threshold) -> list[str]:
    scored = sorted(((stat_score(model, c), c["code"]) for c in cands), reverse=True)
    return [code for s, code in scored[:3] if s >= threshold]


# ---------- 经验条（训练段） ----------

def lessons_from_train(data, train_days, runs, model="deepseek-v4-pro", runner=subprocess.run) -> list[str]:
    from dashboard import ai_score
    stat = stat_model(data, train_days)
    picks_review = []
    for r in runs:
        if r.get("ok") and r["variant"] == "v1" and r["day"] in train_days:
            picks_review.append({"model": r["model"], "gate": r["gate"], "ret": r["ret"],
                                 "dayAvgIfBuyAll": round(mean(c["ret_open%"] for c in data["cands"][r["day"]]), 2),
                                 "market": data["market"][r["day"]], "reason": r["reason"][:150]})
    cases = {"训练段候选分桶（每桶平均收益减总均值，已按样本量收缩）": stat["table"],
             "训练段总均值%": round(stat["base"], 3), "模型逐日判断与结果（ret=点名票次日开盘收益%，空仓为0）": picks_review[:150]}
    lessons = ai_score.ask_lessons(ai_score.review_prompt([cases], [], "训练段 72 天统计 + 模型判断"), model=model,
                                   runner=runner)
    return [f"{x['text']}（{x['evidence']}）" for x in lessons if x["active"]]


# ---------- 报告 ----------

def report(data, runs) -> dict:
    train, test = split(data["days"])
    out = {"days": {"train": [train[0], train[-1], len(train)], "test": [test[0], test[-1], len(test)]}}

    def stats(ds, fn):
        return fb.equity_stats([fn(d) for d in ds])
    out["base"] = {"train": stats(train, lambda d: fb.day_result(data["cands"][d], [c["code"] for c in data["cands"][d]])),
                   "test": stats(test, lambda d: fb.day_result(data["cands"][d], [c["code"] for c in data["cands"][d]]))}
    sm = stat_model(data, train)
    best = max((0.0, 0.05, 0.1, 0.2, 0.3),
               key=lambda t: fb.equity_stats([fb.day_result(data["cands"][d], stat_picks(sm, data["cands"][d], t))
                                              for d in train])["total%"])
    out["stat"] = {"threshold": best,
                   "train": stats(train, lambda d: fb.day_result(data["cands"][d], stat_picks(sm, data["cands"][d], best))),
                   "test": stats(test, lambda d: fb.day_result(data["cands"][d], stat_picks(sm, data["cands"][d], best)))}
    by = defaultdict(dict)
    for r in runs:
        if r.get("ok"):
            by[(r["variant"], r["model"])][r["day"]] = r
    for (variant, model), rows in sorted(by.items()):
        out[f"{variant}:{model}"] = {
            seg: {**stats([d for d in ds if d in rows], lambda d: rows[d]["ret"]),
                  "missing": sum(d not in rows for d in ds)}
            for seg, ds in (("train", train), ("test", test)) if any(d in rows for d in ds)}
    v1 = {m: by.get(("v1", m), {}) for m in MODELS}
    vote_days = [d for d in data["days"] if all(d in v1[m] for m in MODELS)]

    def vote(d):
        cnt = defaultdict(int)
        for m in MODELS:
            for code in v1[m][d]["picks"]:
                cnt[code] += 1
        return fb.day_result(data["cands"][d], [c for c, n in cnt.items() if n >= 2])
    flash5 = [by.get((f"v3s{k}", "deepseek-v4.1-flash"), {}) for k in range(5)]
    f5_days = [d for d in test if all(d in f for f in flash5)]
    if f5_days:
        def vote5(d):
            cnt = defaultdict(int)
            for f in flash5:
                for code in f[d]["picks"]:
                    cnt[code] += 1
            return fb.day_result(data["cands"][d], [c for c, n in cnt.items() if n >= 3])
        out["flash×5 投票(>=3)"] = {"test": stats(f5_days, vote5)}
        out["flash×5 单次平均"] = {"test": {"avgPerTrade%": round(mean(
            stats(f5_days, lambda d, f=f: f[d]["ret"])["avgPerTrade%"] or 0 for f in flash5), 3)}}
    out["vote(v1,>=2)"] = {seg: stats([d for d in ds if d in vote_days], vote) for seg, ds in (("train", train), ("test", test))}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variants", default="v1,v2,v3")
    p.add_argument("--models", default=",".join(MODELS))
    p.add_argument("--workers", type=int, default=10)
    a = p.parse_args(argv)
    data = fb.load()
    train, test = split(data["days"])
    models = tuple(a.models.split(","))
    variants = a.variants.split(",")
    if "v1" in variants:
        run_variant(data, data["days"], "v1", models, workers=a.workers)
    if "v2" in variants:
        run_variant(data, test, "v2", models, workers=a.workers)
    if "flash5" in variants:
        lessons = json.loads((fb.ART / "train_lessons.json").read_text(encoding="utf-8"))
        for k in range(5):           # 同一份经验条，候选顺序按不同种子打乱，各答一次
            run_variant(data, test, f"v3s{k}", ("deepseek-v4.1-flash",), lessons=lessons, workers=a.workers, seed=k + 1)
    if "v3" in variants:
        lessons_path = fb.ART / "train_lessons.json"
        if lessons_path.exists():
            lessons = json.loads(lessons_path.read_text(encoding="utf-8"))
        else:
            lessons = lessons_from_train(data, train, read_runs())
            lessons_path.write_text(json.dumps(lessons, ensure_ascii=False, indent=2), encoding="utf-8")
        run_variant(data, test, "v3", models, lessons=lessons, workers=a.workers)
    rep = report(data, read_runs())
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
