import { memo } from 'react'

// 像素办公室（dj 2026-11-10：「像现实中真的办公区域」）。viewBox 400×176，一个 SVG 单位就是一个像素格。
// 后墙从左到右：茶水间 + 挂钟、窗、行情大屏、窗；地板上第一排是 7 个汇报席，第二排是 3 个策略席 + 2 个交易席 + 机柜；
// 右边隔出经理室。dj 2026-11-10 第二版：小人背对我们坐，显示器在头的正前方、屏幕朝我们（亮着 = 在干活）；
// 渲染顺序是 桌子 → 桌上的东西 → 人 → 椅背，不能随便调。谁在座位上由 sceneOf() 按北京时间排班。

const W = 480
const H = 270
const ROW_A = 86
const ROW_B = 196
const SKINS = ['#f1c9a0', '#e6b88c', '#f7d9b8']

const shade = (hex, f) => `#${[1, 3, 5].map((i) => Math.round(parseInt(hex.slice(i, i + 2), 16) * f).toString(16).padStart(2, '0')).join('')}`

const HEADS = {
  short: ['...hhhhhh...', '..hhhhhhhh..', '..hhhhhhhh..', '..hssssssh..', '..ssessess..', '..sssmmsss..', '...ssssss...'],
  side: ['...hhhhhh...', '..hhhhhhhh..', '..hhhhhsss..', '..hhssssss..', '..ssessess..', '..sssmmsss..', '...ssssss...'],
  long: ['...hhhhhh...', '..hhhhhhhh..', '.hhhhhhhhhh.', '.hhsssssshh.', '.hssessessh.', '.hsssmmsssh.', '.hhsssssshh.'],
  bun: ['.....hh.....', '...hhhhhh...', '..hhhhhhhh..', '..hssssssh..', '..ssessess..', '..sssmmsss..', '...ssssss...'],
  pony: ['...hhhhhh...', '..hhhhhhhhh.', '..hhhhhhhhhh', '..hsssssshhh', '..ssessess.h', '..sssmmsss.h', '...ssssss..h'],
  crew: ['............', '...hhhhhh...', '..hssssssh..', '..hssssssh..', '..ssessess..', '..sssmmsss..', '...ssssss...'],
}
const GLASSES_ROW = '..keksskek..'
const LONG_TAIL = ['.h........h.', '.h........h.']
const HEADSET = ['...kkkkkk...', '..k......k..', '.k........k.', '.kk......kk.', '.kk......kk.', '.k..........', '..kkk.......']
const BODIES = {
  shirt: ['...ccwwcc...', '..cccccccc..', '.ccdccccdcc.', '.ccdccccdcc.', '.cccccccccc.'],
  vest: ['...wcwwcw...', '..wwccccww..', '.wwdccccdww.', '.wwdccccdww.', '.wwwccccwww.'],
  suit: ['...nwrrwn...', '..nnwrrwnn..', '.nnNnrrnNnn.', '.nnNnrrnNnn.', '.nnnnnnnnnn.'],
}
// 背面：只看得到后脑勺、耳朵和脖子
const BACK_HEADS = {
  short: ['...hhhhhh...', '..hhhhhhhh..', '..hhhhhhhh..', '..hhhhhhhh..', '..shhhhhhs..', '...hhhhhh...', '....ssss....'],
  side: ['....hhhhh...', '..hhhhhhhh..', '..hhhhhhhh..', '..hhhhhhhh..', '..shhhhhhs..', '...hhhhhh...', '....ssss....'],
  long: ['...hhhhhh...', '..hhhhhhhh..', '.hhhhhhhhhh.', '.hhhhhhhhhh.', '.hhhhhhhhhh.', '.hhhhhhhhhh.', '.hhhhhhhhhh.'],
  bun: ['.....hh.....', '...hhhhhh...', '..hhhhhhhh..', '..hhhhhhhh..', '..shhhhhhs..', '...hhhhhh...', '....ssss....'],
  pony: ['...hhhhhh...', '..hhhhhhhh..', '..hhhhhhhh..', '..hhhhhhhh..', '..shhhhhhs..', '...hhhhhh...', '....shhs....'],
  crew: ['............', '...hhhhhh...', '..hhhhhhhh..', '..shhhhhhs..', '..sshhhhss..', '...ssssss...', '....ssss....'],
}
const BACK_TAILS = { long: ['.hhhhhhhhhh.', '..hhhhhhhh..'], pony: ['.....hh.....', '.....hh.....'] }
const BACK_HEADSET = ['..kkkkkkkk..', '.k........k.', '.k........k.', '.kk......kk.', '.kk......kk.']
const BACK_BODIES = {
  shirt: ['..cccccccc..', '.cccccccccc.', '.cccccccccc.', '.cccccccccc.'],
  vest: ['..wccccccw..', '.wwccccccww.', '.wwccccccww.', '.wwccccccww.'],
  suit: ['..nnnwwnnn..', '.nnnnnnnnnn.', '.nnnnnnnnnn.', '.nnnnnnnnnn.'],
}
const STAND_LEGS = ['...pp..pp...', '...pp..pp...', '...oo..oo...']
const PLANT = ['....gg....', '..g.gg.g..', '.gggGggg..', '..gGgggGg.', '.gggggggg.', 'gggGgggggg', '.ggggGggg.', '..gggggg..',
  '....tt....', '....tt....', '..pppppp..', '..pqqqqp..', '...pppp...', '...pppp...']
const PLANT_PAL = { g: '#4f8a4a', G: '#74b262', t: '#6d4a2e', p: '#efe8da', q: '#d9cfbc' }
const RUNNER = ['..hhhh..', '.hhhhhh.', '.hhhsss.', '.hhsses.', '.hhssss.', '..ssss..', '..cccc..', '.cccccc.', '.ccccccs', '.cccccc.', '..cccc..', '..pppp..', '..pppp..']
const LEGS_A = ['..pp.pp.', '.pp...pp', '.oo...oo']
const LEGS_B = ['...pp...', '...pp...', '...oo...']
const RUNNER_PAL = { h: '#3a2a1e', s: SKINS[0], e: '#1d1a16', c: '#5f86ad', p: '#3b3f46', o: '#22252b' }

// 岗位表：role 对应接口里的汇报员 / 策略交易员 / AI 交易员。w 是这个工位占的宽度。
const REPORT_SEATS = [
  ['breadth', '宽度', '#c8322b', 'short', '#2a2420', 0],
  ['ladder', '梯队', '#c98a1b', 'long', '#4a3424', 1],
  ['high', '高标', '#7a4fa0', 'side', '#1d1a16', 2, true],
  ['movers', '异动', '#3b78b5', 'bun', '#6b4a2e', 0],
  ['scan', '扫板', '#d0662a', 'short', '#5a3a22', 1],
  ['book', '持仓', '#5b7f3a', 'pony', '#2a2420', 2],
  ['risk', '风控', '#a33a52', 'crew', '#8a8580', 0, true],
].map(([role, name, color, hair, hairColor, skin, glasses], i) => ({ role, name, color, hair, hairColor, skin, glasses, kind: 'report', x: 16 + i * 52, y: ROW_A, w: 52 }))
const STRAT_SEATS = [
  ['strat:relay', '接力', '#e0574a', 'side', '#2a2420', 1],
  ['strat:wts', '弱转强', '#4a8fd1', 'long', '#3a2a1e', 2],
  ['strat:bz', '补涨', '#6aa84f', 'short', '#4a3424', 0],
].map(([role, name, color, hair, hairColor, skin], i) => ({ role, name, color, hair, hairColor, skin, body: 'vest', kind: 'strat', x: 10 + i * 55, y: ROW_B, w: 55 }))
const TRADER_SEATS = [
  ['trader:aiflex', '交易员1', '#d9a21b', 'short', '#1d1a16', 1],
  ['trader:aiflex2', '交易员2', '#2f8a6c', 'long', '#5a3a22', 0],
].map(([role, name, color, hair, hairColor, skin], i) => ({ role, name, color, hair, hairColor, skin, headset: true, kind: 'trader', x: 192 + i * 62, y: ROW_B, w: 62 }))
export const SEATS = [...REPORT_SEATS, ...STRAT_SEATS, ...TRADER_SEATS]
const MANAGER = { color: '#2c3550', hair: 'side', hairColor: '#3a3632', skin: 1, glasses: true, body: 'suit' }
const MGR = { x: 430, y: 96 }

