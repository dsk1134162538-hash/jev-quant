# 公告风险细判 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把公告排雷从「标题分类、沾一类就否决」改为「标题粗筛疑似 → 读正文让 Jev 按轻重打风险分 → 过线才否决」，并在面板、推送、报告中完整展示。

**Architecture:** 新增纯函数模块 `jevq/judge/notice_risk.py`（十一类风险的关键词、判断口径、Jev 问题构造、风险分计算）；新增 `jevq/data/notice_body.py`（东方财富正文接口 + `notice_bodies` 缓存 + 早盘预抓）；重写 `jevq/judge/notices.py::NoticeJudge.check` 为两段式，结果缓存在新表 `notice_risk`，`last_trace` 改为结构化 dict；显示层（推送、面板、报告）同时兼容旧的 list 形态 trace。

**Tech Stack:** Python 3.12、sqlite3、requests、pytest；前端 `jevq/app/ui.html`（原生 JS，pywebview）。

**Spec:** `docs/specs/2026-09-26-notice-risk-design.md`

## Global Constraints

- 低吸、扫首板、自定义策略三方共用 `NoticeJudge`，调用接口 `check(code, today) -> (verdict, detail)` 与 `last_trace` 属性名不变；verdict 取值仍为 `pass | reject | error`。
- 影子账户不走 Jev；消息面 `NewsJudge` 不改；不让 Jev 决定仓位。
- Jev 任何失败 = 该票当日 `error`（否决），失败结果不写缓存。
- 阈值：`RISK_REJECT = 0.5`、`RISK_UNSURE = 0.35`；风险分 `P(高)×1 + P(中)×0.6 + P(低)×0.2`。
- 正文截前 `BODY_CHARS = 1500` 字；每票每次最多细判 `RISK_MAX_PER_CHECK = 20` 条疑似；预抓每次最多 `PREFETCH_CAP = 300` 条。
- 测试不联网（注入假 Jev、假正文抓取）；测试在临时目录（conftest 已有）；推送全局拦截（conftest 已有）。
- 不触碰 `data/`、`reports/`、运行中的 daemon 进程；不做破坏性 git 操作；只 stage 自己改的文件。
- 前端所有外部字符串经 `esc()`。
- 每次提交带 trailer：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 运行测试：`.venv/Scripts/python -m pytest -q -p no:cacheprovider`（全量须通过、输出干净）。

---

### Task 1: 风险类别与打分（纯函数）

**Files:**
- Create: `jevq/judge/notice_risk.py`
- Modify: `jevq/config.py`（新增常量）
- Test: `tests/test_notice_risk.py`

**Interfaces:**
- Produces:
  - `RISK_CATEGORIES: dict[str, dict]`（键为类别名，值含 `keywords: list[str]`、`title: str`（第一段标题分类的描述）、`levels: dict[str, str]`（高/中/低/无 各自口径））
  - `LEVELS = ("高", "中", "低", "无")`
  - `keyword_category(title: str) -> str | None`
  - `title_criteria() -> dict[str, str]`（第一段 Jev 标题分类用：十一类 + `亏损` + `其他`）
  - `risk_question(category: str) -> dict`（第二段 Jev `questions` 字典，键 `risk`）
  - `risk_state(notice, category: str, body: str | None, today: date) -> dict`
  - `risk_score(probs: dict[str, float]) -> float`
  - `classify_score(score: float) -> str`（返回 `"reject" | "unsure" | "pass"`）
  - config：`RISK_REJECT = 0.5`、`RISK_UNSURE = 0.35`、`RISK_VERSION = 1`、`RISK_MAX_PER_CHECK = 20`、`BODY_CHARS = 1500`、`PREFETCH_CAP = 300`、`RISK_JEV_TIMEOUT = 15.0`

- [ ] **Step 1: 写失败测试** `tests/test_notice_risk.py`

```python
from datetime import date

import pytest

from jevq.data.akdata import Notice
from jevq.judge import notice_risk as nr


@pytest.mark.parametrize("title,cat", [
    ("甲:关于持股5%以上股东减持股份计划的预披露公告", "减持"),
    ("先导基电:关于控股股东自愿承诺未来12个月不减持公司股份的公告", "减持"),
    ("甲:关于股东部分股份解除质押的公告", "质押"),
    ("甲:关于回购公司股份的进展公告", "回购"),
    ("甲:关于公司涉及诉讼的公告", "诉讼仲裁"),
    ("甲:关于筹划重大资产重组的停牌公告", "重组"),
    ("甲:股票交易严重异常波动公告", "风险提示"),
    ("甲:关于收到中国证监会立案告知书的公告", "监管处罚"),
    ("甲:关于公司股票可能被实施退市风险警示的提示性公告", "退市风险"),
    ("甲:关于首发限售股上市流通的提示性公告", "限售解禁"),
    ("甲:关于控股股东所持股份被司法冻结的公告", "冻结和债务"),
    ("甲:关于变更会计师事务所的公告", "审计问题"),
    ("甲:2026年半年度报告", None),
    ("甲:第五届董事会第十次会议决议公告", None),
])
def test_keyword_category(title, cat):
    assert nr.keyword_category(title) == cat


def test_eleven_categories_each_with_four_levels():
    assert len(nr.RISK_CATEGORIES) == 11
    for name, c in nr.RISK_CATEGORIES.items():
        assert c["keywords"] and c["title"]
        assert set(c["levels"]) == set(nr.LEVELS), name


def test_title_criteria_has_other_and_loss():
    crit = nr.title_criteria()
    assert set(nr.RISK_CATEGORIES) <= set(crit)
    assert "其他" in crit and "亏损" in crit


def test_risk_question_uses_category_levels():
    q = nr.risk_question("回购")["risk"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == set(nr.LEVELS)
    assert "终止" in q["criteria"]["高"]


def test_risk_state_with_and_without_body():
    n = Notice("AN1", "600001", "甲:回购进展", "回购", "2026-09-20")
    s = nr.risk_state(n, "回购", "正文" * 2000, date(2026, 9, 26))
    assert s["距今天数"] == 6 and s["疑似类别"] == "回购"
    assert len(s["正文摘要"]) == 1500
    s2 = nr.risk_state(n, "回购", None, date(2026, 9, 26))
    assert s2["正文摘要"] == "未读到正文，仅凭标题判断"


@pytest.mark.parametrize("probs,score", [
    ({"高": 1.0}, 1.0),
    ({"中": 1.0}, 0.6),
    ({"低": 1.0}, 0.2),
    ({"无": 1.0}, 0.0),
    ({"高": 0.5, "低": 0.5}, 0.6),
    ({}, 0.0),
])
def test_risk_score(probs, score):
    assert nr.risk_score(probs) == pytest.approx(score)


@pytest.mark.parametrize("score,verdict", [
    (0.349, "pass"), (0.35, "unsure"), (0.499, "unsure"), (0.5, "reject"), (1.0, "reject"),
])
def test_classify_score_boundaries(score, verdict):
    assert nr.classify_score(score) == verdict
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_notice_risk.py`
Expected: FAIL（`ModuleNotFoundError: jevq.judge.notice_risk`）

- [ ] **Step 3: 在 `jevq/config.py` 的 `LOW_CONFIDENCE = 0.6` 下方加常量**

