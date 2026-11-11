import { useEffect, useMemo, useState } from 'react'
import { Office, SEATS, STRAT_INFO, liveEvent, sceneOf, stratLine, traderLine } from './Office.jsx'

// 交易台（dj 2026-11-09，仿「横行 SIDEWAYS」）：经理 + 代码汇报员 + 策略交易员 + DeepSeek 交易员，一屏看清谁在干什么、卡在哪一层。
// 像素办公室在 Office.jsx，这里是页面骨架、岗位卡和右栏。

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
const listOf = (v) => (Array.isArray(v) ? v.filter(Boolean) : v ? [v] : [])
const firstSentence = (s) => { const m = String(s || '').match(/^.*?[。；;]/); return m ? m[0] : String(s || '') }
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

// ---------- 顶部岗位卡 ----------
const KIND_TAG = { 行情: '行情', 算术: '算术', 扫描: '扫描', 账户: '账户' }
const PLACEHOLDER = [['breadth', 'BREADTH', '宽度'], ['ladder', 'LADDER', '梯队'], ['high', 'HIGH', '高标'], ['movers', 'MOVERS', '异动'],
  ['scan', 'SCAN', '扫板'], ['book', 'BOOK', '持仓'], ['risk', 'RISK', '风控']]

function RoleCards({ data, active, onPick }) {
  const m = data?.manager
  const roles = (data?.roles || []).length ? data.roles.map((r) => ({ ...r, tag: KIND_TAG[r.kind] || r.kind }))
    : PLACEHOLDER.map(([role, code, name]) => ({ role, code, name, tag: '算术', line: '经理开会时汇报', level: 'idle' }))
  const strats = Object.fromEntries((data?.strategyDesk?.traders || []).map((t) => [t.key, t]))
  const cards = [
    { role: 'manager', code: 'CHIEF', name: '经理', tag: '问 AI', line: m ? `${m.regime}｜${m.read}` : '等 09:26 开会', level: m ? 'ok' : 'idle' },
    ...roles,
    ...STRAT_INFO.map(([key, code, name]) => {
      const t = strats[key]
      return { role: `strat:${key}`, code, name: t?.label || name, tag: '问 AI', line: stratLine(t),
        level: !t ? 'idle' : t.error ? 'bad' : t.picks.length ? 'ok' : 'idle' }
    }),
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

// ---------- 公司目标：两位 AI 交易员合计，对着每天 / 每周 / 每月的目标记分 ----------
function Company({ c, title = '公司目标', sub = '两位 AI 交易员合计' }) {
  if (!c?.items?.length) return null
  return (
    <section className="d-company" aria-label={title}>
      <h2>{title}<em>{sub} · 净值 {Math.round(c.equity).toLocaleString('en-US')}</em></h2>
      {c.items.map((i) => {
        const fill = i.actual == null ? 0 : Math.max(0, Math.min(1, i.actual / i.target))
        return (
          <div key={i.key} className={`d-goal ${i.hit ? 'hit' : ''}`}>
            <span className="d-goal-name">{i.label}</span>
            <b className={`mono ${tone(i.actual)}`}>{pct(i.actual)}</b>
            <span className="d-goal-of mono">/ 目标 +{i.target}%</span>
            <span className="d-goal-bar"><i style={{ width: `${fill * 100}%` }} /></span>
            <span className="d-goal-gap">{i.hit ? '达标' : i.gapYuan == null ? '—' : `还差 ${i.gapYuan.toLocaleString('en-US')} 元`}</span>
          </div>
        )
      })}
    </section>
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
      <p className="d-read">{firstSentence(m.read)}</p>
      {String(m.read || '').length > firstSentence(m.read).length && (
        <details className="d-more"><summary>看经理的完整解读</summary><p className="d-read">{m.read}</p></details>
      )}
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
      <div className="d-falsify"><b>看错的信号</b>
        {listOf(m.falsify).length ? <ul>{listOf(m.falsify).map((f, i) => <li key={i}>{f}</li>)}</ul> : <span>—</span>}</div>
      {listOf(m.watch).length > 0 && (
        <div className="d-falsify watch"><b>接下来盯</b><ul>{listOf(m.watch).map((w, i) => <li key={i}>{w}</li>)}</ul></div>
      )}
      {m.history?.length > 1 && (
        <ol className="d-history">
          {m.history.slice(0, -1).reverse().map((h) => (
            <li key={h.slot}><span className="mono">{h.slot}</span><b className={REGIME_TONE[h.regime] || ''}>{h.regime}</b><span className="d-why" title={listOf(h.falsify).join('；')}>{listOf(h.falsify)[0] || '—'}</span></li>
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
  const [who, setWho] = useState('')
  const [open, setOpen] = useState(null)
  const people = useMemo(() => [...new Set((log || []).map((e) => e.who))], [log])
  const rows = (log || []).filter((e) => !who || e.who === who)
  return (
    <section className="d-card d-log">
      <header><h2>日志</h2><span className="mono d-dim">{rows.length} 条</span></header>
      {people.length > 1 && (
        <div className="d-filter" role="group" aria-label="按人筛选">
          {['', ...people].map((p) => <button key={p || 'all'} className={who === p ? 'is-on' : ''} onClick={() => { setWho(p); setOpen(null) }}>{p || '全部'}</button>)}
        </div>
      )}
      {!rows.length ? <p className="d-empty">今天还没有事件</p> : (
        <ol>
          {rows.map((e, i) => {
            const id = `${e.t}-${e.who}-${e.tag}-${i}`
            return (
              <li key={id} className={open === id ? 'is-open' : ''} onClick={() => setOpen(open === id ? null : id)} title={open === id ? '再点一次收起' : '点开看全文'}>
                <span className="mono d-dim">{e.t.slice(0, 5)}</span>
                <span className="d-who">{e.who}</span>
                <b className={`d-tag ${TAG_TONE[e.tag] || ''}`}>{e.tag}</b>
                <span className="d-text">{e.text}</span>
              </li>
            )
          })}
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
          <li className="head"><span>持仓</span><span>现价</span><span>今日</span><span>浮盈</span><span title="AI 自设止损和移动止盈线里高的那条">离卖出线</span></li>
          {a.positions.map((p) => (
            <li key={p.code}><b>{p.name}</b><span className="mono">{p.price}</span><span className={`mono ${tone(p.pct)}`}>{pct(p.pct)}</span>
              <span className={`mono ${tone(p.pnl)}`}>{pct(p.pnl, 1)}</span>
              <span className={`mono ${p.toStop != null && p.toStop > -2 ? 'near' : 'd-dim'}`} title={p.stop ? `会触发卖出的线 ${p.stop}${p.peak ? `｜买入后最高 ${p.peak}` : ''}${p.tiers ? `｜已分批卖 ${p.tiers} 档` : ''}` : '这只没有记到止损价'}>
                {p.toStop == null ? '—' : p.toStop >= 0 ? '已破' : `${p.toStop}%`}</span></li>
          ))}
        </ul>
      ) : <p className="d-empty small">空仓 · 现金 {money(a.cash)}</p>}
      {t.fills?.length > 0 && (
        <details className="d-more"><summary>今天成交 {t.fills.length} 笔</summary>
          <ul className="d-fills">
            {t.fills.map((f, i) => (
              <li key={`${f.t}-${f.code}-${i}`}><span className="mono d-dim">{f.t}</span><b className={f.side === 'buy' ? 'up' : 'down'}>{f.side === 'buy' ? '买' : '卖'}</b>
                <span>{f.name || f.code}</span><span className="mono">{f.price} × {f.shares}</span></li>
            ))}
          </ul>
        </details>
      )}
      <Funnel f={t.funnel} />
    </section>
  )
}

// 经理自己的一万元账户：和 dj 的实盘一样大、单票全仓。它的盈亏就是对经理的考核
function ManagerAccount({ m }) {
  if (!m) return null
  const a = m.account
  const p = a.positions?.[0]
  const last = [...(m.rounds || [])].reverse().find((r) => !r.skipped)
  const skipped = m.rounds?.length && !last ? m.rounds[m.rounds.length - 1].skipped : ''
  return (
    <section className="d-card d-mgr">
      <header><h2>经理账户</h2><span className="d-dim">一万元 · 单票全仓 · 经理自己操作</span></header>
      <div className="d-equity">
        <b className="mono">{Math.round(a.equity).toLocaleString('en-US')}</b>
        <span className={`mono ${tone(a.today)}`}>今日 {pct(a.today)}</span>
        <span className={`mono ${tone(a.total)}`}>累计 {pct(a.total, 1)}</span>
      </div>
      {p ? (
        <div className="d-hold">
          <b>{p.name}</b><span className="mono">{p.code}</span>
          <span className="mono">{p.shares} 股 @ {p.price}</span>
          <span className={`mono ${tone(p.pnl)}`}>浮盈 {pct(p.pnl, 1)}</span>
          <span className={`mono ${p.toStop != null && p.toStop > -2 ? 'near' : 'd-dim'}`}>
            {p.stop ? `止损 ${p.stop}（${p.toStop >= 0 ? '已破' : `还有 ${Math.abs(p.toStop)}%`}）` : '没有止损价'}</span>
          {p.buyDate === (m.today || '') && <span className="d-gate flat">今天买的，明天才能卖</span>}
        </div>
      ) : <p className="d-empty small">空仓 · 现金 {Math.round(a.cash).toLocaleString('en-US')} 元</p>}
      {last ? (
        <>
          <p className="d-read"><b className="mono">{last.slot}</b> {last.think || '—'}</p>
          {last.plan && <p className="d-sub">接下来：{last.plan}</p>}
          {last.dropped?.length > 0 && <p className="d-sub">没做成：{last.dropped.map((d) => `${d.action || ''}${d.code}（${d.why}）`).join('；')}</p>}
        </>
      ) : <p className="d-empty small">{skipped || '今天还没轮到他。09:26 / 10:30 / 13:20 / 14:35 各看一次，只从交易员给的票里挑。'}</p>}
      <Curve points={m.curve} />
      {m.fills?.length > 0 && (
        <ul className="d-fills">
          {m.fills.map((f, i) => (
            <li key={`${f.t}-${f.code}-${i}`}><span className="mono d-dim">{f.t}</span><b className={f.side === 'buy' ? 'up' : 'down'}>{f.side === 'buy' ? '买' : '卖'}</b>
              <span>{f.name || f.code}</span><span className="mono">{f.price} × {f.shares}</span></li>
          ))}
        </ul>
      )}
    </section>
  )
}

// 周复盘会：周五收盘后交易员各自复盘，经理总评并给自己定下周的风险档位
function Weekly({ stance, weekly }) {
  if (!stance) return null
  return (
    <section className="d-card d-weekly">
      <header><h2>周复盘 · 经理的风险档位</h2><span className="d-dim">{stance.week || '还没开过周会'}</span></header>
      <div className={`d-risk lv-${stance.level}`}><b>{stance.level}</b><span>{stance.why || '—'}</span></div>
      {stance.market && <p className="d-sub">对这一周市场的看法：{stance.market}</p>}
      {stance.rules?.length > 0 && <ol className="d-rules">{stance.rules.map((r, i) => <li key={i}>{r}</li>)}</ol>}
      {weekly?.history?.length > 1 && (
        <p className="d-sub">历次档位：{weekly.history.map((h) => `${(h.week || '').slice(5)} ${h.level}`).join(' → ')}</p>
      )}
      {weekly ? (
        <details className="d-more" open><summary>{weekly.week} 会议记录</summary>
          <p className="d-read">{weekly.summary}</p>
          {(weekly.stats?.mgr || Object.keys(weekly.stats?.traders || {}).length > 0) && (
            <table className="d-week">
              <thead><tr><th>这一周</th><th>涨跌</th><th>了结</th><th>赚</th><th>盈亏</th><th>最好</th><th>最差</th></tr></thead>
              <tbody>
                {[...Object.entries(weekly.stats.traders || {}), ...(weekly.stats.mgr ? [['mgr', { label: '经理账户', ...weekly.stats.mgr }]] : [])].map(([sid, t]) => (
                  <tr key={sid}><td>{t.label}</td><td className={`mono ${tone(t.weekPct)}`}>{pct(t.weekPct)}</td><td className="mono">{t.closed ?? '—'}</td>
                    <td className="mono">{t.wins ?? '—'}</td><td className={`mono ${tone(t.pnlYuan)}`}>{t.pnlYuan == null ? '—' : Math.round(t.pnlYuan).toLocaleString('en-US')}</td>
                    <td className="mono up">{t.best ? `${t.best.code} ${pct(t.best.pct, 1)}` : '—'}</td><td className="mono down">{t.worst ? `${t.worst.code} ${pct(t.worst.pct, 1)}` : '—'}</td></tr>
                ))}
              </tbody>
            </table>
          )}
          {weekly.stats?.companyWeek != null && <p className="d-sub">公司本周 {pct(weekly.stats.companyWeek)}（目标 +5%）{weekly.stats.regimes?.length > 0 && ` · 每天的定性：${weekly.stats.regimes.map((r) => `${(r.date || '').slice(5)} ${r.regime}`).join('、')}`}</p>}
          {Object.entries(weekly.statements || {}).map(([sid, s]) => (
            <div key={sid} className="d-said">
              <b>{s.label}</b>
              {s.error ? <p className="d-sub">没交复盘：{s.error}</p> : (
                <ul><li>做对：{s.good}</li><li>做错：{s.bad}</li><li>下周：{s.next}</li>
                  {weekly.traders?.[sid] && <li className="ask">经理要求：{weekly.traders[sid]}</li>}</ul>
              )}
            </div>
          ))}
        </details>
      ) : <p className="d-empty small">每周五收盘后开会：交易员各自复盘，经理总评并定下周的档位。默认按你的风格「中高」。</p>}
    </section>
  )
}

function StrategyDesk({ sd }) {
  if (!sd) {
    return (
      <section className="d-card d-sdesk">
        <h2>今天该做什么</h2>
        <p className="d-empty small">今天还没有汇总。09:26 / 10:30 / 13:20 各一次：规则放行候选 → 策略交易员否决 → 经理挑出「应该做」的票和仓位，同时推到你微信。</p>
      </section>
    )
  }
  const todo = (sd.final?.items || []).filter((i) => i.do)
  const rest = (sd.final?.items || []).filter((i) => !i.do)
  return (
    <section className="d-card d-sdesk">
      <h2>今天该做什么 <em className="mono">{sd.slot} 汇总</em></h2>
      <div className={`d-bignum ${todo.length ? 'up' : ''}`}><b className="mono">{todo.length}</b><span>只可做{todo.length ? `，合计仓位 ${Math.round(todo.reduce((n, i) => n + (i.pct || 0), 0) * 100)}%` : ''}</span></div>
      {sd.final?.read && <p className="d-read">{sd.final.read}</p>}
      {todo.length ? (
        <ul className="d-todo">
          {todo.map((i) => (
            <li key={i.code}>
              <b>{i.name}</b><span className="mono">{i.code}</span>
              <span className="d-gate pass">{sd.final.fallback ? '仓位未定' : `仓位 ${Math.round(i.pct * 100)}%`}</span>
              <small>{(i.strategies || []).join(' / ')}</small>
              <p>{i.why}<br /><em>看错的信号：{i.falsify || '—'}</em></p>
            </li>
          ))}
        </ul>
      ) : <p className="d-empty small">本时点没有应该做的票。</p>}
      {rest.length > 0 && <p className="d-sub">经理不做：{rest.map((i) => `${i.name}（${i.why}）`).join('；')}</p>}
      <ul className="d-strats">
        {sd.traders.map((t) => (
          <li key={t.key}>
            <b>{t.label}</b>
            <span className="mono">候选 {t.candidates} · 放行 {t.passed} · 荐 {t.picks.length} · 否 {t.vetoed.length}</span>
            {t.error && <span className="d-gate drop">{t.error}</span>}
            {t.rejected?.length > 0 && <small>规则没放行：{t.rejected.map((r) => `${r.name}（${r.why}）`).join('；')}</small>}
            {t.vetoed.length > 0 && <small>否掉：{t.vetoed.map((v) => `${v.name}（${v.why}）`).join('；')}</small>}
          </li>
        ))}
      </ul>
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
  // 轮流发言只在场的人里轮；有人刚成交就让他先说；办公室没人时不自动冒气泡
  const minute = data?.now ? data.now.slice(0, 16) : ''
  const order = useMemo(() => {
    const here = sceneOf(minute ? `${minute}:00` : null).awake
    return ['manager', ...SEATS.map((s) => s.role)].filter((r) => here.includes(r))
  }, [minute])
  const event = liveEvent(data)
  const active = picked || event?.role || order[beat % (order.length || 1)] || null
  const phase = phaseOf(data?.now)
  const pick = (r) => setPicked(picked === r ? null : r)
  const fresh = at && Date.now() - at < 6000 && !error
  const m = data?.manager
  // 两个账户合计的今日盈亏：用各自的净值和今日涨幅倒推昨天的净值再相加
  const accts = (data?.traders || []).map((t) => t.account).filter((a) => a && a.today != null)
  const eqNow = accts.reduce((n, a) => n + a.equity, 0)
  const eqPrev = accts.reduce((n, a) => n + a.equity / (1 + a.today / 100), 0)
  const dayAll = accts.length ? (eqNow / eqPrev - 1) * 100 : null
  return (
    <div className="desk">
      <header className="d-top">
        <span className="d-brand">衡策<em>TRADING DESK</em></span>
        <span className={`d-phase ${phase === '盘中' ? 'on' : ''}`}>{phase}</span>
        <span className="mono d-clock">{data?.now ? data.now.slice(11, 19) : '--:--:--'}</span>
        {m && <span className={`d-chip ${REGIME_TONE[m.regime] || ''}`}>{m.regime}</span>}
        {data?.stance && <span className="d-chip ghost">风险档位 <b>{data.stance.level}</b></span>}
        {dayAll != null && (
          <span className="d-chip ghost">两个账户合计 <b className={`mono ${tone(dayAll)}`}>{pct(dayAll)}</b> <b className={`mono ${tone(dayAll)}`}>{eqNow - eqPrev > 0 ? '+' : ''}{Math.round(eqNow - eqPrev).toLocaleString('en-US')} 元</b></span>
        )}
        <span className="d-chip ghost">今日调用 DeepSeek <b className="mono">{data?.calls ?? 0}</b> 次</span>
        <span className={`d-live ${fresh ? 'ok' : 'bad'}`}>{fresh ? '实时' : error || '连接中'}</span>
        <nav>
          <button onClick={() => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.())}>全屏</button>
          <button onClick={onTerm}>盘面</button>
          <button onClick={onDetail}>详细记录</button>
        </nav>
      </header>
      {/* 目标放最上面（dj 2026-11-11），下面紧跟整屏办公室，数据往下滚 */}
      <Company c={data?.company} />
      <Company c={data?.mgr?.score} title="经理账户目标" sub="经理自己操作 · 一万元单票全仓" />
      <div className="d-office-wrap"><Office data={data} active={active} picked={!!picked} event={event} phase={phase} onPick={pick} /></div>
      <main className="d-grid">
        <div className="d-left">
          <RoleCards data={data} active={active} onPick={pick} />
          <div className="d-traders">
            {(data?.traders || []).map((t) => <TraderCard key={t.sid} t={t} />)}
          </div>
        </div>
        <aside className="d-right">
          <ManagerAccount m={data?.mgr && { ...data.mgr, today: data.now?.slice(0, 10) }} />
          <StrategyDesk sd={data?.strategyDesk} />
          <Weekly stance={data?.stance} weekly={data?.weekly} />
          <Analysis data={data} />
          <Log log={data?.log} />
          <Params params={data?.params} />
        </aside>
      </main>
    </div>
  )
}