export const STRAT_INFO = [['relay', 'RELAY', '高标接力官'], ['wts', 'WTS', '弱转强官'], ['bz', 'BZ', '补涨官']]

// ---------- 排班：谁在哪，只看北京时间（接口的 now 就是北京时间） ----------
const PEOPLE = ['manager', ...SEATS.map((s) => s.role)]
const MEETING = ['manager', 'trader:aiflex', 'trader:aiflex2']       // 周复盘会（dashboard/weekly_review.py）到场的人
const LUNCH = ['sofa', 'nap', 'out', 'chat', 'out', 'nap', 'out', 'nap', 'out', 'nap', 'chat', 'nap', 'out']
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
function skyOf(iso) {
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

// 一行字符 → 横向合并后的 rect，减少节点数
function Pixels({ rows, pal, x = 0, y = 0 }) {
  const out = []
  rows.forEach((row, j) => {
    let i = 0
    while (i < row.length) {
      const c = row[i]
      let k = i + 1
      while (k < row.length && row[k] === c) k += 1
      if (c !== '.') out.push(<rect key={`${j}-${i}`} x={x + i} y={y + j} width={k - i} height="1" fill={pal[c]} />)
      i = k
    }
  })
  return out
}

const palOf = (look) => {
  const skin = SKINS[look.skin]
  return { h: look.hairColor, s: skin, m: shade(skin, 0.78), e: '#1d1a16', c: look.color, d: shade(look.color, 0.76), w: '#fbf8f2',
    k: look.glasses ? '#8a6238' : '#2b2f36', n: '#2c3550', N: '#1f2740', r: '#c8322b', p: '#3b3f46', o: '#22252b' }
}

// 背对我们坐着的人。x,y 是头顶左上角；身体下半截之后会被椅背盖住
function Sitter({ look, x, y, state, active, typing, stretch, i }) {
  const pal = palOf(look)
  const cls = active ? 'is-active' : state === 'nap' ? 'nap' : 'look'
  return (
    <g transform={`translate(${x},${y})`}>
      <g className={`d-char ${cls}`} style={{ animationDelay: `${-((i * 1.7) % 9)}s` }}>
        <g className={typing ? 'd-typing' : undefined}>
          <rect className="d-hand" style={{ animationDelay: `${-(i * 0.07)}s` }} x="-1" y="8" width="2" height="2" fill={pal.c} />
          <rect className="d-hand b" style={{ animationDelay: `${-(i * 0.07 + 0.16)}s` }} x="11" y="8" width="2" height="2" fill={pal.c} />
        </g>
        <Pixels rows={BACK_BODIES[look.body || 'shirt']} pal={pal} y={7} />
        <Pixels rows={BACK_HEADS[look.hair]} pal={pal} />
        {BACK_TAILS[look.hair] && <Pixels rows={BACK_TAILS[look.hair]} pal={pal} y={7} />}
        {look.headset && <Pixels rows={BACK_HEADSET} pal={pal} />}
        {stretch && (
          <g className="d-arms" style={{ animationDelay: `${-(i % 5)}s` }}>
            <rect x="0" y="-2" width="2" height="9" fill={pal.c} /><rect x="10" y="-2" width="2" height="9" fill={pal.c} />
            <rect x="0" y="-4" width="2" height="2" fill={pal.s} /><rect x="10" y="-4" width="2" height="2" fill={pal.s} />
          </g>
        )}
      </g>
    </g>
  )
}

// 站着的人：front = 面对我们（午休聊天），否则背对（经理写白板）
function Stander({ look, x, y, front, cup, writing }) {
  const pal = palOf(look)
  const head = front ? (look.glasses ? HEADS[look.hair].map((r, j) => (j === 4 ? GLASSES_ROW : r)) : HEADS[look.hair]) : BACK_HEADS[look.hair]
  const body = front ? BODIES[look.body || 'shirt'] : [...BACK_BODIES[look.body || 'shirt'], BACK_BODIES[look.body || 'shirt'][3]]
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="1" y="15" width="10" height="1" fill="#2a241d" opacity="0.18" />
      <Pixels rows={head} pal={pal} />
      <Pixels rows={body} pal={pal} y={7} />
      <Pixels rows={STAND_LEGS} pal={pal} y={12} />
      {cup && <g><rect x="10" y="9" width="2" height="3" fill="#ffffff" /><rect className="d-steam" x="10" y="7" width="1" height="2" fill="#ffffff" /></g>}
      {writing && <g className="d-typing"><rect className="d-hand" x="11" y="3" width="2" height="5" fill={pal.c === pal.n ? pal.n : pal.c} /><rect className="d-hand" x="11" y="1" width="2" height="2" fill={pal.s} /><rect className="d-hand" x="12" y="-1" width="1" height="2" fill="#2a241d" /></g>}
    </g>
  )
}

// 屏幕朝我们。mode：on 亮着干活｜saver 屏保｜off 关机
const TONES = { ok: ['#12261d', '#8fe0b4'], warn: ['#2a2212', '#f0c36a'], bad: ['#2a1412', '#ff8076'], calm: ['#131c26', '#8fb7e0'],
  up: ['#2a1412', '#ff8076'], down: ['#12261d', '#8fe0b4'] }
const BAR_H = [4, 7, 5, 8, 6]

function Monitor({ x, y, w = 18, mode, tone, text, i = 0 }) {
  const t = mode === 'on' ? TONES[tone] || TONES.calm : null
  const bars = Math.floor((w - 4) / 3)
  return (
    <g transform={`translate(${x},${y})`}>
      <rect width={w} height="12" fill="#2b2f36" />
      <rect x="1" y="1" width={w - 2} height="10" fill={t ? t[0] : mode === 'saver' ? '#161a20' : '#23272d'} />
      {t && !text && Array.from({ length: bars }, (_, k) => (
        <rect key={k} className="d-bar" style={{ animationDelay: `${-(i * 0.37 + k * 0.53)}s` }} x={2 + k * 3} y={11 - BAR_H[k % 5]} width="2" height={BAR_H[k % 5]} fill={t[1]} />
      ))}
      {t && text && <text x={w / 2} y="8.4" textAnchor="middle" className={`d-svg-mon ${text.length > 2 ? 's' : ''}`} fill={t[1]}>{text}</text>}
      {mode === 'saver' && <rect className="d-saver" style={{ animationDelay: `${-i * 0.7}s` }} x="2" y="5" width="2" height="2" fill="#4d6a8a" />}
      {mode === 'off' && <rect x="2" y="2" width="3" height="1" fill="#30353c" />}
      <rect x={w / 2 - 2} y="12" width="4" height="2" fill="#22252b" /><rect x={w / 2 - 4} y="14" width="8" height="1" fill="#22252b" />
      {t && <rect x="1" y="15" width={w - 2} height="1" fill={t[1]} opacity="0.28" />}
    </g>
  )
}