```python
# 公告风险细判（docs/specs/2026-09-26-notice-risk-design.md）
RISK_REJECT = 0.5          # 风险分 ≥ 此值：否决「高风险」
RISK_UNSURE = 0.35         # 风险分落在 [RISK_UNSURE, RISK_REJECT)：否决「拿不准」
RISK_VERSION = 1           # 改判断口径时 +1，旧的 notice_risk 缓存自动失效重判
RISK_MAX_PER_CHECK = 20    # 每票每次最多细判的疑似公告条数
BODY_CHARS = 1500          # 交给 Jev 的正文长度
PREFETCH_CAP = 300         # 早盘预抓正文每次最多条数
RISK_JEV_TIMEOUT = 15.0    # 带正文的 Jev 调用超时（秒）
```

- [ ] **Step 4: 创建 `jevq/judge/notice_risk.py`**

```python
"""公告风险细判的纯函数：十一类风险的关键词、判断口径、Jev 问题与风险分。"""
from datetime import date

from jevq.config import BODY_CHARS, RISK_REJECT, RISK_UNSURE

LEVELS = ("高", "中", "低", "无")
LEVEL_WEIGHTS = {"高": 1.0, "中": 0.6, "低": 0.2, "无": 0.0}

# 顺序即关键词匹配优先级：更具体、更严重的类别放前面
RISK_CATEGORIES: dict[str, dict] = {
    "退市风险": {
        "keywords": ["退市", "终止上市", "风险警示", "*ST", "面值"],
        "title": "可能被实施退市风险警示、终止上市、面值退市等提示",
        "levels": {"高": "任何退市风险警示、终止上市、面值退市相关提示", "中": "（本类一般不适用）",
                   "低": "撤销退市风险警示等利好性质", "无": "与退市无实质关系"}},
    "监管处罚": {
        "keywords": ["立案", "调查", "处罚", "警示函", "监管函", "纪律处分", "问询函"],
        "title": "被立案调查、行政处罚、警示函、监管函、纪律处分、问询函",
        "levels": {"高": "被立案调查、行政处罚、重大违规", "中": "警示函、监管函、问询函（尚未回复或问题较重）",
                   "低": "问询函已回复且无实质问题、轻微事项", "无": "与监管处罚无实质关系"}},
    "冻结和债务": {
        "keywords": ["冻结", "逾期", "违约", "违规担保", "资金占用"],
        "title": "股份被冻结、债务逾期违约、违规担保、资金占用",
        "levels": {"高": "控股股东或实控人股份被冻结、公司债务逾期或违约、违规担保、资金被占用",
                   "中": "小比例冻结、小额逾期", "低": "冻结解除、债务已偿还", "无": "与冻结或债务无实质关系"}},
    "审计问题": {
        "keywords": ["非标", "无法表示意见", "保留意见", "会计师事务所辞任", "变更会计师"],
        "title": "非标准审计意见、无法表示意见、会计师事务所辞任或变更",
        "levels": {"高": "非标准审计意见、无法表示意见、保留意见", "中": "年中突然更换或会计师辞任",
                   "低": "例行轮换会计师", "无": "与审计问题无实质关系"}},
    "重组": {
        "keywords": ["重组", "重大资产", "发行股份购买资产"],
        "title": "重大资产重组、发行股份购买资产的筹划、进展、终止、完成",
        "levels": {"高": "重组终止、失败、被否，或收到交易所问询且问题重大",
                   "中": "重组筹划、预案、推进中（有停牌风险和消息兑现风险）",
                   "低": "重组已完成过户、实施完毕", "无": "与重组无实质关系"}},
    "减持": {
        "keywords": ["减持"],
        "title": "股东、董监高的减持计划、减持进展、被动减持、权益变动导致持股减少",
        "levels": {"高": "控股股东或实控人新的减持计划，或合计拟减持比例 ≥1%",
                   "中": "小比例减持计划、董监高零星减持、减持进展且仍在进行",
                   "低": "减持已实施完毕、期限届满、终止减持", "无": "承诺不减持、与减持无实质关系"}},
    "质押": {
        "keywords": ["质押"],
        "title": "股东股份质押、补充质押、质押展期、解除质押",
        "levels": {"高": "控股股东高比例新增或补充质押、接近平仓线、质押违约",
                   "中": "常规新增质押且比例不低", "低": "小比例常规质押、质押展期",
                   "无": "解除质押、与质押无实质关系"}},
    "回购": {
        "keywords": ["回购"],
        "title": "公司股份回购方案、进展、结果、注销、终止",
        "levels": {"高": "回购终止、未按计划实施、回购后拟减持", "中": "回购金额或进度明显低于承诺",
                   "低": "回购方案、进展、完成、注销等常规回购", "无": "与回购无实质关系"}},
    "诉讼仲裁": {
        "keywords": ["诉讼", "仲裁", "起诉"],
        "title": "公司涉及诉讼、仲裁、被起诉、判决",
        "levels": {"高": "公司作为被告的大额诉讼（占净资产比例较高）、败诉需大额赔偿",
                   "中": "金额不小但影响可控", "低": "小额诉讼、公司为原告、胜诉", "无": "与诉讼无实质关系"}},
    "风险提示": {
        "keywords": ["异常波动", "风险提示", "涨幅较大"],
        "title": "股票交易异常波动、严重异常波动、股价涨幅较大的风险提示",
        "levels": {"高": "严重异常波动且公司明确提示股价严重脱离基本面或存在重大不确定性",
                   "中": "严重异常波动，或公司主动提示股价涨幅较大的风险",
                   "低": "普通股票交易异常波动公告，称无应披露未披露事项", "无": "与风险提示无实质关系"}},
    "限售解禁": {
        "keywords": ["解禁", "限售股上市流通", "限售股份上市流通"],
        "title": "限售股解禁、限售股份上市流通",
        "levels": {"高": "解禁数量占流通股比例 ≥5%", "中": "解禁数量占流通股 1%～5%",
                   "低": "解禁数量占流通股 <1%", "无": "与解禁无实质关系"}},
}

NON_RISK_TITLE = {"亏损": "业绩预亏、净利润亏损、业绩预告下修",
                  "其他": "与以上各类都无关的公告（含控制权变更、董事会决议、定期报告等）"}


def keyword_category(title: str) -> str | None:
    """标题关键词命中的第一个类别；只用于挑疑似，不代表否决。"""
    for name, c in RISK_CATEGORIES.items():
        if any(k in title for k in c["keywords"]):
            return name
    return None


def title_criteria() -> dict[str, str]:
    return {**{name: c["title"] for name, c in RISK_CATEGORIES.items()}, **NON_RISK_TITLE}


def risk_question(category: str) -> dict:
    levels = RISK_CATEGORIES[category]["levels"]
    return {"risk": {"type": "choice",
                     "instructions": f"这是一条「{category}」类 A 股公告。它对短线（1～3 个交易日）持有这只股票的风险有多大？"
                                     "请依据正文中的比例、金额、主体、阶段（计划/进行中/已完成/终止）判断。",
                     "criteria": dict(levels)}}


def risk_state(notice, category: str, body: str | None, today: date) -> dict:
    days = (today - date.fromisoformat(notice.date)).days
    return {"公告标题": notice.title, "公告类型": notice.type, "公告日期": notice.date, "距今天数": days,
            "疑似类别": category,
            "正文摘要": body[:BODY_CHARS] if body else "未读到正文，仅凭标题判断"}


def risk_score(probs: dict[str, float]) -> float:
    return sum(LEVEL_WEIGHTS[k] * float(probs.get(k) or 0.0) for k in LEVELS)


def classify_score(score: float) -> str:
    if score >= RISK_REJECT:
        return "reject"
    if score >= RISK_UNSURE:
        return "unsure"
    return "pass"
```

