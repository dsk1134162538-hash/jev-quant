export const marketSeries = {
  上证指数: [3948, 3921, 3908, 3886, 3912, 3934, 3926, 3941],
  深证成指: [13120, 13042, 12980, 12891, 12944, 13012, 13006, 13058],
  创业板指: [3128, 3094, 3076, 3021, 3048, 3066, 3052, 3074],
}

export const markets = [
  { name: '上证指数', value: '3,941.26', change: '+0.38%', direction: 'up', breadth: '上涨 2,184' },
  { name: '深证成指', value: '13,058.40', change: '+0.42%', direction: 'up', breadth: '上涨 2,506' },
  { name: '创业板指', value: '3,074.18', change: '-0.16%', direction: 'down', breadth: '上涨 612' },
]

export const sectors = [
  { name: '电网设备', fiveDay: 4.8, today: 1.62, volume: '1.28×', continuity: '连续 3 日', verdict: '观察延续', tone: 'good' },
  { name: '工业机器人', fiveDay: 3.1, today: 0.86, volume: '1.12×', continuity: '连续 2 日', verdict: '等待回踩', tone: 'good' },
  { name: '创新药', fiveDay: 0.7, today: 1.94, volume: '0.96×', continuity: '首日异动', verdict: '不追一日强', tone: 'warn' },
  { name: '半导体', fiveDay: -2.4, today: 0.32, volume: '0.82×', continuity: '缩量修复', verdict: '证据不足', tone: 'muted' },
]

export const decisions = [
  {
    id: 'D-20261027-001', time: '08:45', type: '盘前计划', title: '不因反弹预期提前满仓', action: '观望',
    market: '大盘仍处在弱修复假设中，盘前无法确认指数低点已经抬高。', sector: '电网设备与机器人仅进入观察池，需等待多股共同响应，而非只看一只高开。',
    stock: '暂不预选具体买入标的。开盘后再排除高开过多、流动性不足与风险公告个股。',
    trigger: '指数回踩不破早盘低点；候选板块强于指数；个股重新站上均价且成交配合。',
    invalidation: '指数再创新低、板块仅单股脉冲或买入逻辑被公告证伪。', risk: '中低', status: '待验证',
  },
  {
    id: 'D-20261027-002', time: '10:15', type: '盘中检查', title: '反弹成立，但主线持续性仍待确认', action: '等待',
    market: '演示情形下指数出现反抽，尚不能把单日反抽直接外推成趋势反转。', sector: '电网设备有多股响应；创新药只有首日强度，暂不按强主线处理。',
    stock: '只观察相对板块更强、历史位置不过高且回踩有承接的个股。',
    trigger: '午后量能不显著衰减，板块内领涨与跟随结构保持完整。', invalidation: '冲高后跌破均价且板块同步回落。',
    risk: '中', status: '待复盘',
  },
  {
    id: 'D-20261027-003', time: '15:20', type: '盘后复盘', title: '记录预判与实际的偏差', action: '复盘',
    market: '对照盘前假设检查趋势判断是否过早，不用收盘结果倒推当时一定应该买入。', sector: '统计板块持续天数、内部响应广度和次日反馈，排除只凭概念标签定主线。',
    stock: '比较候选股与板块、指数的超额表现，并复盘最低佣金、滑点和 T+1 后是否仍有空间。',
    trigger: '样本达到预设要求后，再进行时间顺序隔离的样本外验证。', invalidation: '样本不足时不给策略有效性下结论。',
    risk: '研究', status: '待执行',
  },
]

export const reviews = {
  盘前: {
    label: '08:45 · 盘前计划', title: '先定义什么不做，再等待可验证的机会',
    facts: ['当前执行边界为 dryrun，AI 实盘未启用。', '策略样本不足，不能把昨日反弹直接当作今日胜率。', '一万元账户需优先计入最低佣金与一手起买的约束。'],
    inferences: ['若指数低开后不再创新低，低吸假设才进入观察阶段。', '强主线需要板块广度、持续性和量能共同确认。'],
    actions: ['盘前不挂出 AI 真实委托。', '建立候选板块与失效条件，盘中逐项核对。', '没有符合条件的机会时保持空仓。'],
  },
  盘中: {
    label: '12:05 · 午间复盘', title: '把上午发生的事和上午之前的预判分开',
    facts: ['本区域为界面演示，未读取家里实时行情。', '上午是否放量、板块是否多股响应，需要真实数据填充。', '任何买入都必须有明确失效位和次日退出计划。'],
    inferences: ['反弹可能有交易价值，但不应倒推为必然可捕捉。', '若只有指数修复而板块不跟，个股胜率假设仍不足。'],
    actions: ['午后只跟踪已写明的触发条件。', '不因日收益目标临时放宽标准。', '记录错过机会与主动放弃的区别。'],
  },
  盘后: {
    label: '15:20 · 盘后复盘', title: '复盘决策质量，而不只复盘盈亏',
    facts: ['待导入当日真实决策、意图、委托和成交。', '待比较盘前、盘中与收盘三个时点的判断。', '待核对交易成本与可执行滑点。'],
    inferences: ['盈利的交易也可能来自坏决策，亏损的交易也可能是合理风控。', '只有多样本和时间隔离验证，才能判断规则是否值得保留。'],
    actions: ['输出今日事实、推断、行动与偏差。', '更新假设样本，不事后修改当时记录。', '形成明日观察条件，空仓也写入结论。'],
  },
}

export const experiments = [
  { name: '弱市低吸 · 次日退出', version: 'H-01', status: '样本不足', samples: 0, baseline: '当日等权', description: '验证指数企稳、板块共振与个股承接同时出现时，是否存在扣除成本后的正向期望。' },
  { name: '强主线持续识别', version: 'H-02', status: '样本不足', samples: 0, baseline: '首日强势基线', description: '比较板块广度、连续强度与量能组合，能否区分持续主线和一日游。' },
  { name: '空仓规则对照', version: 'H-03', status: '样本不足', samples: 0, baseline: '始终交易', description: '将空仓作为主动决策，与强制每日交易比较回撤、成本与风险调整后的表现。' },
]

export const systemItems = [
  { name: '执行模式', value: 'dryrun', note: 'AI 非演练买卖均被阻断', state: 'good' },
  { name: '交易网关', value: '未连接', note: '等待家里电脑只读验证', state: 'warn' },
  { name: '自动验证码', value: '待现场验证', note: '代码保护已测，客户端未联调', state: 'warn' },
  { name: '行情与数据库', value: '演示数据', note: '尚未读取家里 SQLite 与行情', state: 'muted' },
  { name: '真实委托', value: '未启用', note: '没有一键切换 live 入口', state: 'good' },
  { name: '策略验证', value: 'insufficient_data', note: '工程通过不等于策略有效', state: 'muted' },
]
