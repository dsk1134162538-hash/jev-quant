import { useEffect, useMemo, useState } from 'react'

// 交易台（dj 2026-11-09，仿「横行 SIDEWAYS」）：经理 + 代码汇报员 + DeepSeek 交易员，一屏看清谁在干什么、卡在哪一层。

function useDesk() {
  const [state, setState] = useState({ data: null, error: '', at: 0 })
  useEffect(() => {
    let stopped = false
    let controller = null
    async function tick() {
      if (document.hidden) return
      controller?.abort()
      controller = new AbortController()
      try {
        const res = await fetch('/api/desk', { signal: controller.signal, cache: 'no-store' })
        if (!res.ok) throw new Error(`接口 ${res.status}`)
        const data = await res.json()
        if (!stopped) setState({ data, error: '', at: Date.now() })
      } catch (e) {
        if (e.name !== 'AbortError' && !stopped) setState((s) => ({ ...s, error: e.message }))
      }
    }
    tick()
    const timer = setInterval(tick, 2000)
    const onVis = () => { if (!document.hidden) tick() }
    document.addEventListener('visibilitychange', onVis)
    return () => { stopped = true; controller?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', onVis) }
  }, [])
  return state
}

const tone = (v) => (v == null || Number.isNaN(v) || Math.abs(v) < 0.005 ? 'flat' : v > 0 ? 'up' : 'down')
const pct = (v, d = 2) => (v == null || Number.isNaN(v) ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}%`)
const money = (v) => (v == null ? '—' : v >= 10000 ? `${(v / 1000).toFixed(1)}K` : Number(v).toFixed(0))
const REGIME_TONE = { 打板日: 'up', 趋势日: 'amber', 混沌日: 'flat', 空仓日: 'down' }

function phaseOf(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (d.getDay() === 0 || d.getDay() === 6) return '休市'
  const m = d.getHours() * 60 + d.getMinutes()
  if (m < 570) return '盘前'
  if (m < 690) return '盘中'
  if (m < 780) return '午休'
  if (m < 900) return '盘中'
  return '收盘'
}

// ---------- 像素小人 ----------
const SPRITES = {
  crab: ['.x....x.', 'x.x..x.x', '.xxxxxx.', 'xxoxxoxx', 'xxxxxxxx', '.x.xx.x.', 'x......x'],
  cat: ['x.....x.', 'xx...xx.', 'xxxxxxx.', 'xoxxxox.', 'xxxkxxx.', '.xxxxx..', '.x...x..'],
  octo: ['..xxxx..', '.xxxxxx.', 'xxoxxoxx', 'xxxxxxxx', 'xxxxxxxx', 'x.x..x.x', 'x.x..x.x'],
  bird: ['..xxx...', '.xxxxx..', '.xoxxxkk', 'xxxxxx..', 'xxxxxx..', '.xxxx...', '.x..x...'],
  boss: ['.kkkkkk.', '.kkkkkk.', 'kkkkkkkk', '.xxxxxx.', '.xoxxox.', '.xxxxxx.', '..xrrx..', '.xxrrxx.'],
}

function Sprite({ kind, color, x, y, s = 2, active }) {
  const rows = SPRITES[kind] || SPRITES.crab
  const cells = []
  rows.forEach((row, j) => [...row].forEach((c, i) => {
    if (c === '.') return
    const fill = c === 'o' ? '#1d1a16' : c === 'k' ? '#2a241d' : c === 'r' ? '#c8322b' : color
    cells.push(<rect key={`${i}-${j}`} x={i * s} y={j * s} width={s} height={s} fill={fill} />)
  }))
  return <g transform={`translate(${x},${y})`} className={active ? 'd-sprite is-active' : 'd-sprite'}>{cells}</g>
}

// 办公室里的工位（viewBox 320×180）；role 对应汇报员 / 交易员 / 经理
const SEATS = [
  { role: 'breadth', x: 22, y: 76, sprite: 'crab', color: '#c8322b' },
  { role: 'ladder', x: 82, y: 76, sprite: 'cat', color: '#c98a1b' },
  { role: 'high', x: 142, y: 76, sprite: 'octo', color: '#7a4fa0' },
  { role: 'movers', x: 202, y: 76, sprite: 'bird', color: '#3b78b5' },
  { role: 'scan', x: 262, y: 76, sprite: 'crab', color: '#d0662a' },
  { role: 'book', x: 22, y: 128, sprite: 'cat', color: '#5b7f3a' },
  { role: 'risk', x: 82, y: 128, sprite: 'octo', color: '#a33a52' },
  { role: 'trader:aiflex', x: 202, y: 128, sprite: 'bird', color: '#d9a21b' },
  { role: 'trader:aiflex2', x: 262, y: 128, sprite: 'crab', color: '#2f8a6c' },
]

function Desk({ x, y, lit }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="-4" y="14" width="40" height="10" fill="#b98d5c" />
      <rect x="-4" y="24" width="40" height="3" fill="#8f6a42" />
      <rect x="-2" y="27" width="3" height="8" fill="#8f6a42" /><rect x="31" y="27" width="3" height="8" fill="#8f6a42" />
      <rect x="10" y="2" width="16" height="11" fill="#2a241d" />
      <rect x="11" y="3" width="14" height="9" fill={lit ? '#7fd1a5' : '#4a5a63'} />
      <rect x="16" y="13" width="4" height="2" fill="#2a241d" />
    </g>
  )
}

function Office({ data, active }) {
  const roles = Object.fromEntries((data?.roles || []).map((r) => [r.role, r]))
  const traders = Object.fromEntries((data?.traders || []).map((t) => [`trader:${t.sid}`, t]))
  const m = data?.manager
  const breadth = roles.breadth
  const nums = (breadth?.line || '').match(/涨 (\d+) \/ 跌 (\d+)/)
  const up = nums ? +nums[1] : 0
  const down = nums ? +nums[2] : 0
  const share = up + down ? up / (up + down) : 0.5
  const bubbleSeat = SEATS.find((s) => s.role === active)
  const bubbleText = active === 'manager' ? (m ? `${m.regime}｜${m.watch || m.read}` : '等 09:26 开会')
    : roles[active]?.line || (traders[active] ? traderLine(traders[active]) : '')
  const bx = active === 'manager' ? 150 : bubbleSeat?.x ?? 0
  const by = active === 'manager' ? 112 : bubbleSeat?.y ?? 0
  return (
    <div className="d-office">
      <svg viewBox="0 0 320 180" shapeRendering="crispEdges" role="img" aria-label="交易台办公室：每个岗位一个小人，正在汇报的那个会动">
        <rect width="320" height="64" fill="#e9dfcd" />
        <rect y="62" width="320" height="2" fill="#cbbb9f" />
        {Array.from({ length: 12 }, (_, i) => Array.from({ length: 5 }, (_, j) => (
          <rect key={`${i}-${j}`} x={i * 28 - (j % 2) * 14} y={64 + j * 24} width="27" height="23" fill={(i + j) % 2 ? '#efe6d6' : '#ebe1cf'} />
        )))}
        {/* 窗户 */}
        <rect x="10" y="10" width="46" height="34" fill="#3a4a5c" /><rect x="12" y="12" width="20" height="14" fill="#5c7a96" />
        <rect x="34" y="12" width="20" height="14" fill="#5c7a96" /><rect x="12" y="28" width="20" height="14" fill="#4f6b85" />
        <rect x="34" y="28" width="20" height="14" fill="#4f6b85" />
        <text x="12" y="52" className="d-svg-tag">SSE · SZSE</text>
        {/* 大屏：涨跌比例 + 宽度 */}
        <rect x="96" y="6" width="120" height="50" fill="#1d1a16" />
        <rect x="99" y="9" width="114" height="44" fill="#14211c" />
        <text x="104" y="21" className="d-svg-screen">{breadth ? breadth.line.split('·')[0] : '宽度 —'}</text>
        <rect x="104" y="27" width="104" height="6" fill="#2f8a4c" />
        <rect x="104" y="27" width={Math.round(104 * share)} height="6" fill="#d8443b" />
        <text x="104" y="45" className="d-svg-screen dim">{roles.ladder ? roles.ladder.line.split('｜')[0] : '梯队 —'}</text>
        {/* 白板：经理的判断 */}
        <rect x="228" y="8" width="82" height="44" fill="#f7f3ea" stroke="#8f6a42" strokeWidth="2" />
        <text x="233" y="19" className="d-svg-board">案 {data?.now ? data.now.slice(5, 10).replace('-', '/') : '—'}</text>
        <text x="233" y="31" className={`d-svg-board big ${REGIME_TONE[m?.regime] || ''}`}>{m ? m.regime : '未开会'}</text>
        <text x="233" y="44" className="d-svg-board">{m ? `${m.slot} 经理判断` : '09:26 开会'}</text>
        {/* 地毯 + 经理大桌 */}
        <rect x="118" y="112" width="84" height="54" fill="#d9b9a0" opacity="0.55" />
        <rect x="128" y="138" width="64" height="12" fill="#8f5a3c" /><rect x="128" y="150" width="64" height="3" fill="#6d4128" />
        <Sprite kind="boss" color="#e7c39a" x={152} y={118} s={2} active={active === 'manager'} />
        {SEATS.map((s) => {
          const r = roles[s.role]
          const t = traders[s.role]
          const lit = (r && r.level !== 'idle') || (t && t.rounds?.length)
          return (
            <g key={s.role}>
              <Desk x={s.x} y={s.y} lit={lit} />
              <Sprite kind={s.sprite} color={s.color} x={s.x + 10} y={s.y + 16} s={2} active={active === s.role} />
              {r?.level === 'bad' && <rect x={s.x + 28} y={s.y} width="4" height="4" fill="#c8322b" className="d-blink" />}
            </g>
          )
        })}
        <rect x="300" y="100" width="10" height="40" fill="#3a3a3a" /><rect x="302" y="104" width="6" height="4" fill="#7fd1a5" className="d-blink" />
        <rect x="6" y="160" width="10" height="10" fill="#5b7f3a" /><rect x="8" y="152" width="6" height="8" fill="#7aa84f" />
      </svg>
      {bubbleText && (
        <div className="d-bubble" style={{ left: `${((bx + 18) / 320) * 100}%`, top: `${(by / 180) * 100}%` }}>{bubbleText}</div>
      )}
    </div>
  )
}

function traderLine(t) {
  const r = t.rounds?.[t.rounds.length - 1]
  if (!r) return '今天还没跑'
  if (r.skipped) return `${r.slot} ${r.skipped}`
  return `${r.slot} ${r.stage || '—'}｜${r.plan || ''}`
}

// ---------- 顶部岗位卡 ----------
const KIND_TAG = { 行情: '行情', 算术: '算术', 扫描: '扫描', 账户: '账户' }
const PLACEHOLDER = [['breadth', 'BREADTH', '宽度'], ['ladder', 'LADDER', '梯队'], ['high', 'HIGH', '高标'], ['movers', 'MOVERS', '异动'],
  ['scan', 'SCAN', '扫板'], ['book', 'BOOK', '持仓'], ['risk', 'RISK', '风控']]

function RoleCards({ data, active, onPick }) {
  const m = data?.manager
  const roles = (data?.roles || []).length ? data.roles.map((r) => ({ ...r, tag: KIND_TAG[r.kind] || r.kind }))
    : PLACEHOLDER.map(([role, code, name]) => ({ role, code, name, tag: '算术', line: '经理开会时汇报', level: 'idle' }))
  const cards = [
    { role: 'manager', code: 'CHIEF', name: '经理', tag: '问 AI', line: m ? `${m.regime}｜${m.read}` : '等 09:26 开会', level: m ? 'ok' : 'idle' },
    ...roles,
    ...(data?.traders || []).map((t) => ({ role: `trader:${t.sid}`, code: t.sid.toUpperCase(), name: t.label, tag: '问 AI',
      line: traderLine(t), level: t.directive ? (t.directive.buy ? 'ok' : 'bad') : t.rounds?.length ? 'ok' : 'idle' })),
  ]
  return (
    <div className="d-roles" role="list">
      {cards.map((c) => (
        <button key={c.role} role="listitem" className={`d-role lv-${c.level} ${active === c.role ? 'is-on' : ''}`} onClick={() => onPick(c.role)}>
          <span className="d-role-head"><b>{c.code}</b><em className={c.tag === '问 AI' ? 'ai' : ''}>{c.tag}</em></span>
          <span className="d-role-name">{c.name}{c.ts && <i className="mono">{c.ts}</i>}</span>
          <span className="d-role-line" title={c.line}>{c.line}</span>
        </button>
      ))}
    </div>
  )
}

// ---------- 右栏 ----------
function Analysis({ data }) {
  const m = data?.manager
  if (!m) {
    return (
      <section className="d-card d-analysis">
        <header><h2>经理判断</h2></header>
        <p className="d-empty">今天还没开会。经理在 09:26、10:30、13:20 各判断一次；没判断前两位交易员照常开仓。</p>
      </section>
    )
  }
  return (
    <section className="d-card d-analysis">
      <header><h2>经理判断</h2><span className="mono d-dim">{m.slot} · 今天 {m.history?.length || 1} 次</span></header>
      <div className={`d-regime ${REGIME_TONE[m.regime] || ''}`}>{m.regime}</div>
      <p className="d-read">{m.read}</p>
      <ul className="d-checks">
        {(data.traders || []).map((t) => {
          const d = t.directive
          return (
            <li key={t.sid}>
              <span>{t.label}</span>
              <span className="d-why">{d?.why || '—'}</span>
              <b className={d ? (d.buy ? 'pass' : 'drop') : 'flat'}>{d ? (d.buy ? `开 ≤${Math.round((d.maxPct || 0) * 100)}%` : '关') : '照常'}</b>
            </li>
          )
        })}
      </ul>
      <div className="d-falsify"><b>否证条件</b><span>{m.falsify || '—'}</span></div>
      {m.watch && <div className="d-falsify watch"><b>接下来盯</b><span>{m.watch}</span></div>}
      {m.history?.length > 1 && (
        <ol className="d-history">
          {m.history.slice(0, -1).reverse().map((h) => (
            <li key={h.slot}><span className="mono">{h.slot}</span><b className={REGIME_TONE[h.regime] || ''}>{h.regime}</b><span className="d-why">{h.falsify}</span></li>
          ))}
        </ol>
      )}
    </section>
  )
}

function Params({ params }) {
  return (
    <section className="d-card d-params">
      <header><h2>护栏参数</h2><span className="d-dim">程序强制，模型改不了</span></header>
      <dl>
        {(params || []).map((p) => <div key={p.name}><dt>{p.name}</dt><dd className="mono">{p.value}</dd></div>)}
      </dl>
    </section>
  )
}

const TAG_TONE = { 经理: 'mgr', 买入: 'pass', 成交: 'pass', 卖出: 'drop', 丢弃: 'drop', 跳过: 'flat', 判断: 'flat' }

function Log({ log }) {
  return (
    <section className="d-card d-log">
      <header><h2>日志</h2><span className="mono d-dim">{log?.length || 0} 条</span></header>
      {!log?.length ? <p className="d-empty">今天还没有事件</p> : (
        <ol>
          {log.map((e, i) => (
            <li key={`${e.t}-${i}`}>
              <span className="mono d-dim">{e.t.slice(0, 5)}</span>
              <span className="d-who">{e.who}</span>
              <b className={`d-tag ${TAG_TONE[e.tag] || ''}`}>{e.tag}</b>
              <span className="d-text" title={e.text}>{e.text}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

// ---------- 底部：交易员账户 + 漏斗 ----------
function Curve({ points }) {
  const vals = (points || []).map((p) => p.v)
  if (vals.length < 2) return <div className="d-curve-empty">净值走势攒两天才有线</div>
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  const span = max - min || 1
  const w = 300
  const h = 64
  const xy = vals.map((v, i) => [(i / (vals.length - 1)) * w, h - 4 - ((v - min) / span) * (h - 10)])
  const line = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const t = tone(vals[vals.length - 1] - vals[0])
  return (
    <svg className={`d-curve ${t}`} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-label="净值走势">
      <path className="area" d={`${line} L${w},${h} L0,${h} Z`} />
      <path className="line" d={line} />
    </svg>
  )
}

function Funnel({ f }) {
  const steps = [
    ['候选', f.candidates, ''], ['AI 提名', f.named, ''], ['经理拦下', f.manager, 'drop'],
    ['规则丢弃', f.dropped, 'drop'], ['发出订单', f.passed, 'pass'], ['成交', f.filled, 'pass'],
  ]
  return (
    <div className="d-funnel" aria-label="今天的漏斗">
      {steps.map(([name, n, cls]) => (
        <div key={name} className={`d-step ${cls} ${n ? '' : 'zero'}`}><b className="mono">{n ?? 0}</b><span>{name}</span></div>
      ))}
    </div>
  )
}

function TraderCard({ t }) {
  const a = t.account
  const d = t.directive
  return (
    <section className="d-card d-trader">
      <header>
        <h2>{t.label}</h2>
        <span className={`d-gate ${d ? (d.buy ? 'pass' : 'drop') : 'flat'}`}>{d ? (d.buy ? '经理：开' : '经理：关') : '照常'}</span>
      </header>
      <p className="d-style">{t.style}</p>
      <div className="d-equity">
        <b className="mono">{money(a.equity)}</b>
        <span className={`mono ${tone(a.today)}`}>今日 {pct(a.today)}</span>
        <span className={`mono ${tone(a.total)}`}>累计 {pct(a.total, 1)}</span>
      </div>
      <Curve points={t.curve} />
      {a.positions?.length ? (
        <ul className="d-pos">
          {a.positions.map((p) => (
            <li key={p.code}><b>{p.name}</b><span className="mono">{p.price}</span><span className={`mono ${tone(p.pct)}`}>{pct(p.pct)}</span>
              <span className={`mono ${tone(p.pnl)}`}>盈 {pct(p.pnl, 1)}</span></li>
          ))}
        </ul>
      ) : <p className="d-empty small">空仓 · 现金 {money(a.cash)}</p>}
      <Funnel f={t.funnel} />
    </section>
  )
}

export default function DeskPage({ onTerm, onDetail }) {
  const { data, error, at } = useDesk()
  const [picked, setPicked] = useState(null)
  const [beat, setBeat] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setBeat((b) => b + 1), 3200)
    return () => clearInterval(t)
  }, [])
  const order = useMemo(() => ['manager', ...SEATS.map((s) => s.role)], [])
  const active = picked || order[beat % order.length]
  const phase = phaseOf(data?.now)
  const fresh = at && Date.now() - at < 6000 && !error
  const m = data?.manager
  return (
    <div className="desk">
      <header className="d-top">
        <span className="d-brand">衡策<em>TRADING DESK</em></span>
        <span className={`d-phase ${phase === '盘中' ? 'on' : ''}`}>{phase}</span>
        <span className="mono d-clock">{data?.now ? data.now.slice(11, 19) : '--:--:--'}</span>
        {m && <span className={`d-chip ${REGIME_TONE[m.regime] || ''}`}>{m.regime}</span>}
        <span className="d-chip ghost">今日调用 DeepSeek <b className="mono">{data?.calls ?? 0}</b> 次</span>
        <span className={`d-live ${fresh ? 'ok' : 'bad'}`}>{fresh ? '实时' : error || '连接中'}</span>
        <nav>
          <button onClick={onTerm}>盘面</button>
          <button onClick={onDetail}>详细记录</button>
        </nav>
      </header>
      <RoleCards data={data} active={active} onPick={(r) => setPicked(picked === r ? null : r)} />
      <main className="d-grid">
        <div className="d-left">
          <div className="d-office-wrap"><Office data={data} active={active} /></div>
          <div className="d-traders">
            {(data?.traders || []).map((t) => <TraderCard key={t.sid} t={t} />)}
          </div>
        </div>
        <aside className="d-right">
          <Analysis data={data} />
          <Log log={data?.log} />
          <Params params={data?.params} />
        </aside>
      </main>
    </div>
  )
}