注意：`risk_score` 用浮点求和，`({"高": 0.5, "低": 0.5}, 0.6)` 用 `pytest.approx` 比较。`classify_score(0.35)` 必须是 `unsure`——若浮点误差导致边界问题，测试会暴露。

- [ ] **Step 5: 运行测试通过**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_notice_risk.py`
Expected: 全部 PASS。若 `甲:关于股东部分股份解除质押的公告` 等用例因关键词顺序命中了别的类别，调整 `RISK_CATEGORIES` 顺序或关键词，而不是改测试期望。

- [ ] **Step 6: 全量测试 + 提交**

```bash
.venv/Scripts/python -m pytest -q -p no:cacheprovider
git add jevq/judge/notice_risk.py jevq/config.py tests/test_notice_risk.py
git commit -m "feat: notice risk categories, criteria and scoring" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 公告正文抓取、缓存与早盘预抓

**Files:**
- Create: `jevq/data/notice_body.py`
- Modify: `jevq/db.py`（SCHEMA 加表）、`run.py`（`_sync_notices` 之后预抓）
- Test: `tests/test_notice_body.py`

**Interfaces:**
- Consumes: `keyword_category(title)`（Task 1）；config `BODY_CHARS`、`PREFETCH_CAP`、`NOTICE_LOOKBACK_DAYS`
- Produces:
  - 表 `notice_bodies(id TEXT PRIMARY KEY, text TEXT, fetched TEXT)`
  - `fetch_body(art_code: str, session=None) -> str | None`
  - `get_body(conn, notice_id: str, fetch=fetch_body) -> str | None`
  - `prefetch_bodies(conn, today: date, codes, fetch=fetch_body, cap: int = PREFETCH_CAP, workers: int = 8) -> int`（返回成功抓取条数）
  - `run._prefetch_bodies(conn, today, universe, log)`

- [ ] **Step 1: 写失败测试** `tests/test_notice_body.py`

```python
from datetime import date

from jevq import db
from jevq.data import notice_body as nb

TODAY = date(2026, 9, 26)


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.urls = payload, exc, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.urls.append((url, params))
        if self.exc:
            raise self.exc
        return FakeResp(self.payload)


def test_fetch_body_cleans_and_truncates():
    s = FakeSession({"data": {"notice_content": "  第一段\n\n 第二段\r\n" + "字" * 3000}})
    text = nb.fetch_body("AN1", session=s)
    assert text.startswith("第一段 第二段")
    assert len(text) == 1500
    assert s.urls[0][1]["art_code"] == "AN1"


def test_fetch_body_failure_returns_none():
    assert nb.fetch_body("AN1", session=FakeSession(exc=OSError("down"))) is None
    assert nb.fetch_body("AN1", session=FakeSession({"data": None})) is None
    assert nb.fetch_body("AN1", session=FakeSession({"data": {"notice_content": "   "}})) is None


def test_get_body_caches_success_only():
    conn = db.connect(":memory:")
    calls = []

    def ok(aid):
        calls.append(aid)
        return "正文"

    assert nb.get_body(conn, "AN1", fetch=ok) == "正文"
    assert nb.get_body(conn, "AN1", fetch=ok) == "正文"
    assert calls == ["AN1"]

    fails = []
    assert nb.get_body(conn, "AN2", fetch=lambda a: fails.append(a)) is None
    assert nb.get_body(conn, "AN2", fetch=lambda a: fails.append(a)) is None
    assert fails == ["AN2", "AN2"]          # 失败不写缓存，下次重试


def _seed(conn):
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)", [
        ("AN1", "600001", "甲:关于股东减持计划的公告", "", "2026-09-20"),
        ("AN2", "600001", "甲:第五届董事会决议公告", "", "2026-09-20"),     # 非疑似：不抓
        ("AN3", "600002", "乙:关于回购进展的公告", "", "2026-09-10"),
        ("AN4", "600009", "丙:关于减持计划的公告", "", "2026-09-10"),       # 不在股票池：不抓
        ("AN5", "600001", "甲:关于减持的公告", "", "2026-05-01"),           # 超出 90 天：不抓
    ])
    conn.commit()


def test_prefetch_only_suspects_in_universe_within_lookback_and_uncached():
    conn = db.connect(":memory:")
    _seed(conn)
    conn.execute("INSERT INTO notice_bodies VALUES ('AN3', '已缓存', '2026-09-25')")
    got = []
    n = nb.prefetch_bodies(conn, TODAY, ["600001", "600002"], fetch=lambda a: got.append(a) or f"正文{a}")
    assert sorted(got) == ["AN1"] and n == 1
    assert nb.get_body(conn, "AN1", fetch=lambda a: None) == "正文AN1"


def test_prefetch_respects_cap_and_counts_failures_as_not_fetched():
    conn = db.connect(":memory:")
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", "甲:减持公告", "", "2026-09-20") for i in range(10)])
    conn.commit()
    got = []
    n = nb.prefetch_bodies(conn, TODAY, ["600001"], fetch=lambda a: got.append(a) or None, cap=3)
    assert len(got) == 3 and n == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_notice_body.py`
Expected: FAIL（模块不存在）

- [ ] **Step 3: `jevq/db.py` 的 SCHEMA 中 `notice_verdicts` 行之后加**

```sql
CREATE TABLE IF NOT EXISTS notice_bodies(id TEXT PRIMARY KEY, text TEXT, fetched TEXT);
```

- [ ] **Step 4: 创建 `jevq/data/notice_body.py`**