const LIGHT_DESK = { top: '#f6f1e7', hi: '#fffdf7', edge: '#cfc5b2', foot: '#bfb39c' }
const WALNUT_DESK = { top: '#9a6842', hi: '#b07e55', edge: '#6d4128', foot: '#55301c' }

// 从背后看过去的桌子：只看到桌面、桌沿、桌下的阴影和桌腿。y 是这一排的基准线
function DeskTop({ x, y, w, c = LIGHT_DESK, seams = [] }) {
  return (
    <g>
      <rect x={x} y={y + 12} width={w} height="4" fill="#2a241d" opacity="0.2" />
      {[x, ...seams, x + w - 2].map((lx) => <rect key={lx} x={lx} y={y + 12} width="2" height="11" fill={c.foot} />)}
      <rect x={x} y={y + 4} width={w} height="7" fill={c.top} />
      <rect x={x} y={y + 4} width={w} height="1" fill={c.hi} />
      <rect x={x} y={y + 11} width={w} height="1" fill={c.edge} />
    </g>
  )
}

// 椅子画在人的后面一层（离我们最近）。没人的时候推回桌下
function Chair({ x, y, tall, tucked }) {
  const top = y + (tucked ? 10 : 13) - (tall ? 4 : 0)
  const h = tall ? 12 : 8
  const col = tall ? ['#3a2f2a', '#51423a'] : ['#4a4f57', '#5d636c']
  return (
    <g>
      <rect x={x + 1} y={y + 24} width="10" height="1" fill="#2a241d" opacity="0.2" />
      <rect x={x - 1} y={top} width="14" height={h} fill={col[0]} /><rect x={x} y={top - 1} width="12" height="1" fill={col[0]} />
      <rect x={x} y={top} width="12" height="1" fill={col[1]} />
      <rect x={x + 5} y={top + h} width="2" height={y + 23 - top - h} fill="#2b2f36" />
      <rect x={x + 1} y={y + 23} width="10" height="1" fill="#2b2f36" />
    </g>
  )
}

const personX = (s) => s.x + Math.floor((s.w - 12) / 2)

// 一个工位桌上的东西：显示器、杯子 / 递上去的纸。info.mode 决定屏幕亮不亮
function DeskItems({ s, info, i }) {
  const px = personX(s)
  return (
    <g>
      {s.kind === 'trader' ? (
        <>
          <Monitor x={px - 11} y={s.y - 9} w={16} mode={info.mode} tone={info.pnlTone} text={info.mode === 'on' ? info.pnl : null} i={i} />
          <Monitor x={px + 7} y={s.y - 9} w={16} mode={info.mode} tone={info.gate === '开' ? 'ok' : info.gate === '关' ? 'bad' : 'calm'} text={info.mode === 'on' ? info.gate || '—' : null} i={i + 3} />
        </>
      ) : <Monitor x={px - 3} y={s.y - 9} mode={info.mode} tone={info.tone} i={i} />}
      {info.alert && info.mode === 'on' && <rect className="d-led" x={px + 16} y={s.y - 11} width="2" height="2" fill="#e5463c" />}
      {s.kind === 'report' && <g><rect x={s.x + s.w - 8} y={s.y + 6} width="2" height="3" fill={shade(s.color, 0.9)} /><rect x={s.x + s.w - 6} y={s.y + 7} width="1" height="1" fill={shade(s.color, 0.9)} /></g>}
      {s.kind === 'strat' && Array.from({ length: Math.min(info.picks || 0, 3) }, (_, k) => (
        <g key={k}><rect x={s.x + 3 + (k % 2)} y={s.y + 8 - k * 2} width="5" height="1" fill="#ffffff" /><rect x={s.x + 3 + (k % 2)} y={s.y + 9 - k * 2} width="5" height="1" fill="#c9c0ae" /></g>
      ))}
    </g>
  )
}

// ---------- 静态背景（只跟天色有关，memo 掉） ----------
const SKY = {
  dawn: { bands: ['#7f8fc4', '#f0b6a0', '#fbe3c0'], far: '#a895ad', near: '#6b5f80', lit: '#ffd76a' },
  day: { bands: ['#8cc6ea', '#a9d6f0', '#c9e6f5'], far: '#a9c2d2', near: '#7f9db5', lit: null },
  dusk: { bands: ['#6f7fb8', '#e89a7a', '#f6cf9a'], far: '#9a7f9c', near: '#5d4f72', lit: '#ffd76a' },
  night: { bands: ['#141b33', '#1c2644', '#27335a'], far: '#1e2540', near: '#10152a', lit: '#ffd76a' },
}
const TOWERS = [[0, 7, 12], [8, 5, 18], [14, 8, 9], [23, 6, 22], [30, 9, 14], [40, 5, 19], [46, 8, 11]]

function Window({ x, y, w, h, id, sky, seed = 0, sun }) {
  const s = SKY[sky]
  const low = sun != null && (sun < 0.12 || sun > 0.88)
  const sx = sun == null ? 0 : Math.round(x + 2 + sun * (w - 9))
  const sy = sun == null ? 0 : Math.round(y + 4 + (1 - Math.sin(sun * Math.PI)) * h * 0.5)
  const band = Math.ceil(h / 3)
  const towers = TOWERS.filter(([dx]) => dx < w)
  return (
    <g>
      <clipPath id={id}><rect x={x} y={y} width={w} height={h} /></clipPath>
      <rect x={x - 3} y={y - 3} width={w + 6} height={h + 6} fill="#b9a98c" />
      <rect x={x - 2} y={y - 2} width={w + 4} height={h + 4} fill="#fbf8f2" />
      <g clipPath={`url(#${id})`}>
        {s.bands.map((c, k) => <rect key={c} x={x} y={y + k * band} width={w} height={band} fill={c} />)}
        {sun != null && sky !== 'night' && <rect x={sx} y={sy} width={low ? 6 : 5} height={low ? 6 : 5} fill={low ? '#ffb347' : '#fff6c0'} />}
        {sun != null && sky === 'night' && <g><rect x={x + w - 11} y={y + 7} width="4" height="4" fill="#f4f0d8" /><rect x={x + w - 8} y={y + 7} width="1" height="1" fill={s.bands[0]} /></g>}
        {sky === 'night'
          ? [[5, 8], [14, 5], [22, 11], [31, 7], [9, 14]].filter(([dx]) => dx < w - 2).map(([dx, dy], k) => (
            <rect key={dx} className={k % 2 ? 'd-led' : undefined} style={{ animationDelay: `${-k * 0.4}s` }} x={x + dx} y={y + dy} width="1" height="1" fill="#ffffff" />
          ))
          : (
            <>
              <g className="d-cloud" style={{ animationDelay: `${-seed * 9}s` }}>
                <rect x={x + 2} y={y + 10} width="11" height="2" fill="#ffffff" opacity="0.92" /><rect x={x + 4} y={y + 8} width="6" height="2" fill="#ffffff" opacity="0.92" />
              </g>
              <g className="d-cloud slow" style={{ animationDelay: `${-seed * 17 - 30}s` }}>
                <rect x={x + 12} y={y + 17} width="8" height="2" fill="#ffffff" opacity="0.8" /><rect x={x + 14} y={y + 16} width="4" height="1" fill="#ffffff" opacity="0.8" />
              </g>
            </>
          )}
        {towers.map(([dx, tw, th]) => <rect key={`f${dx}`} x={x + dx + 3} y={y + h - th - 5} width={tw} height={th + 5} fill={s.far} />)}
        {towers.map(([dx, tw, th], k) => (
          <g key={`n${dx}`}>
            <rect x={x + dx} y={y + h - Math.round(th * 0.65)} width={tw - 1} height={th} fill={s.near} />
            {s.lit && Array.from({ length: Math.floor(th * 0.65 / 3) }, (_, r) => Array.from({ length: Math.floor((tw - 2) / 2) }, (_, c) => (
              (k * 7 + r * 3 + c * 5 + seed) % 3 === 0
                ? <rect key={`${r}-${c}`} x={x + dx + 1 + c * 2} y={y + h - Math.round(th * 0.65) + 2 + r * 3} width="1" height="1" fill={s.lit} /> : null
            )))}
          </g>
        ))}
      </g>
      <rect x={x + Math.round(w / 2) - 1} y={y} width="2" height={h} fill="#fbf8f2" />
      <rect x={x} y={y + Math.round(h * 0.42)} width={w} height="1" fill="#fbf8f2" />
      <rect x={x} y={y} width={w} height="3" fill="#e9e1d0" /><rect x={x} y={y + 3} width={w} height="1" fill="#d2c5ad" />
      <rect x={x - 4} y={y + h + 2} width={w + 8} height="2" fill="#d2c5ad" />
    </g>
  )
}

