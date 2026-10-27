export const snapshot = {
  asOf: '2026-10-27T09:53:00+08:00',
  displayTime: '2026-10-27 09:53',
  fetchedAt: '2026-10-27T09:56:36+08:00',
  phase: '盘中',
  regime: '下跌趋势 · 暂沿用',
  mode: '防守观察',
  source: 'iFinD 真实行情 + ai_experiment 只读账户',
}

export const markets = [
  { name: '上证指数', value: '—', change: '今日未返回', breadth: '历史 10/23 收盘 3,690.48' },
  { name: '深证成指', value: '—', change: '今日未返回', breadth: '历史 10/23 收盘 11,673.83' },
  { name: '创业板指', value: '—', change: '今日未返回', breadth: '历史 10/23 收盘 2,684.10' },
]

export const observations = [
  { name: '万向钱潮', code: '000559.SZ', price: '12.54', change: -1.34, minute: '+0.24%', volume: '819,700', amount: '10,269,948', verdict: '相对抗跌，仍无买点', tone: 'warn' },
  { name: '大丰实业', code: '603081.SH', price: '10.40', change: -7.96, minute: '+1.07%', volume: '157,100', amount: '1,623,741', verdict: '单分钟回升≠修复', tone: 'warn' },
  { name: '睿能科技', code: '603933.SH', price: '18.33', change: -10.01, minute: '0.00%', volume: '103,300', amount: '1,893,489', verdict: '需核验跌停与可成交性', tone: 'muted' },
]

export const decisions = [
  {
    id: 'D-20261027-0953', time: '09:53', type: '盘中决策', title: '样本补跌，继续空仓防守', action: '空仓', risk: '未开仓', status: '真实记录',
    market: '今日主要指数请求返回 EMPTY_DATA，全市场宽度与成交额缺失。下跌趋势只是暂沿用的工作状态，并非今日大盘已经确认。',
    sector: '三个机器人分层观察样本全部下跌，支持样本内补跌与退潮判断；三个样本不能代表完整板块。',
    stock: '万向钱潮 -1.34%、大丰实业 -7.96%、睿能科技 -10.01%。当前无合格候选，不生成 quote.json，不调用 buy 或 sell。',
    trigger: '只有指数停止创新低、板块拒绝新低、核心翻红、多股梯队与量价共同确认后，才评估小仓试错。',
    invalidation: '板块广度收缩、核心再次翻绿或批量补跌、候选跌破买入依据，或 Quote 过期和可成交性无法核验，立即放弃。',
  },
  {
    id: 'D-20261027-0938', time: '09:38', type: '盘中检查', title: '开盘初期证据不足，不预设买点', action: '观察', risk: '未开仓', status: '真实记录',
    market: '开盘初期暂沿用下跌趋势，但当时两次指数请求未返回有效数据，不能用昨日盘面替代今日确认。',
    sector: '昨日机器人样本尾盘四涨两跌只作历史对照，不能冒充今日板块表现。',
    stock: '模拟账户现金 10,000 元、持仓为空；没有新鲜完整 Quote 和合格标的。',
    trigger: '等待今日有效指数、市场宽度、板块广度和量价共振。',
    invalidation: '数据缺失、证据冲突或执行约束无法核验时，不生成模拟成交。',
  },
  {
    id: 'D-20261027-0837', time: '08:37', type: '盘前计划', title: '盘前只写情景，不提前开仓', action: '观察', risk: '未开仓', status: '真实记录',
    market: '盘前暂定下跌趋势并维持防守观察，海外反弹不直接等同于 A 股趋势反转。',
    sector: '观察指数弱时机器人方向是否具有相对强度，昨日三只封板股不直接视为今日可成交买点。',
    stock: '账户空仓，盘前无合格候选，不预设开仓指令。',
    trigger: '竞价强度、首板梯队与板块广度只用于候选观察；确认后才评估小仓试错。',
    invalidation: '若开盘宽度与指数同步恶化，维持空仓；不追高，不修改参数与样本切分。',
  },
]