```python
"""公告正文：东方财富正文接口 + 本地缓存 + 早盘预抓（只抓标题像雷的）。"""
import re
import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

from jevq.config import BODY_CHARS, NOTICE_LOOKBACK_DAYS, PREFETCH_CAP
from jevq.judge.notice_risk import keyword_category

BODY_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_UA = {"User-Agent": "Mozilla/5.0"}


def fetch_body(art_code: str, session=None) -> str | None:
    try:
        r = (session or requests).get(BODY_URL, params={"art_code": art_code, "client_source": "web",
                                                        "page_index": 1}, timeout=8, headers=_UA)
        data = (r.json() or {}).get("data") or {}
        text = re.sub(r"\s+", " ", data.get("notice_content") or "").strip()
    except Exception:
        return None
    return text[:BODY_CHARS] or None


def get_body(conn: sqlite3.Connection, notice_id: str,
             fetch: Callable[[str], str | None] = fetch_body) -> str | None:
    row = conn.execute("SELECT text FROM notice_bodies WHERE id=?", (notice_id,)).fetchone()
    if row:
        return row[0]
    text = fetch(notice_id)
    if text:
        _save(conn, notice_id, text)
    return text


def _save(conn, notice_id: str, text: str):
    conn.execute("INSERT OR REPLACE INTO notice_bodies(id, text, fetched) VALUES (?, ?, ?)",
                 (notice_id, text, datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def prefetch_bodies(conn: sqlite3.Connection, today: date, codes: Iterable[str],
                    fetch: Callable[[str], str | None] = fetch_body,
                    cap: int = PREFETCH_CAP, workers: int = 8) -> int:
    """股票池内、近 NOTICE_LOOKBACK_DAYS 天、标题命中风险关键词、还没有正文缓存的公告，预抓正文。
    网络请求并发；写库只在调用线程里做（sqlite 连接不跨线程）。"""
    codes = set(codes)
    since = (today - timedelta(days=NOTICE_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT n.id, n.code, n.title FROM notices n LEFT JOIN notice_bodies b ON b.id = n.id "
        "WHERE n.date >= ? AND b.id IS NULL ORDER BY n.date DESC, n.id", (since,)).fetchall()
    todo = [nid for nid, code, title in rows if code in codes and keyword_category(title)][:cap]
    if not todo:
        return 0
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(fetch, todo))
    n = 0
    for nid, text in zip(todo, results):
        if text:
            _save(conn, nid, text)
            n += 1
    return n
```

- [ ] **Step 5: 运行测试通过**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_notice_body.py`
Expected: PASS

- [ ] **Step 6: 接入 `run.py`**

在 `_sync_notices` 函数下方新增：

```python
def _prefetch_bodies(conn, today: date, universe, log: Callable[[str], None]):
    """早盘/午间同步公告后预抓疑似公告正文；失败只记日志，不影响交易。"""
    try:
        log(f"公告正文预抓 {prefetch_bodies(conn, today, list(universe))} 条")
    except Exception as e:
        log(f"公告正文预抓失败：{e!r}")
```

顶部 import 加 `from jevq.data.notice_body import prefetch_bodies`。在 `_run_day` 里两处 `_sync_notices(conn, today, log)` 之后（盘前那处在 `if not late:` 块内；13:00 重同步那处在 `if not resynced and ...` 块内）各加一行 `_prefetch_bodies(conn, today, universe, log)`。

在 `tests/test_run.py` 末尾加测试（沿用该文件已有的 run_day 假依赖写法；阅读文件里现有的 `_run_day`/`run_day` 测试 fixture，复用它们注入 fake fetch 的方式）：断言盘前同步后调用了预抓、预抓抛异常时 `run_day` 照常完成并记日志「公告正文预抓失败」。若 `run.py` 用模块级名字调用，测试用 `monkeypatch.setattr(run, "prefetch_bodies", fake)`。

- [ ] **Step 7: 全量测试 + 提交**

```bash
.venv/Scripts/python -m pytest -q -p no:cacheprovider
git add jevq/data/notice_body.py jevq/db.py run.py tests/test_notice_body.py tests/test_run.py
git commit -m "feat: notice body fetch, cache and pre-open prefetch" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 两段式 NoticeJudge

**Files:**
- Modify: `jevq/judge/notices.py`（重写 `check`，删除 `classify_by_keyword`/`MINE_*`/`SAFE_KEYWORDS`/`NEGATION_KEYWORDS`/`CRITERIA`）、`jevq/judge/jev.py`（`ask` 增加可选 `timeout`）、`jevq/db.py`（新表）
- Test: `tests/test_notice_judge.py`（整体改写为新语义）

**Interfaces:**
- Consumes: Task 1 全部函数与常量；Task 2 `get_body(conn, notice_id, fetch)`
- Produces:
  - 表 `notice_risk(id TEXT PRIMARY KEY, version INTEGER, category TEXT, level TEXT, score REAL, probs TEXT, body_read INTEGER, created TEXT)`
  - `NoticeJudge(conn, jev, workers=8, fetch_body=fetch_body)`；`check(code, today) -> tuple[str, str]`
  - `NoticeJudge.last_trace: dict`，结构：
    ```python
    {"suspects": [  # 按 score 降序
        {"id": str, "date": "YYYY-MM-DD", "title": str, "days_ago": int, "category": str,
         "source": "keyword" | "jev_title", "level": "高|中|低|无", "score": float,
         "probs": dict, "body_read": bool, "excerpt": str,  # 正文前 200 字，无正文为 ""
         "verdict": "reject" | "unsure" | "pass"}],
     "others": int,          # 非疑似公告条数
     "skipped": [{"date": str, "title": str, "category": str}]}  # 超过每票上限未细判的
    ```
  - `JevClient.ask(state, questions, timeout: float | None = None)`

**判定规则（写死在实现里）：**
1. 取 `notices_for(conn, code, today)`（已按日期降序）。
2. 每条：`cat = keyword_category(title)` → 有则疑似（source `keyword`）。否则查 `notice_verdicts` 中 `source='jev2'` 的缓存 label；没有则调 Jev 标题分类（问题 `{"kind": {"type": "choice", "instructions": "这条A股上市公司公告属于哪一类？", "criteria": title_criteria()}}`，state `{"公告标题", "公告类型"}`），写 `notice_verdicts(id, label, confidence, source='jev2', probs)`。label ∈ `RISK_CATEGORIES` → 疑似（source `jev_title`），否则计入 `others`。旧 `source` 为 `keyword`/`jev` 的缓存行一律忽略（旧分类体系）。
3. 疑似前 `RISK_MAX_PER_CHECK` 条细判，其余进 `skipped`。
4. 细判：查 `notice_risk` 且 `version == RISK_VERSION` → 用缓存；否则 `body = get_body(conn, id, fetch_body)`，`ans = jev.ask(risk_state(...), risk_question(cat), timeout=RISK_JEV_TIMEOUT)["risk"]`，`score = risk_score(ans["probabilities"])`，`level = ans["choice"]`，写 `notice_risk`。Jev 调用在线程池并发（`workers`），`get_body` 与写库在调用线程完成（先顺序取正文，再并发问 Jev，再顺序写库）。
5. 任一第一段或第二段 Jev 调用抛 `JevError` → 返回 `("error", "Jev 不可用")`，成功的结果照常缓存，`last_trace` 仍写出已有部分。
6. 取 score 最高的疑似：`classify_score` 为 `reject` → `("reject", f"{cat}·高风险 {score:.2f}：{date} {title}")`；`unsure` → `("reject", f"{cat}·拿不准 {score:.2f}：{date} {title}")`；否则有疑似 → `("pass", f"疑似 {n} 条，均判低风险")`，无疑似 → `("pass", "公告无雷")`。

- [ ] **Step 1: 改写测试** `tests/test_notice_judge.py`（替换整个文件）

