# 自建板块（只记录）实现计划

设计：`docs/specs/2026-10-04-own-sectors-design.md`（含 2026-10-04 数据探测结果）。只做第 1 步：分类库 + 日快照 + 面板，**不接任何策略**。

范围口径（实现时定的，已回写 spec）：只收**主板 60/00 股票池**内的票（和策略可交易范围一致，`universe`）；连板高度用收盘价历史 + 当日涨停标记算。

任务（每步带测试，最后全量 pytest）：
1. `db.py`：表 `own_sectors / own_members / own_product_map / own_cache / own_sector_daily`；`config.py`：`OWN_SECTORS_*`。
2. `jevq/data/own_sectors_seed.json`：种子板块（首版整理自 9-30 晚报，**待用户审**）。
3. `jevq/data/own_sectors.py`：种子加载、取数（主营构成/介绍，带重试与缓存）、产品行整理（排除「其中:」细分、其他）、候选（新浪 + 晚报点名）、占比→tier、成员刷新（带预算）、板块日快照与龙头排序。
4. `jevq/judge/own_sectors.py`：产品→板块映射（choice，结果缓存）、主营介绍判断（choice）、板块强弱（score，复用情绪的四档）。
5. `run.py`：收盘后一步（报告之后），try/except + 预算 + 开关；不碰交易循环。
6. `api.py`：`own_sectors()`（只读）、`own_sector_review()`、`own_sector_decide()`（只写自建板块自己的表）；家里只读面板拦截写操作。
7. `ui.html`：「自建板块」页（只读 + 确认/驳回），不参加 5 秒刷新。
8. `report.py`：「自建板块（只记录）」一小节。
9. 隔离测试：开启前后 `decisions/fills` 不变；HANDOFF 新增一节；spec/计划回写。