// 第三版布局（dj 2026-11-11：「太挤了，座位分开一点或者加几个办公室」）：画布 480×270。
// 上半层是汇报大厅（7 个汇报席一排）和右边的经理室；下半层隔成三间：策略室、交易室、会议室（周五复盘、午休的沙发也在这）。
const Backdrop = memo(function Backdrop({ sky, sunStep, lights }) {
  const lamp = lights ? '#fff8de' : '#8f8672'
  return (
    <g>
      <defs>
        <pattern id="d-wood" width="32" height="12" patternUnits="userSpaceOnUse">
          <rect width="32" height="12" fill="#d8bc92" />
          <rect y="5" width="32" height="1" fill="#c9aa7e" /><rect y="11" width="32" height="1" fill="#c9aa7e" />
          <rect x="10" width="1" height="5" fill="#c9aa7e" /><rect x="26" y="6" width="1" height="5" fill="#c9aa7e" />
          <rect x="2" y="2" width="5" height="1" fill="#dfc7a0" /><rect x="16" y="8" width="6" height="1" fill="#dfc7a0" />
        </pattern>
        <pattern id="d-carpet" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="#7f9189" /><rect width="4" height="4" fill="#869890" /><rect x="4" y="4" width="4" height="4" fill="#869890" />
        </pattern>
        <pattern id="d-carpet-blue" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="#7d8ea3" /><rect width="4" height="4" fill="#8696ab" /><rect x="4" y="4" width="4" height="4" fill="#8696ab" />
        </pattern>
        <pattern id="d-carpet-dark" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="#4d5560" /><rect width="4" height="4" fill="#545c68" /><rect x="4" y="4" width="4" height="4" fill="#545c68" />
        </pattern>
        <pattern id="d-carpet-warm" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="#b08a6a" /><rect width="4" height="4" fill="#b89272" /><rect x="4" y="4" width="4" height="4" fill="#b89272" />
        </pattern>
        <clipPath id="d-screen-clip"><rect x="142" y="7" width="108" height="46" /></clipPath>
      </defs>

      {/* 后墙 */}
      <rect width={W} height="64" fill="#ece3d2" />
      <rect x="396" y="0" width="84" height="64" fill="#e2d6bf" />
      <rect width={W} height="3" fill="#d7cbb5" />
      {[80, 184, 290, 424].map((x) => <rect key={x} x={x} y="1" width="24" height="2" fill={lamp} />)}
      <rect y="54" width={W} height="10" fill="#e0d5c0" /><rect y="54" width={W} height="1" fill="#cfc2aa" /><rect y="62" width={W} height="2" fill="#b9a98c" />

      {/* 地面：大厅木地板，经理室地毯，下半层三间各一种地毯 */}
      <rect y="64" width="393" height="88" fill="url(#d-wood)" />
      <rect x="396" y="64" width="84" height="88" fill="url(#d-carpet)" />
      <rect y="156" width="180" height={H - 156} fill="url(#d-carpet-blue)" />
      <rect x="184" y="156" width="160" height={H - 156} fill="url(#d-carpet-dark)" />
      <rect x="348" y="156" width="132" height={H - 156} fill="url(#d-carpet-warm)" />
      <rect y="64" width={W} height="2" fill="#2a241d" opacity="0.1" />

      {/* 大厅后墙：挂钟、茶水台、窗、行情大屏、窗 */}
      <circle cx="28" cy="13" r="8" fill="#2a241d" /><circle cx="28" cy="13" r="6.6" fill="#fbf8f2" />
      {[[28, 7.4], [28, 17.6], [22.4, 12.5], [32.6, 12.5]].map(([x, y]) => <rect key={`${x}-${y}`} x={x - 0.5} y={y} width="1" height="1" fill="#8a7f70" />)}
      <rect x="6" y="46" width="32" height="18" fill="#f3ede0" /><rect x="21.5" y="47" width="1" height="15" fill="#d2c5ad" />
      <rect x="18" y="52" width="2" height="1" fill="#b9a98c" /><rect x="24" y="52" width="2" height="1" fill="#b9a98c" />
      <rect x="5" y="44" width="34" height="2" fill="#8f6a42" /><rect x="6" y="62" width="32" height="2" fill="#b9a98c" />
      <rect x="9" y="30" width="11" height="14" fill="#2b2f36" /><rect x="9" y="30" width="11" height="3" fill="#c8322b" />
      <rect x="13" y="36" width="3" height="2" fill="#111317" /><rect x="13" y="40" width="3" height="3" fill="#ffffff" /><rect x="10" y="43" width="9" height="1" fill="#565b63" />
      <rect className="d-led" x="17" y="34" width="1" height="1" fill="#7fd1a5" />
      <rect className="d-steam" x="14" y="38" width="1" height="2" fill="#ffffff" /><rect className="d-steam late" x="15" y="37" width="1" height="2" fill="#ffffff" />
      <rect x="24" y="39" width="3" height="5" fill="#ffffff" /><rect x="24" y="41" width="3" height="1" fill="#d2c5ad" /><rect x="29" y="40" width="4" height="4" fill="#d9a05b" /><rect x="29" y="39" width="4" height="1" fill="#8f6a42" />
      <rect x="42" y="20" width="8" height="12" fill="#b6dcf0" /><rect x="42" y="24" width="8" height="8" fill="#7fbfe0" /><rect x="44" y="32" width="4" height="2" fill="#9ccbe4" />
      <rect className="d-steam late" x="45" y="29" width="1" height="1" fill="#e6f5fc" />
      <rect x="41" y="34" width="10" height="30" fill="#f8f4ea" /><rect x="49" y="34" width="2" height="30" fill="#e0d8c8" />
      <rect x="43" y="40" width="2" height="2" fill="#c8322b" /><rect x="46" y="40" width="2" height="2" fill="#3b78b5" /><rect x="42" y="45" width="8" height="1" fill="#b9a98c" />
      <Window x={66} y={12} w={56} h={36} id="d-win-1" sky={sky} seed={1} sun={sunStep / 24} />
      <rect x="139" y="4" width="114" height="52" fill="#3a3632" /><rect x="140" y="5" width="112" height="50" fill="#1d1a16" />
      <rect x="142" y="7" width="108" height="46" fill="#0f1a16" /><rect x="142" y="45" width="108" height="8" fill="#18291f" />
      <rect x="194" y="54" width="4" height="1" fill="#c8322b" />
      <Window x={270} y={12} w={48} h={36} id="d-win-2" sky={sky} seed={2} />
      <Window x={336} y={12} w={40} h={36} id="d-win-3" sky={sky} seed={3} />
      <Pixels rows={PLANT} pal={PLANT_PAL} x={380} y={52} />
      <text x="16" y="146" className="d-svg-zone">汇报大厅 · 代码算数，不做判断</text>

      {/* 经理室：白板、门牌，竖隔断中间留门 */}
      <rect x="404" y="9" width="56" height="40" fill="#b9b4aa" /><rect x="406" y="11" width="52" height="36" fill="#fbfaf5" />
      <rect x="412" y="49" width="40" height="2" fill="#b9b4aa" />
      <rect x="416" y="48" width="4" height="1" fill="#c8322b" /><rect x="422" y="48" width="4" height="1" fill="#3b78b5" /><rect x="428" y="48" width="4" height="1" fill="#2a241d" />
      <Window x={464} y={14} w={12} h={32} id="d-win-4" sky={sky} seed={4} />
      <rect x="393" y="56" width="4" height="44" fill="#8a7f70" /><rect x="394" y="58" width="2" height="40" fill="#cfe4ea" />
      <rect x="393" y="126" width="4" height="26" fill="#8a7f70" /><rect x="394" y="128" width="2" height="22" fill="#cfe4ea" />
      <rect x="392" y="98" width="6" height="2" fill="#5d554a" /><rect x="392" y="126" width="6" height="2" fill="#5d554a" /><rect x="393" y="100" width="4" height="26" fill="#c7b596" />
      <rect x="399" y="51" width="20" height="7" fill="#2a241d" /><text x="409" y="56.4" textAnchor="middle" className="d-svg-plate">经理室</text>
      <Pixels rows={PLANT} pal={PLANT_PAL} x={468} y={136} />

      {/* 上下两层之间的矮墙，三扇门 */}
      <rect y="152" width={W} height="4" fill="#8a7f70" /><rect y="152" width={W} height="1" fill="#a39884" />
      {[[84, 26], [252, 26], [404, 26]].map(([x, w]) => <rect key={x} x={x} y="152" width={w} height="4" fill="#c7b596" />)}
      {/* 下半层的两道竖玻璃隔断 */}
      {[180, 344].map((x) => (
        <g key={x}><rect x={x} y="156" width="4" height={H - 156} fill="#8a7f70" /><rect x={x + 1} y="158" width="2" height={H - 160} fill="#cfe4ea" /></g>
      ))}
      {[[10, '策略室'], [194, '交易室'], [358, '会议室']].map(([x, name]) => (
        <g key={name}><rect x={x} y="158" width="20" height="7" fill="#2a241d" /><text x={x + 10} y="163.4" textAnchor="middle" className="d-svg-plate">{name}</text></g>
      ))}
      <text x="34" y="164" className="d-svg-zone light">规则放行，AI 只能否决</text>
      <text x="218" y="164" className="d-svg-zone light">AI 模拟盘</text>
      <text x="382" y="164" className="d-svg-zone">周五复盘 · 午休</text>

      {/* 策略室：书架和绿植 */}
      <rect x="150" y="232" width="24" height="30" fill="#8f6a42" /><rect x="152" y="234" width="20" height="8" fill="#6d4a2e" /><rect x="152" y="244" width="20" height="8" fill="#6d4a2e" /><rect x="152" y="254" width="20" height="6" fill="#6d4a2e" />
      {[[153, 235, '#c8322b'], [156, 235, '#3b78b5'], [159, 236, '#efe8da'], [163, 235, '#5b7f3a'], [154, 245, '#d9a21b'], [158, 246, '#efe8da'], [162, 245, '#7a4fa0'], [166, 245, '#c8322b']].map(([x, y, c]) => (
        <rect key={`${x}-${y}`} x={x} y={y} width="2" height={y % 2 ? 7 : 6} fill={c} />
      ))}
      <Pixels rows={PLANT} pal={PLANT_PAL} x={6} y={250} />

      {/* 交易室：机柜 */}
      <rect x="324" y="238" width="18" height="2" fill="#2a241d" opacity="0.2" />
      <rect x="322" y="200" width="18" height="38" fill="#2b2f36" /><rect x="322" y="200" width="18" height="2" fill="#434851" />
      {[0, 1, 2, 3].map((i) => (
        <g key={i}>
          <rect x="324" y={204 + i * 8} width="14" height="6" fill="#181b20" />
          <rect className="d-led" style={{ animationDelay: `${-i * 0.31}s` }} x="325" y={206 + i * 8} width="1" height="1" fill="#7fd1a5" />
          <rect className="d-led fast" style={{ animationDelay: `${-i * 0.47}s` }} x="327" y={206 + i * 8} width="1" height="1" fill={i === 2 ? '#f0c36a' : '#7fd1a5'} />
          <rect x="330" y={206 + i * 8} width="7" height="1" fill="#2f353d" />
        </g>
      ))}

      {/* 会议室：会议桌、沙发、复印机、绿植 */}
      <rect x="374" y="214" width="70" height="3" fill="#2a241d" opacity="0.18" />
      <rect x="372" y="192" width="70" height="20" fill="#9a6842" /><rect x="372" y="192" width="70" height="1" fill="#b07e55" /><rect x="372" y="211" width="70" height="2" fill="#6d4128" />
      <rect x="402" y="198" width="10" height="6" fill="#fbf8f2" /><rect x="418" y="200" width="3" height="3" fill="#ffffff" /><rect x="384" y="199" width="8" height="5" fill="#2b2f36" />
      {[382, 402, 422].map((x) => <g key={x}><rect x={x} y="181" width="12" height="9" fill="#4a4f57" /><rect x={x + 1} y="180" width="10" height="1" fill="#5d636c" /></g>)}
      <g transform="translate(42,92)">
        <rect x="312" y="152" width="54" height="14" fill="#7a4a3a" /><rect x="313" y="152" width="52" height="2" fill="#93604c" />
        <rect x="338" y="154" width="1" height="11" fill="#653a2d" /><rect x="309" y="156" width="4" height="13" fill="#653a2d" /><rect x="365" y="156" width="4" height="13" fill="#653a2d" />
        <rect x="312" y="166" width="54" height="3" fill="#55301f" /><rect x="311" y="169" width="58" height="2" fill="#2a241d" opacity="0.18" />
      </g>
      <rect x="456" y="206" width="15" height="20" fill="#ebe5d8" /><rect x="456" y="202" width="15" height="4" fill="#cfc8ba" /><rect x="456" y="202" width="15" height="1" fill="#ded7c9" />
      <rect x="458" y="210" width="11" height="1" fill="#a79d8b" /><rect x="460" y="209" width="7" height="2" fill="#ffffff" />
      <rect x="458" y="216" width="11" height="4" fill="#d6cfc1" /><rect className="d-led" x="468" y="207" width="1" height="1" fill="#7fd1a5" />
      <Pixels rows={PLANT} pal={PLANT_PAL} x={466} y={250} />
    </g>
  )
})