```python
import json
from datetime import date

from jevq import db
from jevq.config import RISK_VERSION
from jevq.data.akdata import Notice  # noqa: F401  (与 notices 表结构对应)
from jevq.judge.jev import JevError
from jevq.judge.notices import NoticeJudge

TODAY = date(2026, 9, 26)


class FakeJev:
    """title_labels：标题 → 第一段分类；risk：标题 → 第二段四档概率。"""
    def __init__(self, title_labels=None, risk=None, fail_titles=(), fail_risk=()):
        self.title_labels, self.risk = title_labels or {}, risk or {}
        self.fail_titles, self.fail_risk = set(fail_titles), set(fail_risk)
        self.title_calls, self.risk_calls, self.states = 0, 0, []

    def ask(self, state, questions, timeout=None):
        title = state["公告标题"]
        if "kind" in questions:
            self.title_calls += 1
            if title in self.fail_titles:
                raise JevError("down")
            label = self.title_labels.get(title, "其他")
            return {"kind": {"choice": label, "probabilities": {label: 0.9}, "confidence": 0.9}}
        self.risk_calls += 1
        self.states.append(state)
        if title in self.fail_risk:
            raise JevError("down")
        probs = self.risk[title]
        return {"risk": {"choice": max(probs, key=probs.get), "probabilities": probs,
                         "confidence": max(probs.values())}}


def setup(*rows):
    """rows: (title, date) 或 title（默认 2026-09-20）。"""
    conn = db.connect(":memory:")
    items = [r if isinstance(r, tuple) else (r, "2026-09-20") for r in rows]
    conn.executemany("INSERT INTO notices VALUES (?, ?, ?, ?, ?)",
                     [(f"AN{i}", "600001", t, "", d) for i, (t, d) in enumerate(items)])
    conn.commit()
    return conn


def judge(conn, jev, bodies=None):
    bodies = bodies if bodies is not None else {}
    return NoticeJudge(conn, jev, fetch_body=lambda aid: bodies.get(aid))


def test_no_notices_passes():
    conn = setup()
    j = judge(conn, FakeJev())
    assert j.check("600001", TODAY) == ("pass", "公告无雷")
    assert j.last_trace == {"suspects": [], "others": 0, "skipped": []}


def test_promise_not_to_reduce_is_read_and_passes():
    t = "先导基电:关于控股股东自愿承诺未来12个月不减持公司股份的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"无": 0.9, "低": 0.1}})
    j = judge(conn, jev, {"AN0": "控股股东承诺自即日起12个月内不减持"})
    verdict, detail = j.check("600001", TODAY)
    assert verdict == "pass" and "均判低风险" in detail
    s = j.last_trace["suspects"][0]
    assert s["category"] == "减持" and s["source"] == "keyword" and s["body_read"] is True
    assert jev.states[0]["正文摘要"].startswith("控股股东承诺")


def test_buyback_progress_passes_and_termination_rejects():
    ok, bad = "甲:关于回购公司股份的进展公告", "甲:关于终止回购公司股份的公告"
    conn = setup(ok, bad)
    jev = FakeJev(risk={ok: {"低": 0.9, "无": 0.1}, bad: {"高": 0.8, "中": 0.2}})
    verdict, detail = judge(conn, jev).check("600001", TODAY)
    assert verdict == "reject" and detail.startswith("回购·高风险 0.92：") and "终止回购" in detail


def test_restructuring_in_progress_rejects_as_high():
    t = "甲:关于筹划重大资产重组的进展公告"
    conn = setup(t)
    verdict, detail = judge(conn, FakeJev(risk={t: {"中": 1.0}})).check("600001", TODAY)
    assert verdict == "reject" and detail.startswith("重组·高风险 0.60")


def test_unsure_band_rejects_with_label():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    verdict, detail = judge(conn, FakeJev(risk={t: {"中": 0.7, "无": 0.3}})).check("600001", TODAY)
    assert verdict == "reject" and detail.startswith("减持·拿不准 0.42：")


def test_boundaries():
    for probs, verdict in [({"低": 1.0, "中": 0.2475}, "pass"),     # 0.2 + 0.1485 = 0.3485
                           ({"中": 0.5, "低": 0.25}, "reject"),      # 0.35 拿不准
                           ({"中": 0.5, "低": 1.0}, "reject")]:      # 0.5 高风险
        t = "甲:关于股东减持计划的公告"
        conn = setup(t)
        assert judge(conn, FakeJev(risk={t: probs})).check("600001", TODAY)[0] == verdict


def test_body_missing_falls_back_to_title():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"低": 1.0}})
    j = judge(conn, jev, {})
    j.check("600001", TODAY)
    assert jev.states[0]["正文摘要"] == "未读到正文，仅凭标题判断"
    assert j.last_trace["suspects"][0]["body_read"] is False


def test_jev_title_classification_marks_suspect_and_caches_jev2():
    t = "甲:关于控股股东协议转让部分股份的公告"
    conn = setup(t)
    jev = FakeJev(title_labels={t: "减持"}, risk={t: {"中": 0.2, "低": 0.8}})
    j = judge(conn, jev)
    assert j.check("600001", TODAY)[0] == "pass"
    assert j.last_trace["suspects"][0]["source"] == "jev_title"
    row = conn.execute("SELECT label, source FROM notice_verdicts WHERE id='AN0'").fetchone()
    assert row == ("减持", "jev2")
    j.check("600001", TODAY)
    assert jev.title_calls == 1                     # 标题分类走缓存


def test_old_verdict_cache_is_ignored():
    t = "甲:关于限售股份上市流通的提示性公告"      # 关键词命中「限售股份上市流通」
    t2 = "甲:董事会决议公告"
    conn = setup(t, t2)
    conn.execute("INSERT INTO notice_verdicts(id, label, confidence, source) VALUES ('AN1', '其他', 0.9, 'jev')")
    conn.commit()
    jev = FakeJev(risk={t: {"低": 1.0}})
    judge(conn, jev).check("600001", TODAY)
    assert jev.title_calls == 1                     # 旧 'jev' 缓存不算数，重新按新体系分类


def test_non_suspects_counted_in_others_without_risk_call():
    conn = setup("甲:2026年半年度报告", "甲:董事会决议公告")
    jev = FakeJev()
    j = judge(conn, jev)
    assert j.check("600001", TODAY) == ("pass", "公告无雷")
    assert j.last_trace["others"] == 2 and jev.risk_calls == 0


def test_risk_cache_and_version_bump(monkeypatch):
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    jev = FakeJev(risk={t: {"低": 1.0}})
    judge(conn, jev).check("600001", TODAY)
    judge(conn, jev).check("600001", TODAY)
    assert jev.risk_calls == 1
    conn.execute("UPDATE notice_risk SET version=?", (RISK_VERSION - 1,))
    conn.commit()
    judge(conn, jev).check("600001", TODAY)
    assert jev.risk_calls == 2


def test_risk_failure_is_error_and_not_cached():
    t = "甲:关于股东减持计划的公告"
    conn = setup(t)
    j = judge(conn, FakeJev(fail_risk=[t]))
    assert j.check("600001", TODAY) == ("error", "Jev 不可用")
    assert conn.execute("SELECT COUNT(*) FROM notice_risk").fetchone()[0] == 0


def test_title_failure_is_error():
    t = "甲:董事会决议公告"
    conn = setup(t)
    assert judge(conn, FakeJev(fail_titles=[t])).check("600001", TODAY) == ("error", "Jev 不可用")


def test_partial_failure_caches_successes():
    a, b = "甲:关于股东减持计划的公告", "甲:关于回购进展的公告"
    conn = setup(a, b)
    jev = FakeJev(risk={a: {"低": 1.0}}, fail_risk=[b])
    assert judge(conn, jev).check("600001", TODAY)[0] == "error"
    assert conn.execute("SELECT id FROM notice_risk").fetchall() == [("AN0",)]


def test_per_check_cap_and_skipped():
    conn = setup(*[(f"甲:减持公告{i}", f"2026-09-{i + 1:02d}") for i in range(25)])
    jev = FakeJev(risk={f"甲:减持公告{i}": {"低": 1.0} for i in range(25)})
    j = judge(conn, jev)
    j.check("600001", TODAY)
    assert jev.risk_calls == 20 and len(j.last_trace["skipped"]) == 5
    assert j.last_trace["skipped"][0]["category"] == "减持"


def test_trace_sorted_by_score_and_fields():
    a, b = "甲:关于股东减持计划的公告", "甲:关于股份质押的公告"
    conn = setup(a, b)
    j = judge(conn, FakeJev(risk={a: {"低": 1.0}, b: {"中": 1.0}}), {"AN1": "质押" * 200})
    j.check("600001", TODAY)
    s = j.last_trace["suspects"]
    assert [x["title"] for x in s] == [b, a]
    assert s[0]["verdict"] == "reject" and s[1]["verdict"] == "pass"
    assert s[0]["days_ago"] == 6 and len(s[0]["excerpt"]) == 200 and s[1]["excerpt"] == ""
    assert set(s[0]) >= {"id", "date", "title", "days_ago", "category", "source", "level", "score",
                         "probs", "body_read", "excerpt", "verdict"}
    json.dumps(j.last_trace, ensure_ascii=False)      # 能写进 decisions.trace
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_notice_judge.py`
Expected: 多数 FAIL（`NoticeJudge` 无 `fetch_body` 参数、trace 结构不同）

