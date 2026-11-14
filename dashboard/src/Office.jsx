// 办公室里的人和排班：谁是谁、长什么样、几点在哪、开口说什么。画面在 IsoOffice.jsx（等距像素办公室），这里不画东西。
// （原来这个文件里还有前四版平视办公室的整套 SVG，2026-11-12 换成等距版后没人用了，已删；要看去翻 git 历史。）

// 岗位表：role 对应接口里的汇报员 / 策略交易员 / AI 交易员；后面几项是小人的长相
const REPORT_SEATS = [
  ['breadth', '宽度', '#c8322b', 'short', '#2a2420', 0],
  ['ladder', '梯队', '#c98a1b', 'long', '#4a3424', 1],
  ['high', '高标', '#7a4fa0', 'side', '#1d1a16', 2, true],
  ['movers', '异动', '#3b78b5', 'bun', '#6b4a2e', 0],
  ['scan', '扫板', '#d0662a', 'short', '#5a3a22', 1],
  ['book', '持仓', '#5b7f3a', 'pony', '#2a2420', 2],
  ['risk', '风控', '#a33a52', 'crew', '#8a8580', 0, true],
].map(([role, name, color, hair, hairColor, skin, glasses]) => ({ role, name, color, hair, hairColor, skin, glasses, kind: 'report' }))
const STRAT_SEATS = [
  ['strat:relay', '接力', '#e0574a', 'side', '#2a2420', 1],
  ['strat:wts', '弱转强', '#4a8fd1', 'long', '#3a2a1e', 2],
  ['strat:bz', '补涨', '#6aa84f', 'short', '#4a3424', 0],
].map(([role, name, color, hair, hairColor, skin]) => ({ role, name, color, hair, hairColor, skin, body: 'vest', kind: 'strat' }))
const TRADER_SEATS = [
  ['trader:aiflex', '交易员1', '#d9a21b', 'short', '#1d1a16', 1],
  ['trader:aiflex2', '交易员2', '#2f8a6c', 'long', '#5a3a22', 0],
].map(([role, name, color, hair, hairColor, skin]) => ({ role, name, color, hair, hairColor, skin, headset: true, kind: 'trader' }))
// 顺序不能随便动：排班（到岗 / 下班 / 午休干什么）和邻座聊天都按这个下标算
// 回测员（dj 2026-11-12）：后加的，放在最后——前面的下标排班要用，插到中间会把每个人的午休和到岗时间都打乱
const LATE_SEATS = [{ role: 'backtest', name: '回测', color: '#4a7a8c', hair: 'side', hairColor: '#2a2420', skin: 2, glasses: true, kind: 'report' }]
export const SEATS = [...REPORT_SEATS, ...STRAT_SEATS, ...TRADER_SEATS, ...LATE_SEATS]

export const STRAT_INFO = [['relay', 'RELAY', '高标接力官'], ['wts', 'WTS', '弱转强官'], ['bz', 'BZ', '补涨官']]


// ---------- 排班：谁在哪，只看北京时间（接口的 now 就是北京时间） ----------
const PEOPLE = ['manager', ...SEATS.map((s) => s.role)]
const MEETING = ['manager', 'trader:aiflex', 'trader:aiflex2']       // 周复盘会（dashboard/weekly_review.py）到场的人
const LUNCH = ['sofa', 'nap', 'out', 'chat', 'out', 'nap', 'out', 'nap', 'out', 'nap', 'chat', 'nap', 'out', 'nap']
const STAGE_OF = [[525, 'empty'], [555, 'arrive'], [690, 'work'], [780, 'lunch'], [900, 'work'], [930, 'review'], [1050, 'leave']]

