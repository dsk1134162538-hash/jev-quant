"""拐点接 Claude 把关（用户 2026-10-14：必须 Claude 同意才确认）。本机 claude 命令行 + 订阅登录，不联网调 API。"""
import json
import subprocess
from datetime import datetime, timedelta

from jevq import db
from jevq.judge.claude_cli import ClaudeCLI
from jevq.judge.turn_claude import ClaudeTurnJudge
from jevq.rules.turn import Breadth


class Done:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def envelope(result, is_error=False):
    return json.dumps({"is_error": is_error, "result": result, "duration_ms": 5000, "total_cost_usd": 0.01})


class Runner:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def cli(outcome, tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    r = Runner(outcome)
    return ClaudeCLI(exe=str(exe), proxy="http://127.0.0.1:8889", run=r), r


def test_ask_json_parses_result_and_isolates_the_call(tmp_path, monkeypatch):
    c, r = cli(Done(envelope('{"turn_prob": 0.72, "reason": "修复"}')), tmp_path, monkeypatch)
    assert c.ask_json("系统", "数据") == {"turn_prob": 0.72, "reason": "修复"}
    args, kw = r.calls[0]
    assert args[0].endswith("claude.exe") and "-p" in args and args[args.index("--tools") + 1] == ""
    assert "--safe-mode" in args and args[args.index("--model") + 1] == "sonnet"
    assert kw["input"] == "数据" and kw["timeout"] > 0
    assert "ANTHROPIC_API_KEY" not in kw["env"]                    # 订阅登录，不能被外壳里的 API key 改成按量计费
    assert kw["env"]["HTTPS_PROXY"] == "http://127.0.0.1:8889"     # 命令行 claude 不读 Windows 系统代理
    assert c.last_trace["cost_usd"] == 0.01


def test_ask_json_reads_json_inside_text_or_code_fence(tmp_path, monkeypatch):
    c, _ = cli(Done(envelope('判断如下：\n```json\n{"turn_prob": 0.3}\n```')), tmp_path, monkeypatch)
    assert c.ask_json("s", "u") == {"turn_prob": 0.3}


def test_ask_json_none_on_error_timeout_or_garbage(tmp_path, monkeypatch):
    for outcome in (Done(envelope("x", is_error=True), 1), Done("not json"), Done(envelope("没有 JSON")),
                    subprocess.TimeoutExpired("claude", 60), OSError("missing")):
        c, _ = cli(outcome, tmp_path, monkeypatch)
        assert c.ask_json("s", "u") is None


def test_unavailable_when_exe_missing(tmp_path):
    c = ClaudeCLI(exe=str(tmp_path / "nope.exe"), run=Runner(Done(envelope("{}"))))
    assert not c.available() and c.ask_json("s", "u") is None


DAY = datetime(2026, 10, 14)


def _series():
    rows = [("09:30", 641, 1), ("09:45", 820, 5), ("10:00", 1040, 4), ("10:01", 1223, 4)]
    return [Breadth(DAY.replace(hour=int(t[:2]), minute=int(t[3:])), up, 3036 - up, 3036, 0, ld, -13.0)
            for t, up, ld in rows]


class FakeCLI:
    def __init__(self, answer):
        self.answer, self.prompts, self.last_trace = answer, [], {"cost_usd": 0.01}

    def ask_json(self, system, user):
        self.prompts.append((system, user))
        return self.answer


def test_turn_judge_sends_breadth_curve_and_sentiment_stats_and_returns_prob():
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO decisions(ts, code, verdict, detail, strategy) VALUES (?, '', 'sentiment', ?, 'market')",
                 ("2026-10-14T10:00:00", json.dumps({"stats": {"涨停家数": 31, "跌停家数": 4, "炸板率%": 22.6,
                                                               "最高连板": 3, "昨日涨停今日均涨%": 1.39}})))
    conn.commit()
    fake = FakeCLI({"turn_prob": 0.35, "reason": "弱势反抽"})
    j = ClaudeTurnJudge(fake, conn)
    assert j.confirm(_series(), {}, DAY.replace(hour=10, minute=1)) == 0.35
    system, user = fake.prompts[0]
    assert "恐慌后的情绪修复拐点" in system and "turn_prob" in system
    assert "10:01 红盘40%" in user and "09:45 红盘27%" in user and "涨停31" in user and "炸板率22.6%" in user
    assert j.last_trace["reason"] == "弱势反抽" and j.last_trace["prob"] == 0.35


def test_turn_judge_none_when_claude_gives_nothing_usable():
    conn = db.connect(":memory:")
    for answer in (None, {"reason": "x"}, {"turn_prob": "高"}):
        assert ClaudeTurnJudge(FakeCLI(answer), conn).confirm(_series(), {}, DAY.replace(hour=10, minute=1)) is None
    assert ClaudeTurnJudge(FakeCLI({"turn_prob": 1.7}), conn).confirm(_series(), {}, DAY) == 1.0


# ---- 引擎：规则满足 → 问 Claude（把关）和 Jev（只记录） ----

from test_turn import TODAY_1009, FakeTurnJudge, _engine, _feed  # noqa: E402

D1009 = datetime(2026, 10, 9)


class FakeGate:
    def __init__(self, probs):
        self.probs, self.calls, self.last_trace = list(probs), [], None

    def confirm(self, series, detail, now):
        self.calls.append(now)
        p = self.probs.pop(0) if self.probs else 0.9
        self.last_trace = {"prob": p, "reason": "测试"}
        return p


def test_claude_gate_must_agree_and_jev_is_only_recorded():
    jev, gate = FakeTurnJudge([0.9, 0.9, 0.9]), FakeGate([0.4, None, 0.7])
    eng, conn = _engine(jev)
    eng.turn_gate = gate
    _feed(eng, TODAY_1009[:14])
    t = D1009.replace(hour=13, minute=10)
    eng._turn_step(t)                                                  # Claude 不认（Jev 认也没用）→ 5 分钟后再问
    eng._turn_step(t + timedelta(minutes=4))
    assert len(gate.calls) == 1 and not eng.turn_on
    eng._turn_step(t + timedelta(minutes=5))                           # Claude 没答上 → 下一分钟就再问
    assert not eng.turn_on
    eng._turn_step(t + timedelta(minutes=6))
    assert len(gate.calls) == 3 and eng.turn_on
    rows = [json.loads(d) for (d,) in conn.execute("SELECT detail FROM decisions WHERE verdict='turn' ORDER BY id")]
    assert [r["confirmed"] for r in rows] == [False, False, True]
    assert rows[0]["by"] == "规则+Claude" and rows[0]["claude_prob"] == 0.4 and rows[0]["prob"] == 0.9
    assert rows[2]["claude_reason"] == "测试"