- [ ] **Step 3: `jevq/db.py` SCHEMA 加表**

```sql
CREATE TABLE IF NOT EXISTS notice_risk(id TEXT PRIMARY KEY, version INTEGER, category TEXT, level TEXT,
  score REAL, probs TEXT, body_read INTEGER, created TEXT);
```

- [ ] **Step 4: `jevq/judge/jev.py` 的 `ask` 加可选超时**

```python
    def ask(self, state, questions: dict, timeout: float | None = None) -> dict:
        ...
                r = self.session.post(JEV_URL, json=body, headers=headers, timeout=timeout or JEV_TIMEOUT)
```

（只改签名与这一处 `timeout=`，其余不动。）

- [ ] **Step 5: 重写 `jevq/judge/notices.py`**

```python
"""公告排雷（两段式）：标题粗筛疑似 → 读正文让 Jev 按轻重打风险分 → 过线才否决。
设计：docs/specs/2026-09-26-notice-risk-design.md"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from jevq.config import RISK_JEV_TIMEOUT, RISK_MAX_PER_CHECK, RISK_VERSION
from jevq.data.notice_body import fetch_body as default_fetch_body
from jevq.data.notice_body import get_body
from jevq.data.notice_store import notices_for
from jevq.judge.jev import JevError
from jevq.judge.notice_risk import (RISK_CATEGORIES, classify_score, keyword_category, risk_question,
                                    risk_score, risk_state, title_criteria)

TITLE_SOURCE = "jev2"      # 新分类体系的标题分类缓存；旧的 keyword/jev 行不再使用


class NoticeJudge:
    def __init__(self, conn: sqlite3.Connection, jev, workers: int = 8, fetch_body=default_fetch_body):
        self.conn, self.jev, self.workers, self.fetch_body = conn, jev, workers, fetch_body
        self.last_trace: dict = {"suspects": [], "others": 0, "skipped": []}

    def check(self, code: str, today: date) -> tuple[str, str]:
        notices = notices_for(self.conn, code, today)
        failed = False

        # 第一段：挑疑似
        suspects, others, need_title = [], 0, []
        for n in notices:
            cat = keyword_category(n.title)
            if cat:
                suspects.append((n, cat, "keyword"))
                continue
            row = self.conn.execute("SELECT label FROM notice_verdicts WHERE id=? AND source=?",
                                    (n.id, TITLE_SOURCE)).fetchone()
            if row:
                if row[0] in RISK_CATEGORIES:
                    suspects.append((n, row[0], "jev_title"))
                else:
                    others += 1
            else:
                need_title.append(n)
        if need_title:
            with ThreadPoolExecutor(self.workers) as pool:
                results = list(pool.map(self._ask_title, need_title))
            for n, res in zip(need_title, results):
                if res is None:
                    failed = True
                    continue
                label, conf, probs = res
                self._save_title(n.id, label, conf, probs)
                if label in RISK_CATEGORIES:
                    suspects.append((n, label, "jev_title"))
                else:
                    others += 1
        suspects.sort(key=lambda s: (s[0].date, s[0].id), reverse=True)
        judged, skipped = suspects[:RISK_MAX_PER_CHECK], suspects[RISK_MAX_PER_CHECK:]

        # 第二段：细判（缓存 → 正文 → Jev）
        items, pending = [], []
        for n, cat, source in judged:
            row = self.conn.execute("SELECT level, score, probs, body_read FROM notice_risk "
                                    "WHERE id=? AND version=?", (n.id, RISK_VERSION)).fetchone()
            if row:
                level, score, probs, body_read = row
                body = self.conn.execute("SELECT text FROM notice_bodies WHERE id=?", (n.id,)).fetchone()
                items.append(self._item(n, cat, source, level, score, json.loads(probs or "{}"),
                                        bool(body_read), body[0] if body else None, today))
            else:
                pending.append((n, cat, source, get_body(self.conn, n.id, self.fetch_body)))
        if pending:
            with ThreadPoolExecutor(self.workers) as pool:
                answers = list(pool.map(lambda p: self._ask_risk(p[0], p[1], p[3], today), pending))
            for (n, cat, source, body), ans in zip(pending, answers):
                if ans is None:
                    failed = True
                    continue
                level, probs = ans
                score = risk_score(probs)
                self._save_risk(n.id, cat, level, score, probs, body is not None)
                items.append(self._item(n, cat, source, level, score, probs, body is not None, body, today))

        items.sort(key=lambda it: it["score"], reverse=True)
        self.last_trace = {"suspects": items, "others": others,
                           "skipped": [{"date": n.date, "title": n.title, "category": cat} for n, cat, _ in skipped]}
        if failed:
            return "error", "Jev 不可用"
        if not items:
            return "pass", "公告无雷"
        top = items[0]
        if top["verdict"] == "reject":
            return "reject", f"{top['category']}·高风险 {top['score']:.2f}：{top['date']} {top['title']}"
        if top["verdict"] == "unsure":
            return "reject", f"{top['category']}·拿不准 {top['score']:.2f}：{top['date']} {top['title']}"
        return "pass", f"疑似 {len(items)} 条，均判低风险"

    @staticmethod
    def _item(n, cat, source, level, score, probs, body_read, body, today) -> dict:
        return {"id": n.id, "date": n.date, "title": n.title,
                "days_ago": (today - date.fromisoformat(n.date)).days, "category": cat, "source": source,
                "level": level, "score": round(score, 4), "probs": probs, "body_read": body_read,
                "excerpt": (body or "")[:200], "verdict": classify_score(score)}

    def _ask_title(self, n):
        try:
            ans = self.jev.ask({"公告标题": n.title, "公告类型": n.type},
                               {"kind": {"type": "choice", "instructions": "这条A股上市公司公告属于哪一类？",
                                         "criteria": title_criteria()}})["kind"]
        except JevError:
            return None
        return ans["choice"], ans.get("confidence"), ans.get("probabilities")

    def _ask_risk(self, n, cat, body, today):
        try:
            ans = self.jev.ask(risk_state(n, cat, body, today), risk_question(cat),
                               timeout=RISK_JEV_TIMEOUT)["risk"]
        except JevError:
            return None
        return ans["choice"], ans.get("probabilities") or {}

    def _save_title(self, nid, label, conf, probs):
        self.conn.execute("INSERT OR REPLACE INTO notice_verdicts(id, label, confidence, source, probs) "
                          "VALUES (?, ?, ?, ?, ?)",
                          (nid, label, conf, TITLE_SOURCE, None if probs is None else json.dumps(probs, ensure_ascii=False)))
        self.conn.commit()

    def _save_risk(self, nid, cat, level, score, probs, body_read):
        self.conn.execute("INSERT OR REPLACE INTO notice_risk(id, version, category, level, score, probs, body_read, created) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (nid, RISK_VERSION, cat, level, score, json.dumps(probs, ensure_ascii=False),
                           int(body_read), datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
```

