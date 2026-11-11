"""策略交易员 → 经理 → dj（dj 2026-11-10）。

每位策略交易员只负责一个策略：高标接力、弱转强、补涨。三层分工，每层只做一件事：
1. 规则出候选（daemon 盘前写进 strategy_targets.json），代码按竞价高开区间把关，算好证据——不花额度、不编数字；
2. 交易员（DeepSeek）只能对「规则放行的候选」逐只表态：买 / 不买，写理由和否证条件。不能加票；
3. 经理（DeepSeek）只能从各交易员「买」的票里挑「应该做」的，定仓位、写否证条件，汇总成一条微信发给 dj。
这是荐股，不是下单：不碰任何账户、不写订单文件；dj 自己决定买不买。任何一步失败都只退回上一层的原始结果，不丢票。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jevq import config

from dashboard import flex_trader as ft

RUNTIME = ft.RUNTIME
TARGETS_PATH = RUNTIME / "strategy_targets.json"
SUBMIT_PATH = RUNTIME / "desk_submissions.json"
LOG_PATH = RUNTIME / "desk_picks.jsonl"
STATE_PATH = RUNTIME / "desk_state.json"
SLOTS = ("09:26", "10:30", "13:20")
MAIN_BOARD = ("60", "00")
MIN_PCT, MAX_PCT = 0.05, 0.30


@dataclass(frozen=True)
class Strat:
    key: str
    label: str
    gap: tuple
    brief: str          # 给交易员的策略说明（历史均值，不是禁令）


STRATS = {
    "relay": Strat("relay", "高标接力官", config.RELAY_GAP_PCT,
                   f"高标竞价接力：昨日 ≥{config.RELAY_MIN_STREAK} 连板，今天竞价高开 {config.RELAY_GAP_PCT[0]:g}~{config.RELAY_GAP_PCT[1]:g}% "
                   "说明资金接力；开盘价买、下一交易日卖。历史上单笔最差 -23%，高标一旦掉头亏得很快——要看板块、封单和大盘情绪是否还撑得住高度。"),
    "wts": Strat("wts", "弱转强官", config.WTS_GAP_PCT,
                 f"弱转强：昨日炸板的高标（前天 ≥2 连板、昨天断板），今天竞价高开 {config.WTS_GAP_PCT[0]:g}~{config.WTS_GAP_PCT[1]:g}% "
                 "说明分歧后有人回流；开盘价买、次日卖。要看它炸板的原因（是否有利空）、板块还在不在、承接量够不够。"),
    "bz": Strat("bz", "补涨官", config.BZ_GAP_PCT,
                f"补涨：题材龙头 ≥{config.BZ_MIN_LEADER} 板，同题材昨日首板，今天竞价高开 {config.BZ_GAP_PCT[0]:g}~{config.BZ_GAP_PCT[1]:g}%；"
                "开盘价买、次日卖。高开太多反而亏。要看龙头今天还强不强、这只是不是题材里的正宗（换手、量比、成交额）。"),
}


# ---------- 第一层：候选 + 规则 + 证据（纯代码） ----------
def read_candidates(now: datetime, path: Path | None = None) -> dict[str, dict]:
    """今天的候选 {策略: {代码: 附加信息}}。文件不是今天的就当没有。"""
    try:
        data = json.loads(Path(path or TARGETS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("date") != now.date().isoformat():
        return {}
    out = {}
    for key in STRATS:
        raw = data.get(key)
        if isinstance(raw, dict) and raw:
            out[key] = {c: (v if isinstance(v, dict) else {"n": v}) for c, v in raw.items() if str(c).startswith(MAIN_BOARD)}
    return out


def _evidence(code: str, info: dict, q, ctx: dict) -> dict:
    ev = {"code": code, "name": getattr(q, "name", "") or code}
    for k, v in info.items():
        ev[{"n": "连板数", "theme": "题材", "leader": "龙头板数"}.get(k, k)] = v
    if q is not None:
        ev.update({"今日开盘涨幅%": round(q.open_pct, 2), "现价涨幅%": round(q.pct, 2), "成交额亿": round(q.amount / 1e8, 2),
                   "换手%": q.turnover, "量比": q.volume_ratio, "已封涨停": q.price >= q.limit_up > 0})
    for t in ctx.get("themeLadder") or []:
        if any(m.get("code") == code for m in t.get("members", [])):
            ev["所属梯队"] = f"{t.get('theme')}（{t.get('n')} 家，最高 {t.get('maxb')} 板，{t.get('phase') or '—'}）"
            break
    return ev


def screen(strat: Strat, cands: dict, quotes: dict, ctx: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    """规则把关。返回 (放行的证据列表, 被规则挡掉的 [{code,name,why}])。没有今天的竞价数据一律挡掉（等竞价，不猜）。"""
    lo, hi = strat.gap
    passed, rejected = [], []
    for code, info in cands.items():
        q = quotes.get(code)
        name = getattr(q, "name", "") or code
        if q is None or q.ts.date() != now.date() or q.open <= 0:
            rejected.append({"code": code, "name": name, "why": "还没有今天的竞价/开盘数据"})
            continue
        gap = q.open_pct
        if lo <= gap < hi:
            passed.append(_evidence(code, info, q, ctx))
        else:
            rejected.append({"code": code, "name": name, "why": f"竞价高开 {gap:+.2f}%，不在 {lo:g}~{hi:g}% 区间"})
    return passed, rejected


# ---------- 第二层：策略交易员 ----------
def trader_system(strat: Strat) -> str:
    return (f"你是 A 股短线交易台里的「{strat.label}」，只负责一个策略。\n策略：{strat.brief}\n"
            "规则已经把关过：你拿到的都是满足竞价区间的候选。你的工作是逐只表态：买 / 不买。你只能对给你的候选表态，不能加票。"
            "你是否决者——有理由才买，没理由就不买；但也别因为一条经验就一票否决，看板块、封单、承接、大盘情绪（market）综合判断。\n"
            "每只必须写 why（一句话，引用候选里的具体数字）和 falsify（否证条件：什么具体、可观察的情况出现就说明你错了，"
            "例如「开盘 10 分钟内跌破昨收」「板块龙头炸板」）。买的话 confidence 写 高/中/低。不调用任何工具，模拟推荐，不下真单。")


def trader_schema(snapshot_id: str, codes: list[str]) -> dict:
    one = {"type": "object", "additionalProperties": False, "required": ["code", "buy", "why", "falsify", "confidence"],
           "properties": {"code": {"enum": list(codes)}, "buy": {"type": "boolean"}, "why": {"type": "string", "maxLength": 100},
                          "falsify": {"type": "string", "maxLength": 100}, "confidence": {"enum": ["高", "中", "低"]}}}
    return {"type": "object", "additionalProperties": False, "required": ["snapshotId", "verdicts"],
            "properties": {"snapshotId": {"const": snapshot_id}, "verdicts": {"type": "array", "items": one}}}


def _market(ctx: dict) -> dict:
    return {"breadth": (ctx.get("market") or {}).get("breadth"), "sentiment": (ctx.get("market") or {}).get("sentiment"),
            "highBoards": {k: v for k, v in (ctx.get("highBoards") or {}).items() if k != "items"}}


def validate_verdicts(answer, snapshot_id: str, passed: list[dict]) -> list[dict]:
    """编号必须对；只认候选里的代码；没表态的当「不买」；同一只多次表态取第一次。"""
    if not isinstance(answer, dict) or answer.get("snapshotId") != snapshot_id:
        raise ValueError("交易员的回答与快照编号不一致")
    by_code = {p["code"]: p for p in passed}
    seen = {}
    for v in answer.get("verdicts") if isinstance(answer.get("verdicts"), list) else []:
        code = v.get("code") if isinstance(v, dict) else None
        if code in by_code and code not in seen:
            seen[code] = {"code": code, "name": by_code[code]["name"], "buy": v.get("buy") is True,
                          "why": ft._text(v.get("why"), 100), "falsify": ft._text(v.get("falsify"), 100),
                          "confidence": v.get("confidence") if v.get("confidence") in ("高", "中", "低") else "低"}
    return [seen.get(c) or {"code": c, "name": by_code[c]["name"], "buy": False, "why": "交易员没表态", "falsify": "", "confidence": "低"}
            for c in by_code]


def run_trader(strat: Strat, now: datetime, slot: str, scene: dict, ask) -> dict:
    """一位交易员一轮：规则把关 → （有放行的才）问 DeepSeek → 提交单。失败只记在自己名下。"""
    ctx = scene.get("ctx") or {}
    cands = (scene.get("cands") or {}).get(strat.key) or {}
    sub = {"key": strat.key, "label": strat.label, "slot": slot, "candidates": len(cands), "passed": 0, "rejected": [],
           "picks": [], "vetoed": [], "error": None}
    if not cands:
        return sub
    passed, rejected = screen(strat, cands, scene.get("quotes") or {}, ctx, now)
    sub["passed"], sub["rejected"] = len(passed), rejected
    if not passed:
        return sub
    snap = f"DESK-{strat.key}-{now:%Y%m%d}-{slot.replace(':', '')}"
    prompt = ("快照编号必须原样返回。\n候选（规则已放行）：" + json.dumps(passed, ensure_ascii=False)
              + "\n市场：" + json.dumps(_market(ctx), ensure_ascii=False)
              + f"\n\n请返回 JSON：snapshotId、verdicts（每个候选一项：code、buy、why、falsify、confidence）。")
    try:
        verdicts = validate_verdicts(ask(trader_system(strat), prompt, trader_schema(snap, [p["code"] for p in passed])), snap, passed)
    except Exception as e:                                             # noqa: BLE001
        sub["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return sub
    sub["picks"] = [v for v in verdicts if v["buy"]]
    sub["vetoed"] = [v for v in verdicts if not v["buy"]]
    by_code = {p["code"]: p for p in passed}
    for v in verdicts:
        v["evidence"] = by_code[v["code"]]
    return sub


# ---------- 第三层：经理汇总 ----------
MANAGER_SYSTEM = (
    "你是 A 股短线交易台的经理。各策略交易员已经把自己看好的票递上来了（每只带理由、否证条件和证据）。"
    "你的工作：只从递上来的票里挑「今天应该做」的，给每只定仓位（占 dj 总资金的比例，0.05~0.30，把握大的给大，试错给小），"
    "写一句理由和一条否证条件；其余的写成「不做」。你不能加票。同一只被多个策略推荐是加分项；大盘情绪差、高标走弱的日子要更挑剔，"
    "但不要因为谨慎就一只不选——有明确优势的就选。不调用任何工具，只是给 dj 的参考，不下真单。")


def final_schema(snapshot_id: str, codes: list[str]) -> dict:
    one = {"type": "object", "additionalProperties": False, "required": ["code", "do", "pct", "why", "falsify"],
           "properties": {"code": {"enum": list(codes)}, "do": {"type": "boolean"},
                          "pct": {"type": "number", "minimum": MIN_PCT, "maximum": MAX_PCT},
                          "why": {"type": "string", "maxLength": 100}, "falsify": {"type": "string", "maxLength": 100}}}
    return {"type": "object", "additionalProperties": False, "required": ["snapshotId", "read", "items"],
            "properties": {"snapshotId": {"const": snapshot_id}, "read": {"type": "string", "maxLength": 200},
                           "items": {"type": "array", "items": one}}}


def _all_picks(subs: list[dict]) -> list[dict]:
    """把各交易员的「买」按代码合并：同一只多个策略推荐就合成一条。"""
    merged = {}
    for s in subs:
        for p in s.get("picks") or []:
            m = merged.setdefault(p["code"], {"code": p["code"], "name": p["name"], "from": [], "evidence": p.get("evidence")})
            m["from"].append({"strategy": s["label"], "why": p["why"], "falsify": p["falsify"], "confidence": p["confidence"]})
    return list(merged.values())


def validate_final(answer, snapshot_id: str, picks: list[dict]) -> dict:
    if not isinstance(answer, dict) or answer.get("snapshotId") != snapshot_id:
        raise ValueError("经理的回答与快照编号不一致")
    known = {p["code"]: p for p in picks}
    done = {}
    for it in answer.get("items") if isinstance(answer.get("items"), list) else []:
        code = it.get("code") if isinstance(it, dict) else None
        if code not in known or code in done:
            continue
        pct = ft._number(it.get("pct"))
        done[code] = {"code": code, "name": known[code]["name"], "do": it.get("do") is True,
                      "pct": round(min(max(pct if pct is not None else MIN_PCT, MIN_PCT), MAX_PCT), 3),
                      "why": ft._text(it.get("why"), 100), "falsify": ft._text(it.get("falsify"), 100),
                      "strategies": [f["strategy"] for f in known[code]["from"]]}
    items = [done.get(c) or {"code": c, "name": known[c]["name"], "do": False, "pct": MIN_PCT, "why": "经理没表态",
                             "falsify": "", "strategies": [f["strategy"] for f in known[c]["from"]]} for c in known]
    return {"read": ft._text(answer.get("read"), 200), "items": items}


def run_final(now: datetime, slot: str, subs: list[dict], ctx: dict, directive: dict | None, ask) -> dict:
    """经理汇总。没有票就不调用；调用失败退回「各交易员推荐，经理未汇总」，票不丢。"""
    picks = _all_picks(subs)
    if not picks:
        return {"read": "", "items": [], "fallback": False}
    snap = f"DESKM-{now:%Y%m%d}-{slot.replace(':', '')}"
    prompt = ("快照编号必须原样返回。\n各交易员推荐：" + json.dumps(picks, ensure_ascii=False)
              + "\n今天的行情判断：" + json.dumps({k: (directive or {}).get(k) for k in ("regime", "slot", "falsify")}, ensure_ascii=False)
              + "\n市场：" + json.dumps(_market(ctx), ensure_ascii=False)
              + "\n\n请返回 JSON：snapshotId、read（两句话总评）、items（每只一项：code、do、pct、why、falsify）。")
    try:
        out = validate_final(ask(MANAGER_SYSTEM, prompt, final_schema(snap, [p["code"] for p in picks])), snap, picks)
        return {**out, "fallback": False}
    except Exception as e:                                             # noqa: BLE001
        items = [{"code": p["code"], "name": p["name"], "do": True, "pct": MIN_PCT,
                  "why": p["from"][0]["why"], "falsify": p["from"][0]["falsify"], "strategies": [f["strategy"] for f in p["from"]]}
                 for p in picks]
        return {"read": f"经理汇总失败（{type(e).__name__}），以下是各交易员的原始推荐，仓位未定", "items": items, "fallback": True}


# ---------- 推送 ----------
def push_text(now: datetime, slot: str, subs: list[dict], final: dict) -> str:
    do = [i for i in final["items"] if i["do"]]
    skip = [i for i in final["items"] if not i["do"]]
    parts = [f"{now:%H:%M}｜{slot} 时点"]
    if final.get("read"):
        parts.append("【经理】" + final["read"])
    if do:
        parts.append("【应该做】\n" + "\n".join(
            f"▶ {i['name']} {i['code']}｜{'/'.join(i['strategies'])}｜" + ("仓位未定" if final.get("fallback") else f"仓位 {i['pct']:.0%}")
            + f"\n   理由：{i['why']}\n   否证：{i['falsify'] or '—'}" for i in do))
    else:
        parts.append("【应该做】本时点没有。")
    if skip:
        parts.append("【交易员推荐但经理不做】" + "；".join(f"{i['name']}{i['code']}（{i['why']}）" for i in skip))
    vetoed = [f"{s['label']}否掉 {v['name']}{v['code']}（{v['why']}）" for s in subs for v in s.get("vetoed") or []]
    if vetoed:
        parts.append("【交易员否掉】" + "；".join(vetoed))
    rej = [f"{s['label']}：{r['name']}{r['code']}（{r['why']}）" for s in subs for r in s.get("rejected") or []]
    if rej:
        parts.append("【规则没放行】" + "；".join(rej))
    errs = [f"{s['label']}失败（{s['error']}）" for s in subs if s.get("error")]
    if errs:
        parts.append("⚠ " + "；".join(errs))
    parts.append("只是参考：不会替你下单，买不买你定。模拟推荐，不构成投资建议。")
    return "\n\n".join(parts)


# ---------- 调度与落盘 ----------
def due_slot(now: datetime, state_path: Path | None = None) -> str | None:
    if now.weekday() >= 5:
        return None
    state = ft._read_state(state_path or STATE_PATH)
    done = set(state.get("done", [])) if state.get("date") == now.date().isoformat() else set()
    due = None
    for slot in SLOTS:
        h, m = map(int, slot.split(":"))
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if start <= now <= start + ft.SLOT_WINDOW and slot not in done:
            due = slot
    return due


def _write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def run_desk(now: datetime, slot: str, *, gather, ask, notifier=None, directive: dict | None = None, submit_path: Path | None = None,
             log_path: Path | None = None, state_path: Path | None = None) -> dict:
    """gather(now, slot) -> {ctx, cands, quotes}；ask(system, prompt, schema) -> 回答。gather 失败直接抛出（不记已跑，窗口内再试）。"""
    submit_path, log_path, state_path = Path(submit_path or SUBMIT_PATH), Path(log_path or LOG_PATH), Path(state_path or STATE_PATH)
    scene = gather(now, slot)
    with ThreadPoolExecutor(max_workers=len(STRATS)) as pool:
        subs = list(pool.map(lambda s: run_trader(s, now, slot, scene, ask), STRATS.values()))
    final = run_final(now, slot, subs, scene.get("ctx") or {}, directive, ask)
    record = {"date": now.date().isoformat(), "slot": slot, "ts": now.isoformat(timespec="seconds"), "traders": subs, "final": final}
    _write_json(submit_path, record)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    state = ft._read_state(state_path)
    state = state if state.get("date") == now.date().isoformat() else {"date": now.date().isoformat(), "done": [], "pushed": []}
    done_codes = {i["code"] for i in final["items"] if i["do"]}
    new = done_codes - set(state.get("pushed", []))
    # 09:26 一定推一条（含「今天没有」和为什么）；之后的时点只在有新的「应该做」时才推，别刷屏
    if notifier is not None and (slot == SLOTS[0] or new or any(s.get("error") for s in subs)):
        try:
            notifier.send(f"🎯 今日可做的票｜{slot}｜{len(done_codes)} 只", push_text(now, slot, subs, final))
            notifier.flush(timeout=55)
        except Exception:                                              # noqa: BLE001
            pass
    state["pushed"] = sorted(set(state.get("pushed", [])) | done_codes)
    state["done"] = sorted(set(state.get("done", [])) | {slot})
    _write_json(state_path, state)
    return {"slot": slot, "do": sorted(done_codes), "traders": {s["key"]: {"passed": s["passed"], "picks": len(s["picks"]),
                                                                           "error": s["error"]} for s in subs}}


def gather_scene(now: datetime, slot: str) -> dict:
    """真实现场：一号的 context（市场、梯队、高标）+ 今天各策略候选 + 候选实时行情。"""
    from jevq.data import tencent
    ctx = ft.gather_context(now, slot, skip_src=())
    cands = read_candidates(now)
    codes = sorted({c for v in cands.values() for c in v})
    try:
        quotes = tencent.fetch_quotes(codes) if codes else {}
    except Exception:                                                  # noqa: BLE001
        quotes = {}
    return {"ctx": ctx, "cands": cands, "quotes": quotes}