function tickerOf(data) {
  const m = data?.manager
  const parts = [m ? `经理 ${m.slot} ${m.regime}` : '经理 09:26 开会']
  ;(data?.traders || []).forEach((t) => {
    const a = t.account || {}
    if (a.today != null) parts.push(`${t.label} 今日 ${a.today > 0 ? '+' : ''}${Number(a.today).toFixed(2)}%`)
  })
  const ma = data?.mgr?.account
  if (ma?.today != null) parts.push(`经理账户 今日 ${ma.today > 0 ? '+' : ''}${Number(ma.today).toFixed(2)}%`)
  ;(data?.roles || []).filter((r) => r.level === 'bad' || r.level === 'warn').forEach((r) => parts.push(`${r.name}：${r.line}`))
  const text = parts.join('   ◆   ')
  const width = [...text].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? 5 : 2.8), 0)
  return { text, width: Math.ceil(width / 20) * 20 }
}

const REGIME_TONE = { 打板日: 'up', 趋势日: 'amber', 混沌日: 'flat', 空仓日: 'down' }
const CHAT_PAIRS = [[0, 1], [2, 3], [4, 5], [7, 8], [10, 11], [5, 6]]
const SEAT_NAME = Object.fromEntries(SEATS.map((s) => [s.role, s.name]))
const MEET_X = { manager: 382, 'trader:aiflex': 402, 'trader:aiflex2': 422 }      // 会议桌近侧的三个位子
const MEET_Y = 216
const minutesOf = (hhmm) => (hhmm ? +hhmm.slice(0, 2) * 60 + +hhmm.slice(3, 5) : -999)