- [ ] **Step 6: 运行新测试通过**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider tests/test_notice_judge.py`
Expected: PASS

- [ ] **Step 7: 修复其他受影响的测试**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider`
引用了 `classify_by_keyword`、`MINE_LABELS`、旧 trace 字段（`mine`、`unsure`、`label`、`probabilities`）或旧 FakeJev 返回 `kind` 答案来驱动否决的测试（如 `tests/test_engine.py`、`tests/test_board_strategy.py`、`tests/test_custom_strategy.py`、`tests/test_notify_content.py`）：
- 策略测试若只是用假 `notice_judge`（自带 `check` 返回值）的，不需要改。
- 真正构造 `NoticeJudge` 的：按新语义更新假 Jev（加 `risk` 答案）与断言，并在测试 docstring 注明「2026-09-26 公告两段式细判：语义更新」。不得删除测试或削弱其断言的意图（例：原来断言「减持公告 → 否决」的，改成「减持公告 + 高风险正文 → 否决」）。
- 推送/面板相关的 trace 断言留给 Task 4，此处若失败只允许做最小改动让其读新结构，或标注由 Task 4 更新——**不要**用 `xfail`/`skip`。

- [ ] **Step 8: 全量测试 + 提交**

```bash
.venv/Scripts/python -m pytest -q -p no:cacheprovider
git add jevq/judge/notices.py jevq/judge/jev.py jevq/db.py tests/
git commit -m "feat: two-stage notice judge (title screen + body risk scoring)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 推送、面板、报告的展示

**Files:**
- Modify: `jevq/notify_content.py`（`buy_push_notices_line`）、`jevq/report.py`（新增统计小节）、`jevq/app/ui.html`（「公告风险」区块）
- Test: `tests/test_notify_content.py`、`tests/test_report.py`、`tests/test_ui.py`

**Interfaces:**
- Consumes: Task 3 `last_trace` dict 结构（经 `decisions.trace` 的 `"notices"` 键读回）
- Produces:
  - `buy_push_notices_line(notices: dict | list) -> list[str]`（list = 旧格式，保留原输出）
  - `report.notice_risk_stats(conn, day: date) -> dict`：`{"judged": int, "reject": int, "unsure": int, "unread": int, "by_category": dict[str, int]}`（按公告 id 去重，统计当天所有 decisions.trace 中 `notices` 为 dict 的疑似项）
  - 报告新增 `## 公告风险统计` 小节

- [ ] **Step 1: 写推送测试**（追加到 `tests/test_notify_content.py`）

```python
from jevq.notify_content import buy_push_notices_line


def test_push_notices_new_format_top3_by_score():
    trace = {"suspects": [
        {"date": "2026-09-04", "title": "甲:股东减持计划公告很长很长很长很长", "category": "减持", "level": "低",
         "score": 0.18, "body_read": True, "verdict": "pass"},
        {"date": "2026-09-02", "title": "甲:回购进展", "category": "回购", "level": "低",
         "score": 0.12, "body_read": False, "verdict": "pass"},
        {"date": "2026-09-01", "title": "甲:质押", "category": "质押", "level": "无",
         "score": 0.02, "body_read": True, "verdict": "pass"},
        {"date": "2026-08-01", "title": "甲:第四条", "category": "质押", "level": "无",
         "score": 0.01, "body_read": True, "verdict": "pass"}],
        "others": 12, "skipped": []}
    lines = buy_push_notices_line(trace)
    assert "疑似 4 条" in lines[0] and "其余 12 条无关" in lines[0]
    assert len(lines) == 4
    assert "减持·低 0.18" in lines[1] and "放行" in lines[1] and "已读正文" in lines[1]
    assert "仅标题" in lines[2]


def test_push_notices_new_format_no_suspects():
    lines = buy_push_notices_line({"suspects": [], "others": 5, "skipped": []})
    assert lines == ["▍Jev 公告排雷（近 90 天 5 条公告，无疑似雷）"]


def test_push_notices_legacy_list_unchanged():
    lines = buy_push_notices_line([{"date": "2026-09-01", "title": "甲:公告", "source": "keyword"}])
    assert lines[0].startswith("▍Jev 公告排雷") and "标题关键词判定" in lines[1]
```

- [ ] **Step 2: 实现推送**（`jevq/notify_content.py`，替换 `buy_push_notices_line`）

```python
def buy_push_notices_line(notices) -> list[str]:
    if isinstance(notices, list):          # 2026-09-26 之前的旧 trace
        lines = [f"▍Jev 公告排雷（近 {NOTICE_LOOKBACK_DAYS} 天 {len(notices)} 条，全部放行）"]
        for item in notices[:5]:
            date_str = (item.get("date") or "")[5:10]
            title = (item.get("title") or "")[:20]
            if item.get("source") == "keyword":
                lines.append(f"· {date_str} {title} → 标题关键词判定，未调用 Jev")
            else:
                probs = fmt_probs(item.get("probabilities"), top=2)
                line = f"· {date_str} {title} → {item.get('label')}"
                lines.append(f"{line} {probs}" if probs else line)
        return lines
    suspects, others = notices.get("suspects") or [], notices.get("others", 0)
    if not suspects:
        return [f"▍Jev 公告排雷（近 {NOTICE_LOOKBACK_DAYS} 天 {others} 条公告，无疑似雷）"]
    lines = [f"▍Jev 公告排雷（近 {NOTICE_LOOKBACK_DAYS} 天：疑似 {len(suspects)} 条，其余 {others} 条无关）"]
    for s in suspects[:3]:
        read = "已读正文" if s.get("body_read") else "仅标题"
        lines.append(f"· {(s.get('date') or '')[5:10]} {(s.get('title') or '')[:20]} → "
                     f"{s.get('category')}·{s.get('level')} {float(s.get('score') or 0):.2f}（{read}）放行")
    return lines
```

