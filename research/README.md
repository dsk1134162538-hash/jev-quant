# Qlib 独立研究验证

只用于历史研究和风险分析，不连接实时盯盘、模拟账户、券商或真实交易执行器。

```bash
./.venv/Scripts/python.exe -m pip install -r requirements-research.txt
./.venv/Scripts/python.exe -m research.qlib_validation
```

默认通过 AKShare 的新浪接口下载 5 只样本股的前复权日线与沪深 300，训练区间早于 2026-01-01，2026-01-01 起为样本外。T 日收盘动量信号在 T+1 日开盘生效，使用开盘到下一开盘收益，买入成本 0.05%、卖出成本 0.15%。Qlib 只负责 `risk_analysis` 风险指标。

结果写入 `research/artifacts/qlib-validation/`。下载后的真实行情已保存时，可用 `--reuse-data` 完全离线复算：

```bash
./.venv/Scripts/python.exe -m research.qlib_validation --reuse-data
```

可自定义 `--symbols 002553 603001`、`--start`、`--end`、`--split` 和 `--output`。下载失败会明确报错，不会以合成数据替代真实报告。

此工程验证不覆盖涨跌停撮合、T+1、整手、最低佣金、滑点或部分成交，样本结果不能证明策略有效或未来收益。

## K 线与成交量研究模型

`kline_model.py` 将仓库知识库中的周期环境、K 线实体与影线、均线位置、平台突破、缩量回踩、量价配合和相对强弱转换为 41 个数值特征。LightGBM 学习 T 日收盘特征与 T+1 开盘至 T+2 开盘净收益之间的关系；2022—2024 年训练，2025 年选择阈值，2026 年最终样本外测试。股票代码不作为特征。

```bash
# 首次运行：下载 30 只当前沪深 300 主板成分股的真实历史数据并训练
./.venv/Scripts/python.exe -m research.kline_model

# 新行情到达后刷新并训练；输入未变化时自动跳过
./.venv/Scripts/python.exe -m research.kline_model --refresh

# 强制重新训练
./.venv/Scripts/python.exe -m research.kline_model --force
```

结果写入 `research/artifacts/kline-model/`，包括模型、预测、逐日收益、特征重要性和报告。`--end` 默认取运行当天；验证集选出的分数门槛会固定应用于后续测试，不会在测试区间重新拟合门槛。

报告同时列出 IC、AUC、十分位收益和日收益均值 t 统计量。当前模型 `deployment_eligible=false`：整体样本外排序能力接近随机，少数高分交易的区间收益不构成部署依据。当前成分股还存在幸存者偏差，且未覆盖行业、消息、分钟行情和真实撮合，不能由测试指标推断未来收益或启用实盘。

每日自动重训入口为 `research/train_kline_automation.py`，调用项目 `.venv` 刷新真实数据并输出研究状态。它只执行研究代码，不导入实时盯盘、账户或交易模块；输入数据未变化时返回 `unchanged`。

## 五日横截面相对强弱模型

`cross_sectional_model.py` 使用同一组 41 个量价特征，但训练目标改为：T 日收盘生成信号，T+1 开盘至 T+6 开盘的五日收益先相对沪深 300 去市场 beta，再按同一信号日股票池转换成横截面排名。默认使用 504 个信号日训练、126 日验证、126 日样本外测试、126 日步长；训练／验证／测试之间保留 6 日隔离，并按实际退出日再次 purge。LightGBM 只用验证集 early stopping，不根据测试折调参。

```bash
# 复用已有真实行情，运行非 PIT 管线冒烟基线
./.venv/Scripts/python.exe -m research.cross_sectional_model --allow-non-pit-sample
```

产物写入 `research/artifacts/cross-sectional-model/`：三折模型、样本外预测、逐日 RankIC、五分层收益、非重叠最高分位组合、换手扣费、逐折指标和 `summary.json`。组合买入成本为 0.05%、卖出成本为 0.15%；多空分层价差只作诊断，不视为 A 股可执行收益。

当前 30 只真实行情冒烟基线覆盖 378 个样本外信号日：RankIC 均值 0.0254、t 值 1.835、日 ICIR 0.0944，特征覆盖率 99.94%；第三折 RankIC 为 −0.0092，五分层收益不单调。最高分位非重叠组合扣费区间收益 13.85%，最大回撤 −14.86%，但收益均值 t 值仅 0.724。该样本是当前成分股，不是历史时点全市场股票池，存在幸存者偏差；结果不能证明策略有效，`deployment_eligible=false`。

iFinD／Wind 的当前可调用接口未提供完整历史时点全市场股票池；财新已抽样验证上市／退市主数据接口，但全量历史回溯需要分页及公司行动等数据配合。取得完整 PIT 股票池后，使用相同输入 CSV 合约并以 `--universe-mode pit_full_market` 运行，不能把非 PIT 冒烟结果改名为全市场结果。