function footOf(sc, phase, n) {
  if (sc.stage === 'closed') return '周末休市 · 办公室没人'
  if (sc.stage === 'empty') return sc.m < 525 ? '还没上班 · 08:45 开始到岗' : '下班了 · 办公室没人，程序还在机柜里跑'
  if (sc.stage === 'arrive') return `盘前 · 陆续到岗 ${n}/13`
  if (sc.stage === 'lunch') return '午休 · 13:00 开工'
  if (sc.stage === 'review') return '收盘 · 经理在白板前复盘'
  if (sc.stage === 'leave') return sc.meeting ? `周五 · 经理和两位交易员在会议室开周复盘会，其他人陆续下班` : `收盘 · 陆续下班，还剩 ${n} 人`
  return `${phase} · 全员在岗`
}

export function Office({ data, active, picked, event, phase, onPick }) {
  const roles = Object.fromEntries((data?.roles || []).map((r) => [r.role, r]))
  const traders = Object.fromEntries((data?.traders || []).map((t) => [`trader:${t.sid}`, t]))
  const sd = data?.strategyDesk
  const strats = Object.fromEntries((sd?.traders || []).map((t) => [`strat:${t.key}`, t]))
  const m = data?.manager
  const sc = sceneOf(data?.now)
  const { sky, sun } = skyOf(data?.now)
  const hh = data?.now ? +data.now.slice(11, 13) : 12
  const mm = data?.now ? +data.now.slice(14, 16) : 0
  const todo = (sd?.final?.items || []).filter((i) => i.do)
  const working = sc.stage === 'work' || sc.stage === 'arrive' || sc.stage === 'review' || sc.stage === 'leave'
  const lightsOn = working || sc.stage === 'lunch'
  const dark = lightsOn ? (sc.stage === 'lunch' ? 0.16 : 0) : sky === 'night' ? 0.56 : sky === 'day' ? 0.16 : 0.38

  // 真实事件：经理刚判断完的 8 分钟站到白板前；策略室刚汇总完的 12 分钟，跑腿手里才有票
  const mgrFresh = m?.ts && data?.now ? (new Date(data.now) - new Date(m.ts)) / 60000 : 999
  const where = (role) => (role === 'manager' && sc.stage === 'work' && mgrFresh >= 0 && mgrFresh < 8 ? 'board' : sc.where(role))
  const sinceDesk = sc.m - minutesOf(sd?.slot)
  const carrying = sinceDesk >= 0 && sinceDesk < 12 && (sd?.traders || []).some((t) => t.picks.length)

  const infoOf = (s) => {
    const st = where(s.role)
    const at = st === 'seat'
    const idle = st === 'nap' || st === 'chat' || st === 'meeting' || (sc.stage === 'lunch' && st !== 'seat')
    if (s.kind === 'report') {
      const r = roles[s.role]
      const tone = r && r.level !== 'idle' ? r.level : null
      return { st, tone, mode: at && tone ? 'on' : at || idle ? 'saver' : 'off', alert: r?.level === 'bad' }
    }
    if (s.kind === 'strat') {
      const t = strats[s.role]
      const tone = !t ? null : t.error ? 'bad' : t.picks.length ? 'ok' : 'calm'
      return { st, tone, mode: at && tone ? 'on' : at || idle ? 'saver' : 'off', alert: !!t?.error, picks: t?.picks.length || 0 }
    }
    const t = traders[s.role]
    const d = t?.directive
    const today = t?.account?.today
    return { st, tone: t ? 'calm' : null, mode: at && t ? 'on' : at || idle ? 'saver' : 'off', gate: d ? (d.buy ? '开' : '关') : null,
      pnl: today == null ? '—' : `${today > 0 ? '+' : ''}${Number(today).toFixed(1)}`, pnlTone: today > 0 ? 'up' : today < 0 ? 'down' : 'calm' }
  }
  const infos = Object.fromEntries(SEATS.map((s) => [s.role, infoOf(s)]))
  const mgrAt = where('manager')

  // 日常小动作（纯装饰，按分钟换人）：邻座聊两句、伸个懒腰
  const chat = sc.stage === 'work' ? CHAT_PAIRS[Math.floor(sc.m / 3) % (CHAT_PAIRS.length + 2)] : null
  const chatOn = chat && chat.every((k) => infos[SEATS[k].role].st === 'seat' && SEATS[k].role !== active)
  const stretcher = sc.stage === 'work' || sc.stage === 'lunch' ? (Math.floor(sc.m / 2) * 5 + 3) % SEATS.length : -1

  const seat = SEATS.find((s) => s.role === active)
  const speech = active ? speechOf(active, data) : null
  const activeAt = !active ? null : active === 'manager' ? mgrAt : infos[active].st
  const anchor = !active ? null
    : activeAt === 'meeting' ? [MEET_X[active] + 6, MEET_Y - 8]
      : active === 'manager' ? (mgrAt === 'board' ? [446, 49] : mgrAt === 'sofa' ? [372, 240] : [MGR.x + 6, MGR.y - 12])
        : [seat.x + seat.w / 2, seat.y - 12]
  const away = activeAt === 'gone'
  const who = active === 'manager' ? '经理' : SEAT_NAME[active]
  const bubbleText = !speech ? '' : event && event.role === active ? event.text : away ? `（人不在）走之前说的：${speech.short}` : speech.short

  const breadth = roles.breadth
  const nums = (breadth?.line || '').match(/涨 (\d+) \/ 跌 (\d+)/)
  const up = nums ? +nums[1] : 0
  const down = nums ? +nums[2] : 0
  const share = up + down ? up / (up + down) : 0.5
  const ticker = tickerOf(data)
  const hand = (deg, len) => ({ x2: 28 + Math.sin((deg * Math.PI) / 180) * len, y2: 13 - Math.cos((deg * Math.PI) / 180) * len })
  const runner = sc.stage === 'work' || sc.stage === 'arrive' ? 'walk' : sc.stage === 'lunch' || sc.stage === 'review' || (sc.stage === 'leave' && sc.m < 990) ? 'stand' : 'gone'
  const chatters = SEATS.filter((s) => infos[s.role].st === 'chat')
  const meeting = ['manager', 'trader:aiflex', 'trader:aiflex2'].filter((r) => where(r) === 'meeting')
  const lookOf = (role) => (role === 'manager' ? MANAGER : SEATS.find((s) => s.role === role))

  const rowA = SEATS.filter((s) => s.y === ROW_A)
  const rowB = SEATS.filter((s) => s.y === ROW_B)
  const items = (rows) => rows.map((s) => <DeskItems key={s.role} s={s} info={infos[s.role]} i={SEATS.indexOf(s)} />)
  const people = (rows) => rows.map((s) => {
    const i = SEATS.indexOf(s)
    const info = infos[s.role]
    const here = info.st === 'seat' || info.st === 'nap'
    const lean = chatOn && chat.includes(i) ? (i === chat[0] ? 1 : -1) : 0
    const px = personX(s)
    return (
      <g key={s.role}>
        {here && <Sitter look={s} x={px + lean} y={s.y + 4} state={info.st} active={active === s.role && info.st === 'seat'} typing={info.mode === 'on'}
          stretch={i === stretcher && info.st === 'seat'} i={i} />}
        <Chair x={px} y={s.y} tucked={!here} />
        {info.st === 'nap' && <text className="d-zz" style={{ animationDelay: `${-i * 0.45}s` }} x={px + 12} y={s.y + 3}>z</text>}
        <rect x={px - 4} y={s.y + 26} width="2" height="5" fill={s.color} />
        <text x={px} y={s.y + 30.4} className={`d-svg-name ${s.y === ROW_B ? 'light' : ''}`}>{s.name}</text>
      </g>
    )
  })

  return (
    <div className="d-office">
      <svg viewBox={`0 0 ${W} ${H}`} shapeRendering="crispEdges" role="img" aria-label="交易台办公室：汇报大厅、经理室、策略室、交易室和会议室，按北京时间上下班">
        <Backdrop sky={sky} sunStep={Math.round(sun * 24)} lights={lightsOn} />
        {/* 挂钟指针 */}
        <line x1="28" y1="13" {...hand(((hh % 12) + mm / 60) * 30, 3.2)} stroke="#2a241d" strokeWidth="1.2" />
        <line x1="28" y1="13" {...hand(mm * 6, 5)} stroke="#2a241d" strokeWidth="0.7" />
        <rect x="27.5" y="12.5" width="1" height="1" fill="#c8322b" />
        {/* 大屏上的字 */}
        <g clipPath="url(#d-screen-clip)">
          {nums ? (
            <text x="146" y="17" className="d-svg-screen">
              <tspan className="up">涨 {up}</tspan><tspan dx="4" className="down">跌 {down}</tspan>
              <tspan dx="4" className="dim">{breadth.line.split('·').slice(1, 2).join('').trim()}</tspan>
            </text>
          ) : <text x="146" y="17" className="d-svg-screen dim">{breadth?.line || '宽度 · 等开盘'}</text>}
          <rect x="146" y="21" width="100" height="4" fill="#2f8a4c" />
          <rect x="146" y="21" width={Math.round(100 * share)} height="4" fill="#d8443b" />
          <text x="146" y="33" className="d-svg-screen small">{roles.ladder ? roles.ladder.line.split('｜')[0] : '梯队 · 等开盘'}</text>
          <text x="146" y="41" className="d-svg-screen small amber">
            {sd ? (todo.length ? `今日可做 ${todo.length} 只：${todo.map((i) => i.name).join('、')}` : `${sd.slot} 没有应该做的票`) : '今日可做 · 09:26 汇总'}
          </text>
          <text key={ticker.width} x="142" y="50.6" className="d-svg-screen small d-ticker"
            style={{ '--tw': `${ticker.width}px`, animationDuration: `${Math.round((110 + ticker.width) / 11)}s` }}>{ticker.text}</text>
        </g>
        {/* 经理室的白板 */}
        <text x="409" y="19" className="d-svg-board">{data?.now ? data.now.slice(5, 10).replace('-', '/') : '—'} {sc.stage === 'review' ? '复盘' : '晨会'}</text>
        <text x="409" y="32" className={`d-svg-board big ${REGIME_TONE[m?.regime] || ''}`}>{m ? m.regime : '未开会'}</text>
        <text x="409" y="42" className="d-svg-board">{data?.stance ? `档位 ${data.stance.level}` : ''}{m ? ` · 可做 ${sd ? todo.length : '—'}` : ' 09:26 开会'}</text>
        <text x="331" y="246" textAnchor="middle" className="d-svg-zone light">DeepSeek ×{data?.calls ?? 0}</text>

        {/* 汇报大厅：一排 7 个汇报席（桌子 → 桌上 → 人 → 椅背） */}
        <DeskTop x={14} y={ROW_A} w={368} seams={rowA.slice(1).map((s) => s.x - 1)} />
        {items(rowA)}
        {people(rowA)}

        {/* 经理室 */}
        <DeskTop x={406} y={MGR.y} w={60} c={WALNUT_DESK} />
        <Monitor x={MGR.x - 3} y={MGR.y - 9} mode={mgrAt === 'seat' && m ? 'on' : mgrAt === 'gone' ? 'off' : 'saver'} tone="calm" i={5} />
        <rect x="454" y={MGR.y - 3} width="8" height="3" fill="#2f8a6c" /><rect x="455" y={MGR.y - 3} width="6" height="1" fill="#57b592" />
        <rect x="457" y={MGR.y} width="1" height="5" fill="#c9a13b" /><rect x="455" y={MGR.y + 5} width="6" height="1" fill="#c9a13b" />
        {Array.from({ length: Math.min(todo.length, 3) }, (_, k) => (
          <g key={k}><rect x={410 + (k % 2)} y={MGR.y + 8 - k * 2} width="6" height="1" fill="#ffffff" /><rect x={410 + (k % 2)} y={MGR.y + 9 - k * 2} width="6" height="1" fill="#c9c0ae" /></g>
        ))}
        {mgrAt === 'seat' && <Sitter look={MANAGER} x={MGR.x} y={MGR.y + 4} state="seat" active={active === 'manager'} typing={!!m} i={13} />}
        <Chair x={MGR.x} y={MGR.y} tall tucked={mgrAt !== 'seat'} />
        <rect x={MGR.x - 1} y={MGR.y + 26} width="14" height="6" fill="#c9a13b" /><text x={MGR.x + 6} y={MGR.y + 30.8} textAnchor="middle" className="d-svg-name dark">经理</text>
        {mgrAt === 'board' && <Stander look={MANAGER} x={442} y={50} writing />}

        {/* 过道：跑腿（盘中来回走，刚汇总完手里有票）；午休时两个同事在茶水台边端着杯子聊天 */}
        {runner !== 'gone' && (
          <g transform="translate(140,124)">
            <g className={runner === 'walk' ? 'd-runner' : undefined}>
              <g className={runner === 'walk' ? 'd-runner-in' : undefined}>
                <Pixels rows={RUNNER} pal={RUNNER_PAL} />
                {runner === 'walk' ? (
                  <>
                    <g className="d-leg a"><Pixels rows={LEGS_A} pal={RUNNER_PAL} y={13} /></g>
                    <g className="d-leg b"><Pixels rows={LEGS_B} pal={RUNNER_PAL} y={13} /></g>
                    {carrying && <g className="d-carry"><rect x="8" y="6" width="3" height="4" fill="#ffffff" /><rect x="8" y="10" width="3" height="1" fill="#c9c0ae" /></g>}
                  </>
                ) : (
                  <>
                    <Pixels rows={LEGS_B} pal={RUNNER_PAL} y={13} />
                    <rect x="8" y="7" width="2" height="3" fill="#ffffff" /><rect className="d-steam" x="8" y="5" width="1" height="2" fill="#ffffff" />
                  </>
                )}
              </g>
            </g>
          </g>
        )}
        {chatters.map((s, k) => <Stander key={s.role} look={s} x={60 + k * 16} y={122} front cup />)}
        {chatters.length > 0 && (
          <g><rect x="56" y="114" width="11" height="5" fill="#ffffff" /><rect x="58" y="119" width="2" height="1" fill="#ffffff" />
            {[0, 1, 2].map((k) => <rect key={k} className="d-led" style={{ animationDelay: `${-k * 0.4}s` }} x={58 + k * 3} y="116" width="1" height="1" fill="#2a241d" />)}</g>
        )}

        {/* 策略室、交易室 */}
        <DeskTop x={8} y={ROW_B} w={167} seams={rowB.filter((s) => s.kind === 'strat').slice(1).map((s) => s.x - 1)} />
        <DeskTop x={190} y={ROW_B} w={126} seams={[253]} />
        {items(rowB)}
        {people(rowB)}
        {chatOn && (() => {
          const [a, b] = chat.map((k) => SEATS[k])
          const cx = Math.round((personX(a) + personX(b)) / 2) + 1
          return (
            <g><rect x={cx} y={a.y - 14} width="11" height="5" fill="#ffffff" /><rect x={cx + 2} y={a.y - 9} width="2" height="1" fill="#ffffff" />
              {[0, 1, 2].map((k) => <rect key={k} className="d-led" style={{ animationDelay: `${-k * 0.4}s` }} x={cx + 2 + k * 3} y={a.y - 12} width="1" height="1" fill="#2a241d" />)}</g>
          )
        })()}

        {/* 会议室：周五收盘后的复盘会；午休时经理躺沙发 */}
        {meeting.map((role, k) => (
          <g key={role}>
            <Sitter look={lookOf(role)} x={MEET_X[role]} y={MEET_Y} state="seat" active={active === role} typing={false} i={k} />
            <Chair x={MEET_X[role]} y={MEET_Y - 4} tall={role === 'manager'} />
          </g>
        ))}
        {meeting.length > 0 && (
          <g><rect x="396" y="184" width="11" height="5" fill="#ffffff" /><rect x="398" y="189" width="2" height="1" fill="#ffffff" />
            {[0, 1, 2].map((k) => <rect key={k} className="d-led" style={{ animationDelay: `${-k * 0.4}s` }} x={398 + k * 3} y="186" width="1" height="1" fill="#2a241d" />)}</g>
        )}
        {mgrAt === 'sofa' && (
          <g transform="translate(40,97)">
            <rect x="322" y="148" width="26" height="5" fill="#2c3550" /><rect x="348" y="149" width="10" height="4" fill="#3b3f46" /><rect x="358" y="149" width="3" height="4" fill="#22252b" />
            <rect x="316" y="147" width="6" height="6" fill={SKINS[MANAGER.skin]} /><rect x="315" y="146" width="5" height="4" fill={MANAGER.hairColor} />
            <text className="d-zz" x="322" y="145">z</text><text className="d-zz" style={{ animationDelay: '-1.3s' }} x="326" y="142">z</text>
          </g>
        )}

        {/* 关灯：整间屋子压暗，只有机柜的灯还在闪（程序确实还在跑） */}
        {dark > 0 && <rect width={W} height={H} fill={sc.stage === 'lunch' ? '#2a241d' : '#0b1020'} opacity={dark} pointerEvents="none" />}
        {!lightsOn && [0, 1, 2, 3].map((i) => (
          <g key={i}>
            <rect className="d-led" style={{ animationDelay: `${-i * 0.31}s` }} x="325" y={206 + i * 8} width="1" height="1" fill="#7fd1a5" />
            <rect className="d-led fast" style={{ animationDelay: `${-i * 0.47}s` }} x="327" y={206 + i * 8} width="1" height="1" fill={i === 2 ? '#f0c36a' : '#7fd1a5'} />
          </g>
        ))}

        {/* 点击区：整块工位都能点 */}
        {SEATS.map((s) => (
          <rect key={s.role} className={`d-hit ${active === s.role ? 'is-on' : ''}`} x={s.x} y={s.y - 10} width={s.w - 1} height="42" onClick={() => onPick(s.role)}>
            <title>{`${s.name}：点开看完整汇报`}</title>
          </rect>
        ))}
        <rect className={`d-hit ${active === 'manager' ? 'is-on' : ''}`} x="406" y={MGR.y - 10} width="60" height="43" onClick={() => onPick('manager')}>
          <title>经理：点开看完整判断</title>
        </rect>
      </svg>
      {picked && speech ? (
        <div className={`d-sheet ${anchor[0] < W / 2 ? 'right' : 'left'}`} role="dialog" aria-label={`${who}的汇报`}>
          <header><b>{who}</b><span>{away ? '人不在 · 这是走之前说的' : '当面汇报'}</span><button onClick={() => onPick(active)} aria-label="关闭">×</button></header>
          <div className="d-sheet-body">
            <p className="lead">{event && event.role === active ? event.text : speech.short}</p>
            {speech.full.map((line, k) => <p key={k}>{line}</p>)}
            {!speech.full.length && <p className="dim">就这些，没有更多要报的。</p>}
          </div>
        </div>
      ) : bubbleText && (
        <>
          <div className="d-bubble" style={{ left: `${Math.min(84, Math.max(16, (anchor[0] / W) * 100))}%`, top: `${(anchor[1] / H) * 100}%` }}>{bubbleText}</div>
          <div className="d-tail" style={{ left: `${(anchor[0] / W) * 100}%`, top: `${(anchor[1] / H) * 100}%` }} />
        </>
      )}
      <div className="d-office-foot">
        <span className={`d-dot ${working ? 'work' : sc.stage === 'lunch' ? 'nap' : ''}`} />
        <span>{footOf(sc, phase, sc.present.length)}</span>
        <span className="d-office-hint">点小人看完整汇报，滚轮往下翻；再点一次收起</span>
      </div>
    </div>
  )
}