// where(role)：seat 坐着干活｜nap 趴桌睡｜chat 过道里端着杯子聊天｜sofa 沙发上躺着｜board 站在白板前｜gone 不在
export function sceneOf(iso) {
  if (!iso) return { stage: 'empty', m: 0, where: () => 'gone', present: [], awake: [] }
  const d = new Date(iso)
  const m = d.getHours() * 60 + d.getMinutes()
  const weekend = d.getDay() === 0 || d.getDay() === 6
  const stage = weekend ? 'closed' : (STAGE_OF.find(([end]) => m < end) || [0, 'empty'])[1]
  const meeting = !weekend && d.getDay() === 5 && m >= 930 && m < 960
  const where = (role) => {
    const i = PEOPLE.indexOf(role)
    if (meeting && MEETING.includes(role)) return 'meeting'
    if (stage === 'work') return 'seat'
    if (stage === 'review') return role === 'manager' ? 'board' : 'seat'
    if (stage === 'arrive') return m >= 525 + ((i * 7) % 13) * 2 ? 'seat' : 'gone'
    if (stage === 'leave') return m >= 930 + ((i * 5) % 13) * 9 ? 'gone' : 'seat'
    if (stage === 'lunch') {
      if (m < 695 || m >= 777) return 'seat'                 // 11:35 才起身，12:57 都坐回来
      const act = LUNCH[i]
      if (act === 'sofa') return m < 770 ? 'sofa' : 'seat'
      if (act === 'out') return m < 765 ? 'gone' : 'seat'    // 出去吃饭
      if (act === 'chat') return m < 725 ? 'chat' : 'nap'
      return 'nap'
    }
    return 'gone'
  }
  // present 在办公室里的人；awake 醒着、能开口汇报的人（睡着的不会自己冒气泡）
  return { stage, m, where, meeting, present: PEOPLE.filter((r) => where(r) !== 'gone'),
    awake: PEOPLE.filter((r) => ['seat', 'board', 'chat', 'meeting'].includes(where(r))) }
}

// 天色：按月份的日出日落（上海附近，够用），返回 sky 和太阳走到哪了（0 日出 → 1 日落）
const SUNRISE = [6.9, 6.6, 6.1, 5.5, 5.0, 4.9, 5.0, 5.3, 5.6, 5.9, 6.3, 6.7]
const SUNSET = [17.2, 17.7, 18.0, 18.3, 18.7, 19.0, 19.0, 18.6, 18.0, 17.4, 16.9, 16.9]
export function skyOf(iso) {
  if (!iso) return { sky: 'day', sun: 0.5 }
  const d = new Date(iso)
  const h = d.getHours() + d.getMinutes() / 60
  const rise = SUNRISE[d.getMonth()]
  const set = SUNSET[d.getMonth()]
  const sun = Math.min(1, Math.max(0, (h - rise) / (set - rise)))
  if (h < rise - 0.5 || h >= set + 0.5) return { sky: 'night', sun }
  if (h < rise + 0.5) return { sky: 'dawn', sun }
  if (h >= set - 0.75) return { sky: 'dusk', sun }
  return { sky: 'day', sun }
}

// ---------- 说人话：气泡里的短句 + 点开后的完整汇报。只改写接口给的真数，不编内容 ----------
const list = (v) => (Array.isArray(v) ? v : v ? [v] : [])
const yi = (txt) => {
  const n = +String(txt).replace(/,/g, '')
  return n >= 10000 ? `${(n / 10000).toFixed(2)} 万亿` : `${n} 亿`
}