## 全 A 历史主数据续传

`pit_backfill.py` 将上海主板、深圳主板、创业板、科创板和北交所分别回溯，拒绝任何达到 2,000 条截断线的分区。2026-10-27 已完成 5,936 条历史记录、299 页的下载与合并。该数量含上市、终止上市及其他状态的历史证券，不等于当前可交易股票数。续传台账共有 294 次成功请求和 2 次失败请求；失败中一次为已报告 20 积分的响应校验失败，一次为未报告积分的网络错误。

```bash
# 默认不发起新计费请求，只生成 manifest 并复用五个探针首页
./.venv/Scripts/python.exe -m research.pit_backfill

# 明确批准积分消耗后才执行；上限约束本轮新增请求数，可分段续传
./.venv/Scripts/python.exe -m research.pit_backfill --execute --max-paid-calls 50
```

产物位于 `research/artifacts/pit-backfill/`。`manifest.json` 记录各分区总数、缺页和剩余估算积分；`request_ledger.jsonl` 逐次记录成功或失败请求及响应中报告的积分。页面先写临时文件，校验 A 股类型、板块、行数及统一编码后才原子替换；跨分区统一编码重复时拒绝合并。全部 299 页齐备后才生成 `universe_all_a_history.csv`。

面板只读接口为 `GET /api/research/cross-sectional`，由 `research/status.py` 投影摘要和各折结果。它不会触发训练、替换 champion、连接账户或调用交易执行器。

## Kronos-small 零样本对照

官方代码固定在 `third_party/Kronos`（commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a`），模型和 tokenizer 分别固定 revision `901c26c1332695a2a8f243eb2f37243a37bea320`、`0e0117387f39004a9016484a186a908917e22426`。依赖安装在项目虚拟环境，权重缓存位于项目 `.cache/huggingface`，官方 512 上下文 CPU 回归已通过。

```bash
# 可选依赖
./.venv/Scripts/python.exe -m pip install -r requirements-kronos.txt

# 真实权重小样本链路检查：第 1 折前 2 个测试日，每日等距选择 16 只
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --fold 1 --smoke-days 2 --smoke-names 16

# 全量零样本对照；按折续跑，不微调、不自动替换基线
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --fold 1
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --fold 2
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --fold 3
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --fold 4
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --fold 5
./.venv/Scripts/python.exe -m research.kronos_cross_sectional --finalize
```

Kronos 仅读取 T 日及之前的 512 根 OHLCV，预测 T+1 至 T+6 六根日线，以预测 T+6 开盘价／T+1 开盘价减一形成横截面得分。评价复用 LightGBM 基线的测试折、RankIC、五分组、非重叠组合与成本口径。固定 `top_k=1`、单路径和随机种子；股票代码不是特征。

2026-10-28 已完成 2 日 × 16 只共 32 条真实权重冒烟，产物在 `research/artifacts/pit-kronos-small-smoke/`。其全市场覆盖率约 0.30%，`comparison_eligible=false`，不得据此比较模型优劣。全量五折尚未运行；官方 predictor 的 amount 是由 OHLC 均价乘 volume 推导，并非真实成交额。日线预测仍不覆盖涨跌停锁死、部分成交、市场冲击和最低佣金。

2026-10-28 另完成 2 日 × 256 只共 512 条 CPU 速度基准，100 个实际批次累计 518 秒，吞吐约 0.988 条／秒。按相同硬件、批量大小和历史长度分布连续运行估算，五折分别约 7.88、7.89、7.94、8.01、8.11 天，全量约 39.83 天；实际全量可能更慢。不同历史长度会产生未满批次，因此不能按 32 条小冒烟直接外推。结果保存在 `research/artifacts/pit-kronos-small-benchmark/benchmark.json`，仅用于速度评估，不是业绩对照。续跑会在构造上下文前跳过已保存键，避免重复推理。

随后按精确上下文长度跨信号日分组，并缓存每只股票的排序结果、日期索引和各信号日未来时间戳。相同样本复测为 98 批、508 秒、1.008 条／秒，仅比原基准快约 1.9%；512 个键和分数逐项完全一致。优化产物位于 `research/artifacts/pit-kronos-small-optimized-benchmark/`，全量时间估算约 39.06 天，仍未启动全量五折。主要成本仍是模型推理，分组优化不会带来数量级提速；当前轻量元数据仍在内存中物化，全量运行如需限制内存，应先实现外部分桶或分块处理。

面板通过 `GET /api/research/kronos` 只读展示上述状态；接口不加载模型、不触发推理或训练，也不连接交易。