export const reviews = {
  盘前: {
    label: '08:37 · 盘前计划', title: '先定义什么不做，再等待可验证的机会', recordStatus: '真实记录',
    facts: ['模拟账户现金 10,000 元，持仓为空。', '昨日机器人观察样本分化，不能外推为整个板块已经瓦解。', '盘前没有今日集合竞价和完整板块广度。'],
    inferences: ['指数弱而板块强时才进入相对强度观察。', '昨日封板不等于今天有可成交买点。'],
    actions: ['不预设开仓指令。', '开盘核验指数、宽度、成交额和板块广度。', '任一证据不足即继续空仓。'],
  },
  盘中: {
    label: '09:53 · 盘中决策', title: '样本补跌证据增强，但不拿样本替代大盘', recordStatus: '真实记录',
    facts: ['万向钱潮 -1.34%、大丰实业 -7.96%、睿能科技 -10.01%。', '主要指数返回 EMPTY_DATA，全市场宽度和成交额缺失。', '模拟账户现金 10,000 元、持仓为空，无模拟成交。'],
    inferences: ['样本内补跌与退潮证据较昨日尾盘增强。', '单分钟回升不能确认修复，三个样本不能代表整个板块。', '关键数据缺失时不做伪精确评分。'],
    actions: ['维持空仓，不生成 quote.json，不调用 buy 或 sell。', '等待指数停止创新低、板块多股共振和核心翻红。', '未来买入先写失效条件，再核验 Quote、费用、100 股和 T+1。'],
  },
  盘后: {
    label: '15:20 · 盘后复盘', title: '尚未到盘后复盘时间', recordStatus: '待复盘',
    facts: ['盘后真实指数、宽度、成交额和完整板块数据待补齐。', '当日委托与成交记录待核对。'],
    inferences: ['不能用收盘结果倒推 09:53 一定存在必然买点。', '空仓是否有效要结合当时证据，而不是只看当天涨跌。'],
    actions: ['收盘后追加记录，不覆盖盘中判断。', '对照盘前与盘中情景检查触发和证伪。', '保留缺失数据与未成交原因。'],
  },
}

export const experiments = [
  { name: '板块共振假设', version: 'H1', status: '样本外 · 数据不足', samples: '17 / 34', baseline: '多股首板 vs 单股首板', description: '样本外差值 +2.8632 个百分点，但有效标签仅 17 对 34 条，训练与验证差值为负，不能据此调参或宣布有效。' },
  { name: '早盘 vs 尾盘首板', version: 'H2', status: '样本外 · 数据不足', samples: '53 / 4', baseline: '早盘首板 vs 尾盘首板', description: '样本外早盘与尾盘有效标签为 53 对 4 条，尾盘对照不足，不支持把早盘首板当成已验证优势。' },
  { name: '市场状态分组', version: 'H3', status: '样本外 · 缺少对照', samples: '0', baseline: '指数前 5 日正收益分组', description: '样本外没有指数前 5 日收益为正的对照事件，无法判断该市场状态分组是否有效。' },
]

export const systemItems = [
  { name: '执行模式', value: 'paper / dryrun', note: '模拟账户，liveTrading=false', state: 'good' },
  { name: '交易网关', value: '未连接', note: '桌面面板不调用交易接口', state: 'warn' },
  { name: '自动验证码', value: '待现场验证', note: '本次未读取、未使用', state: 'warn' },
  { name: '行情快照', value: '真实 · 09:53', note: 'iFinD 个股真实快照；今日指数缺失', state: 'good' },
  { name: '真实委托', value: '未启用', note: '没有下单、撤单或切换实盘入口', state: 'good' },
  { name: '策略验证', value: 'insufficient_data', note: '工程通过不等于策略有效', state: 'muted' },
]
