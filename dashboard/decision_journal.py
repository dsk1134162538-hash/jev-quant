"""盘中 AI 研究判断的追加式本地日志；不提供交易或浏览器写入接口。"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

REGIMES = {"防守观察": "证据偏弱或冲突，优先控制暴露", "修复观察": "宽度与行业修复，但仍需确认",
           "进攻观察": "宽度与行业共振偏强，只允许进一步研究"}
ACTIONS = {"空仓等待": "不生成候选或交易指令", "仅观察候选": "只更新研究候选，不生成交易指令",
           "允许模拟小仓评估": "仅允许另行做 dryrun 风险评估，本服务仍不下单"}


def build_questions() -> dict:
    return {
        "regime": {"type": "choice", "instructions": "只根据当前全市场宽度、指数和行业广度，选择当前研究状态；不要使用旧策略规则。",
                   "criteria": REGIMES},
        "action": {"type": "choice", "instructions": "选择研究动作。这不是交易指令，不得假设可以自动买卖。",
                   "criteria": ACTIONS},
    }


def make_record(answers: dict, context: dict, now: datetime, model: str = "unknown") -> dict:
    regime = answers["regime"]["choice"]
    action = answers["action"]["choice"]
    if regime not in REGIMES or action not in ACTIONS:
        raise ValueError("AI 返回了未定义的研究状态或动作")
    market = context["marketBreadth"]
    sectors = context["sectorBreadth"]
    leaders = "、".join(f"{row['name']} {row['change']:+.2f}%" for row in sectors["leaders"][:3])
    confidence = [answers[key].get("confidence") for key in ("regime", "action")]
    confidence = [float(value) for value in confidence if value is not None]
    return {
        "id": f"AI-{now:%Y%m%d-%H%M%S}", "ts": now.isoformat(timespec="seconds"), "time": now.strftime("%H:%M"),
        "type": "盘中 AI", "title": f"{regime} · {action}", "action": action, "regime": regime,
        "risk": "仅研究 · 不下单", "status": "已持久化", "model": model,
        "confidence": round(min(confidence), 4) if confidence else None,
        "market": (f"全市场上涨 {market['up']} 家、下跌 {market['down']} 家，红盘占比 {market['upRatio']}%；"
                   f"覆盖 {market['total']}/{market['universe']} 只，涨停 {market['limitUp']} 家、跌停 {market['limitDown']} 家。"),
        "sector": f"行业上涨 {sectors['up']} 个、下跌 {sectors['down']} 个。领涨：{leaders or '暂无'}。",
        "stock": f"AI 研究动作：{action}。{ACTIONS[action]}；未生成 quote.json，未调用 buy 或 sell。",
        "trigger": "仅在新鲜全市场与行业数据完整时进行下一次判断；历史记录不被覆盖。",
        "invalidation": "行情过期、覆盖不足、行业数据失效或证据冲突时，本判断立即失效。",
        "evidence": context, "answers": answers, "persisted": True,
    }


class DecisionJournal:
    def __init__(self, path: Path, judge=None, interval_seconds: float = 300, clock=datetime.now, on_record=None,
                 event_cooldown_seconds: float = 60, external: bool = False):
        self.path = Path(path)
        self.judge = judge
        self.external = external
        self.interval_seconds = interval_seconds
        self.event_cooldown_seconds = event_cooldown_seconds
        self.clock = datetime.now if clock is datetime else clock
        self.on_record = on_record
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._loaded_offset = 0
        self._last_attempt = 0.0
        self._last_decision_signature = None
        self._running = False
        self._error = ""
        self._listener_error = ""
        self._load()

    def _load(self) -> None:
        self._reload()

    def _reload(self) -> None:
        """增量读取订阅任务等其他本地进程追加的完整 JSONL 行。"""
        if not self.path.exists():
            return
        if self.path.stat().st_size < self._loaded_offset:
            self._records = []
            self._loaded_offset = 0
        with self.path.open("rb") as handle:
            handle.seek(self._loaded_offset)
            while True:
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break
                self._loaded_offset = handle.tell()
                try:
                    record = json.loads(line.decode("utf-8"))
                    if record.get("id") and record.get("ts"):
                        self._records.append(record)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    continue

    @staticmethod
    def _signature(context: dict) -> dict:
        prices = {}
        for row in context.get("observations", []):
            try:
                prices[row["code"]] = float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
        return {"upRatio": context.get("marketBreadth", {}).get("upRatio"),
                "sectorUp": context.get("sectorBreadth", {}).get("up"), "prices": prices}

    def _significant_event(self, context: dict) -> bool:
        """与上次成功判断相比出现明显变化；调用方负责持有 _lock。"""
        previous, current = self._last_decision_signature, self._signature(context)
        if previous is None:
            return False
        try:
            if abs(float(current["upRatio"]) - float(previous["upRatio"])) >= 5:
                return True
        except (TypeError, ValueError):
            pass
        if current["sectorUp"] is not None and previous["sectorUp"] is not None and \
                abs(int(current["sectorUp"]) - int(previous["sectorUp"])) >= 5:
            return True
        for code, price in current["prices"].items():
            old_price = previous["prices"].get(code)
            if old_price and abs(price / old_price - 1) >= 0.01:
                return True
        return False

    def records(self, limit: int = 100) -> list[dict]:
        with self._lock:
            self._reload()
            return list(reversed(self._records[-limit:]))

    def maybe_update(self, context: dict) -> None:
        if self.judge is None or context.get("snapshot", {}).get("phase") != "盘中":
            return
        market, sectors = context["marketBreadth"], context["sectorBreadth"]
        if not all((market.get("complete"), sectors.get("complete"),
                    not market.get("stale"), not sectors.get("stale"))):
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_attempt
            periodic_due = not self._error and elapsed >= self.interval_seconds
            event_due = not self._error and elapsed >= self.event_cooldown_seconds and self._significant_event(context)
            if self._running or (self._error and elapsed < 3600) or not (periodic_due or event_due):
                return
            self._running = True
            self._last_attempt = time.monotonic()
        threading.Thread(target=self.update_now, args=(context,), daemon=True, name="dashboard-ai-journal").start()

    def update_now(self, context: dict) -> None:
        try:
            if self.judge is None:
                raise RuntimeError("未配置 Kimi 判断器")
            answers = self.judge.ask(context, build_questions())
            model = getattr(self.judge, "model_name", "unknown")
            record = make_record(answers, context, self.clock(), model=model)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._loaded_offset = self.path.stat().st_size
                self._records.append(record)
                self._last_decision_signature = self._signature(context)
                self._error = ""
                self._listener_error = ""
            if self.on_record is not None:
                try:
                    self.on_record(record, context)
                except Exception as exc:
                    # 策略监听器独立记账；即使它自身有程序错误，也不抹去已经持久化的盘中判断。
                    with self._lock:
                        self._listener_error = str(exc)
        except Exception as exc:
            message = str(exc)
            trace = getattr(getattr(self.judge, "cli", None), "last_trace", None)
            if trace and trace.get("result"):
                message = f"{message}：{trace['result']}"
            with self._lock:
                self._error = message
        finally:
            with self._lock:
                self._running = False

    def status(self) -> dict:
        with self._lock:
            self._reload()
            return {"enabled": self.judge is not None or self.external, "persistent": True,
                    "mode": "subscription" if self.external else "api", "count": len(self._records),
                    "running": self._running, "error": self._error, "listenerError": self._listener_error,
                    "source": str(self.path), "refreshSeconds": int(self.interval_seconds),
                    "eventCooldownSeconds": int(self.event_cooldown_seconds), "eventDriven": not self.external}