function reportSpeech(role, r) {
  if (!r || r.level === 'idle') return { short: '数还没出来，开会的时候我报。', full: [] }
  const raw = r.line || ''
  const head = r.level === 'bad' ? '经理，有情况——' : '经理，'
  let m
  if (role === 'breadth' && (m = raw.match(/涨 (\d+) \/ 跌 (\d+)(?: · 成交 ([\d,]+) 亿)?/))) {
    const share = +m[1] / (+m[1] + +m[2] || 1)
    const mood = share >= 0.6 ? '大多数票在涨' : share <= 0.35 ? '跌的明显比涨的多' : '涨跌差不多一半一半'
    return { short: `${head}现在涨 ${m[1]} 家、跌 ${m[2]} 家，${mood}${m[3] ? `，成交 ${yi(m[3])}` : ''}。`,
      full: [`上涨家数占 ${(share * 100).toFixed(1)}%。`] }
  }
  if (role === 'ladder' && (m = raw.match(/^(.+?) (\d+) 家 · 最高 (\S+) 板 · (\S+)(?: ｜次 (.+?) (\d+) 家)?/))) {
    return { short: `${head}最厚的题材是${m[1]}，${m[2]} 家涨停，最高 ${m[3]} 板，现在算「${m[4]}」阶段。`,
      full: m[5] ? [`排第二的是${m[5]}，${m[6]} 家。`] : [] }
  }
  if (role === 'high' && (m = raw.match(/昨日≥3板 (\d+) 只 · 走弱 (\d+) · (\S+)(?: ｜(.+))?/))) {
    return { short: `${head}昨天 3 板以上的有 ${m[1]} 只，今天走弱 ${m[2]} 只，我看是「${m[3]}」。`,
      full: m[4] ? [`最高的那只：${m[4]}。`] : [] }
  }
  if (role === 'movers' && (m = raw.match(/异动 (\d+) 只(?: · (.+))?/))) {
    return { short: `${head}盘中异动的有 ${m[1]} 只${m[2] ? `，排前面的是 ${m[2]}` : ''}。`, full: [] }
  }
  if (role === 'scan' && (m = raw.match(/候选 (\d+) 只 · 封板 (\d+) · 扫板池 (\d+)/))) {
    return { short: `${head}扫板这边候选 ${m[1]} 只，已经封板 ${m[2]} 只，进扫板池的 ${m[3]} 只。`, full: [] }
  }
  if (role === 'book') {
    const parts = raw.split(' · ').map((p) => p.match(/^(.+?) ([\d,]+) \/ (\d+) 只$/)).filter(Boolean)
    if (parts.length) return { short: `${head}账我对过了：${parts.map((p) => `${p[1]}净值 ${p[2]}、拿着 ${p[3]} 只`).join('；')}。`, full: [] }
  }
  if (role === 'backtest') {
    const items = r.items || []
    const top = items.find((i) => i.rise20 >= 30)
    const fast = items.filter((i) => i.biasMa5 >= 8).sort((a, b) => b.biasMa5 - a.biasMa5)[0]   // 短期追高（乖离，11-14 加）
    const short = top ? `${head}${raw}。${top.name} 20 天涨了 ${Math.round(top.rise20)}%，这个位置历史上一半的票 5 天内要跌超 10%，别追。`
      : fast ? `${head}${raw}。${fast.name} 比 5 日线高了 ${Math.round(fast.biasMa5)}%，冲得太急，历史上这样的票 5 天内跌超 10% 的有三成往上。`
        : `${head}${raw}，没有在高位的票。`
    const sign = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v}`)
    return { short, full: items.map((i) => `${i.name || i.code}　5日 ${sign(i.rise5)}%　20日 ${sign(i.rise20)}%　乖离 ${sign(i.biasMa5)}%　${i.note || i.verdict}`) }
  }
  if (role === 'risk') {
    return raw === '护栏正常' ? { short: `${head}护栏都正常，没有碰线的。`, full: [] } : { short: `${head}风控这边要注意：${raw}。`, full: [] }
  }
  return { short: `${head}${raw}。`, full: [] }
}

function stratSpeech(t) {
  if (!t) return { short: '我 09:26 才开始看票。规则先筛一遍，我只负责把不该做的否掉，不会自己加票。', full: [] }
  if (t.error) return { short: `经理，我这边出错了：${t.error}`, full: [] }
  const head = `经理，规则给我放行了 ${t.passed} 只（候选 ${t.candidates} 只），我否掉 ${t.vetoed.length} 只`
  const short = t.picks.length ? `${head}，推荐 ${t.picks.map((p) => p.name).join('、')}。` : `${head}，这个时点没有我敢推的。`
  return { short, full: [
    ...t.picks.map((p) => `推荐 ${p.name}（${p.code}）：${p.why}。看错的信号：${p.falsify || '—'}。把握：${p.confidence || '—'}。`),
    ...t.vetoed.map((v) => `否掉 ${v.name}：${v.why}。`),
    ...(t.rejected || []).map((r) => `规则没放行 ${r.name}：${r.why}。`),
  ] }
}

function traderSpeech(t) {
  if (!t) return { short: '今天还没轮到我。', full: [] }
  const d = t.directive
  const order = d ? (d.buy ? `经理让我开仓，单笔不超过 ${Math.round((d.maxPct || 0) * 100)}%。` : '经理让我今天别开新仓。') : ''
  const r = t.rounds?.[t.rounds.length - 1]
  const a = t.account || {}
  const book = a.equity != null ? `账户净值 ${Math.round(a.equity).toLocaleString('en-US')}，今天 ${a.today > 0 ? '+' : ''}${Number(a.today || 0).toFixed(2)}%，拿着 ${(a.positions || []).length} 只。` : ''
  if (!r) return { short: `今天还没轮到我。${order}`, full: [book].filter(Boolean) }
  if (r.skipped) return { short: `${r.slot} 这轮我没动：${r.skipped}。${order}`, full: [book].filter(Boolean) }
  return { short: `${r.slot} 这轮我看是「${r.stage || '—'}」。${order}`,
    full: [r.plan, d?.why ? `经理的原话：${d.why}` : '', book,
      ...(t.fills || []).slice(-4).map((f) => `${f.t} ${f.side === 'buy' ? '买' : '卖'} ${f.name} ${f.price} × ${f.shares}`)].filter(Boolean) }
}

function managerSpeech(m) {
  if (!m) return { short: '09:26 开会，开完我告诉大家今天怎么做。', full: [] }
  const watch = list(m.watch)
  return { short: `我判断今天是「${m.regime}」。${watch[0] ? `接下来先盯：${watch[0]}。` : ''}`,
    full: [m.read, ...watch.slice(1).map((w) => `还要盯：${w}。`), ...list(m.falsify).map((f) => `我看错的信号：${f}。`)].filter(Boolean) }
}

export function speechOf(role, data) {
  if (role === 'manager') return managerSpeech(data?.manager)
  if (role.startsWith('strat:')) return stratSpeech((data?.strategyDesk?.traders || []).find((t) => `strat:${t.key}` === role))
  if (role.startsWith('trader:')) return traderSpeech((data?.traders || []).find((t) => `trader:${t.sid}` === role))
  return reportSpeech(role, (data?.roles || []).find((r) => r.role === role))
}

// 3 分钟内有成交：那位交易员自己站出来说（真实事件，优先于轮流发言）
export function liveEvent(data) {
  if (!data?.now) return null
  const now = new Date(data.now)
  for (const e of data.log || []) {
    if (e.tag !== '成交') continue
    const age = (now - new Date(`${data.now.slice(0, 10)}T${e.t}`)) / 1000
    if (age < 0 || age > 180) continue
    const t = (data.traders || []).find((x) => x.label === e.who)
    if (t) return { role: `trader:${t.sid}`, text: `刚成交一笔：${e.text.split('｜')[0]}。` }
  }
  return null
}

export function stratLine(t) {
  if (!t) return '等 09:26：规则先放行候选，我只能否决'
  if (t.error) return `出错：${t.error}`
  const head = `候选 ${t.candidates} · 放行 ${t.passed} · 荐 ${t.picks.length} · 否 ${t.vetoed.length}`
  return t.picks.length ? `${head}｜荐 ${t.picks.map((p) => p.name).join('、')}` : head
}

export function traderLine(t) {
  const r = t.rounds?.[t.rounds.length - 1]
  if (!r) return '今天还没跑'
  if (r.skipped) return `${r.slot} ${r.skipped}`
  return `${r.slot} ${r.stage || '—'}｜${r.plan || ''}`
}