（主账户买入时 trace 一定是放行的，所以固定写「放行」。）

- [ ] **Step 3: 写报告测试**（追加到 `tests/test_report.py`）

```python
def test_report_notice_risk_stats(tmp_path):
    import json
    from datetime import datetime
    from jevq import db
    from jevq.ledger import Ledger
    from jevq.report import notice_risk_stats, write_report
    conn = db.connect(":memory:")
    ledger = Ledger(conn)
    t = datetime(2026, 9, 28, 14, 50).isoformat()
    s1 = {"id": "AN1", "category": "减持", "verdict": "reject", "body_read": True}
    s2 = {"id": "AN2", "category": "回购", "verdict": "pass", "body_read": False}
    s3 = {"id": "AN3", "category": "减持", "verdict": "unsure", "body_read": True}
    rows = [(t, "600001", "reject", "减持·高风险", json.dumps({"notices": {"suspects": [s1, s2], "others": 3, "skipped": []}})),
            (t, "600002", "reject", "减持·拿不准", json.dumps({"notices": {"suspects": [s3, s1], "others": 0, "skipped": []}})),
            (t, "600003", "pass", "旧记录", json.dumps({"notices": [{"title": "旧"}]}))]
    conn.executemany("INSERT INTO decisions(ts, code, verdict, detail, trace) VALUES (?, ?, ?, ?, ?)", rows)
    st = notice_risk_stats(conn, datetime(2026, 9, 28).date())
    assert st == {"judged": 3, "reject": 1, "unsure": 1, "unread": 1, "by_category": {"减持": 2, "回购": 1}}
    text = write_report(conn, ledger, datetime(2026, 9, 28).date(), {}, "中性", "正常", tmp_path).read_text(encoding="utf-8")
    assert "## 公告风险统计" in text and "| 减持 | 2 |" in text
```

- [ ] **Step 4: 实现报告**（`jevq/report.py`）

```python
def notice_risk_stats(conn, day: date) -> dict:
    """当天所有决策留痕里的疑似公告（按公告 id 去重）统计。旧格式（list）trace 跳过。"""
    seen: dict[str, dict] = {}
    for (raw,) in conn.execute("SELECT trace FROM decisions WHERE substr(ts, 1, 10)=? AND trace IS NOT NULL",
                               (day.isoformat(),)):
        try:
            notices = (json.loads(raw) or {}).get("notices")
        except (ValueError, AttributeError):
            continue
        if not isinstance(notices, dict):
            continue
        for s in notices.get("suspects") or []:
            if s.get("id"):
                seen[s["id"]] = s
    by_cat: dict[str, int] = {}
    for s in seen.values():
        by_cat[s.get("category") or "?"] = by_cat.get(s.get("category") or "?", 0) + 1
    return {"judged": len(seen),
            "reject": sum(1 for s in seen.values() if s.get("verdict") == "reject"),
            "unsure": sum(1 for s in seen.values() if s.get("verdict") == "unsure"),
            "unread": sum(1 for s in seen.values() if not s.get("body_read")),
            "by_category": by_cat}
```

在 `write_report` 中「## 主账户检查记录」表格之后、「## 今日首板时段分布」之前插入：

```python
    st = notice_risk_stats(conn, day)
    lines += ["", "## 公告风险统计", "",
              f"细判 {st['judged']} 条｜否决：高风险 {st['reject']}、拿不准 {st['unsure']}｜未读到正文 {st['unread']}", ""]
    if st["by_category"]:
        lines += ["| 类别 | 条数 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(st["by_category"].items(), key=lambda kv: -kv[1])]
```

（`report.py` 顶部若没有 `import json` 则补上。）

- [ ] **Step 5: 面板「公告风险」区块**（`jevq/app/ui.html`，决策详情里 `if (t.notices) {` 整块替换为）

```js
  if (Array.isArray(t.notices)) {
    // …… 原有旧格式渲染代码原样保留在这个分支里（flagged/rest/noticeRow/展开全部）……
  } else if (t.notices) {
    const sus = t.notices.suspects || [], skipped = t.notices.skipped || [];
    const LV = {"高": "down", "中": "warn", "低": "", "无": ""};
    const bar = (p) => ["高", "中", "低", "无"].map((k) =>
      `<span class="tag">${k} ${fmt((p || {})[k] || 0)}</span>`).join(" ");
    const row = (s, i) => [esc(s.date), esc(s.title), esc(s.category),
      `<b>${esc(s.level)} ${fmt(s.score)}</b>${s.verdict === "reject" ? " ❌" : s.verdict === "unsure" ? " ⚠️拿不准" : ""}`,
      bar(s.probs), s.body_read ? "已读正文" : "仅标题",
      s.excerpt ? `<details><summary>摘要</summary>${esc(s.excerpt)}</details>` : ""];
    parts.push(`<h4>公告风险（近 90 天：疑似 ${sus.length} 条，其余 ${esc(t.notices.others || 0)} 条无关公告）</h4>` +
      (sus.length ? table(["日期", "标题", "类别", "等级·分数", "概率", "正文", ""], sus.map(row), [1, 6])
                  : '<div class="tag">没有疑似雷的公告</div>') +
      (skipped.length ? `<div class="tag">另有 ${skipped.length} 条疑似超过每票上限，未细判</div>` : ""));
  }
```

说明：把原来 `if (t.notices) { ... }` 里的全部旧代码移入 `if (Array.isArray(t.notices)) { ... }` 分支，一字不改。`table(headers, rows, wrapCols)` 为现有工具函数——实现前在 ui.html 中核对它的签名与「第三个参数表示哪些列允许换行」的语义，按实际签名调用；`LV` 若未使用就删除。

`tests/test_ui.py` 追加：

```python
def test_decision_detail_renders_new_notice_risk_and_legacy_list():
    js = _scripts()
    assert "Array.isArray(t.notices)" in js and "t.notices.suspects" in js
    assert "已读正文" in js and "仅标题" in js
```

- [ ] **Step 6: 全量测试 + 提交**

```bash
.venv/Scripts/python -m pytest -q -p no:cacheprovider
git add jevq/notify_content.py jevq/report.py jevq/app/ui.html tests/test_notify_content.py tests/test_report.py tests/test_ui.py
git commit -m "feat: show notice risk in push, panel and report" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 上线前对照脚本（controller 执行）

**Files:**
- Create: `scripts/notice_risk_replay.py`

**Interfaces:**
- Consumes: `NoticeJudge`（Task 3）、`JevClient`、`db.connect`
- Produces: 命令行输出对照表；不写 `decisions`，不影响交易。

- [ ] **Step 1: 创建脚本**

```python
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
```

- [ ] **Step 2: 语法检查 + 提交**（不在测试里运行它——它联网）

```bash
.venv/Scripts/python -m py_compile scripts/notice_risk_replay.py
git add scripts/notice_risk_replay.py
git commit -m "chore: notice risk replay script for pre-launch comparison" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3（controller）：** 非盘中时段，以用户环境变量中的 key 运行脚本（不打印 key），把对照表交给用户确认；用户同意后，才在收盘后/非交易时段重启 daemon 上线，并推送 GitHub（先做密钥检查）。
