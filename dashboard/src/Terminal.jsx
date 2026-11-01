import { useEffect, useMemo, useState } from 'react'
import { ArrowRightLeft, Bot, Cable, CircleDot, Gauge, Radio } from 'lucide-react'

const SIDE = { buy: '买', sell: '卖' }

function useTerminal() {
  const [state, setState] = useState({ data: null, error: '', at: 0 })
  useEffect(() => {
    let stopped = false
    let controller = null
    async function tick() {
      if (document.hidden) return
      controller?.abort()
      controller = new AbortController()
      try {
        const res = await fetch('/api/terminal', { signal: controller.signal, cache: 'no-store' })
        if (!res.ok) throw new Error(`接口 ${res.status}`)
        const data = await res.json()
        if (!stopped) setState({ data, error: '', at: Date.now() })
      } catch (e) {
        if (e.name !== 'AbortError' && !stopped) setState((s) => ({ ...s, error: e.message }))
      }
    }
    tick()
    const timer = setInterval(tick, 1000)
    const onVis = () => { if (!document.hidden) tick() }
    document.addEventListener('visibilitychange', onVis)
    return () => { stopped = true; controller?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', onVis) }
  }, [])
  return state
}

function useMarkets() {
  const [markets, setMarkets] = useState([])
  useEffect(() => {
    let stopped = false
    async function tick() {
      if (document.hidden) return
      try {
        const res = await fetch('/api/dashboard', { cache: 'no-store' })
        if (res.ok) {
          const d = await res.json()
          if (!stopped) setMarkets(d.markets || [])
        }
      } catch { /* 指数取不到时保留上一笔 */ }
    }
    tick()
    const t = setInterval(tick, 2000)
    return () => { stopped = true; clearInterval(t) }
  }, [])
  return markets
}

const tone = (v) => (v == null || Number.isNaN(v) || Math.abs(v) < 0.005 ? 'flat' : v > 0 ? 'up' : 'down')
const pct = (v, digits = 2) => (v == null || Number.isNaN(v) ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(digits)}%`)
const num = (v, digits = 2) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }))
const int = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN'))

function Clock({ iso }) {
  const t = iso ? iso.slice(11, 19) : '--:--:--'
  return <span className="t-clock">{t}<em>北京</em></span>
}

function phaseOf(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  const day = d.getDay()
  if (day === 0 || day === 6) return '休市'
  const m = d.getHours() * 60 + d.getMinutes()
  if (m < 570) return '盘前'
  if (m < 690) return '盘中'
  if (m < 780) return '午休'
  if (m < 900) return '盘中'
  return '收盘'
}

function IndexTape({ markets }) {
  if (!markets.length) return <div className="t-tape"><span className="t-dim">指数载入中</span></div>
  return (
    <div className="t-tape">
      {markets.map((m) => {
        const v = parseFloat(m.change)
        return (
          <div className="t-index" key={m.name}>
            <span className="t-index-name">{m.name.replace('指数', '').replace('成指', '成指')}</span>
            <b className={tone(v)}>{m.value}</b>
            <span className={tone(v)}>{m.change}</span>
          </div>
        )
      })}
    </div>
  )
}

function Breadth({ market }) {
  if (!market) return null
  const total = (market.up || 0) + (market.down || 0)
  const upShare = total ? (market.up / total) * 100 : 50
  const weak = market.ret5 != null && market.ret5 <= -2
  return (
    <section className="t-breadth">
      <div className="t-breadth-bar" aria-label={`上涨 ${market.up} 家，下跌 ${market.down} 家`}>
        <i className="up" style={{ width: `${upShare}%` }} />
        <i className="down" style={{ width: `${100 - upShare}%` }} />
      </div>
      <dl className="t-stats">
        <div><dt>涨</dt><dd className="up">{int(market.up)}</dd></div>
        <div><dt>跌</dt><dd className="down">{int(market.down)}</dd></div>
        <div><dt>涨停</dt><dd className="up">{int(market.limitUp)}</dd></div>
        <div><dt>跌停</dt><dd className="down">{int(market.limitDown)}</dd></div>
        <div><dt>5日中位</dt><dd className={tone(market.ret5)}>{pct(market.ret5)}</dd></div>
        <div><dt>情绪</dt><dd className={market.sentiment === '火热' || market.sentiment === '正常' ? 'up' : market.sentiment ? 'down' : 'flat'}>{market.sentiment || '—'}</dd></div>
        <div className="t-regime"><dt>大盘</dt><dd className={weak ? 'down' : 'flat'}>{market.ret5 == null ? '—' : weak ? '普跌' : '不弱'}</dd></div>
      </dl>
      <span className="t-asof">{market.live ? '实时' : market.asOf ? `收盘 ${market.asOf.slice(5)}` : ''}</span>
    </section>
  )
}

function Curve({ points, equity }) {
  const series = useMemo(() => {
    const vals = (points || []).map((p) => p.v)
    if (equity != null) vals.push(equity)
    return vals
  }, [points, equity])
  if (series.length < 2) return <span className="t-dim">—</span>
  const min = Math.min(...series)
  const max = Math.max(...series)
  const span = max - min || 1
  const w = 120
  const h = 28
  const d = series.map((v, i) => `${i === 0 ? 'M' : 'L'}${((i / (series.length - 1)) * w).toFixed(1)},${(h - ((v - min) / span) * (h - 4) - 2).toFixed(1)}`).join(' ')
  const trend = tone(series[series.length - 1] - series[0])
  return <svg className={`t-curve ${trend}`} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true"><path d={d} /></svg>
}

function Accounts({ accounts, curves, showShadow }) {
  const rows = accounts.filter((a) => showShadow || a.main)
  return (
    <section className="t-panel t-accounts">
      <header><h2>账户</h2></header>
      <table>
        <thead>
          <tr><th>账户</th><th className="r">净值</th><th className="r">今日</th><th className="r">累计</th><th className="c">30日</th><th>持仓</th></tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id} className={a.live ? 'is-live' : ''}>
              <td className="t-acct">{a.live && <Radio size={13} aria-label="实盘跟这个账户" />}{a.name}</td>
              <td className="r mono">{num(a.equity, 0)}</td>
              <td className={`r mono ${tone(a.today)}`}>{pct(a.today)}</td>
              <td className={`r mono ${tone(a.total)}`}>{pct(a.total, 1)}</td>
              <td className="c"><Curve points={curves[a.id]} equity={a.equity} /></td>
              <td className="t-holds">
                {a.positions.length === 0 ? <span className="t-dim">空仓</span> : a.positions.map((p) => (
                  <span className="t-hold" key={p.code}>
                    <b>{p.name}</b>
                    <span className="mono">{num(p.price)}</span>
                    <span className={`mono ${tone(p.pct)}`}>{pct(p.pct)}</span>
                    <span className={`mono t-pnl ${tone(p.pnl)}`}>盈 {pct(p.pnl, 1)}</span>
                  </span>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function Fills({ fills }) {
  return (
    <section className="t-panel t-fills">
      <header><h2>今日成交</h2><span className="t-dim">{fills.length ? `${fills.length} 笔` : ''}</span></header>
      {fills.length === 0 ? <p className="t-empty">今天还没有成交</p> : (
        <ol>
          {fills.map((f, i) => (
            <li key={`${f.t}-${f.code}-${f.account}-${i}`} className={f.live ? 'is-live' : ''}>
              <span className="mono t-time">{f.t}</span>
              <span className={`t-side ${f.side === 'buy' ? 'up' : 'down'}`}>{SIDE[f.side]}</span>
              <b>{f.name}</b>
              <span className="mono">{num(f.price)}</span>
              <span className="t-dim t-why">{f.account} · {f.reason}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function Status({ data, error, at }) {
  const live = data?.live
  const ai = data?.ai
  const gw = data?.gateway
  const fresh = at && Date.now() - at < 5000 && !error
  const loop = ai?.loop
  const items = [
    { icon: CircleDot, name: '数据', ok: fresh, text: fresh ? '每秒刷新' : error || '连不上面板服务' },
    { icon: Cable, name: '网关', ok: gw?.ok, text: gw?.text || '—' },
    { icon: ArrowRightLeft, name: '实盘', ok: live && !live.paused && live.mode === 'live', text: !live ? '—' : live.paused ? `暂停 · ${live.reason || ''}` : live.mode },
    { icon: Bot, name: 'AI', ok: loop ? loop.ok : null, text: !loop ? '循环没在跑' : loop.called ? `${loop.ts.slice(11, 16)} 判断 · ${loop.seconds}s` : loop.error ? `${loop.ts.slice(11, 16)} 出错` : `${loop.ts.slice(11, 16)} ${loop.skipped || '跳过'}` },
  ]
  return (
    <section className="t-panel t-status">
      <header><h2>系统</h2></header>
      <ul>
        {items.map(({ icon: Icon, name, ok, text }) => (
          <li key={name} className={ok === true ? 'ok' : ok === false ? 'bad' : 'idle'}>
            <Icon size={14} aria-hidden="true" /><span>{name}</span><em title={text}>{text}</em>
          </li>
        ))}
      </ul>
      {ai?.decision && (
        <p className="t-ai"><Gauge size={14} aria-hidden="true" /><span>{ai.decision.time}</span><b>{ai.decision.action || ai.decision.title}</b></p>
      )}
    </section>
  )
}

export default function Terminal({ onDetail }) {
  const { data, error, at } = useTerminal()
  const markets = useMarkets()
  const [showShadow, setShowShadow] = useState(false)
  const phase = phaseOf(data?.now)
  return (
    <div className="term">
      <header className="t-top">
        <span className="t-brand">衡策</span>
        <span className={`t-phase ${phase === '盘中' ? 'on' : ''}`}>{phase}</span>
        <Clock iso={data?.now} />
        <IndexTape markets={markets} />
        <button className="t-link" onClick={onDetail}>详细记录</button>
      </header>
      <Breadth market={data?.market} />
      <main className="t-grid">
        <div className="t-left">
          <Accounts accounts={data?.accounts || []} curves={data?.curves || {}} showShadow={showShadow} />
          <button className="t-toggle" onClick={() => setShowShadow((v) => !v)}>{showShadow ? '收起影子账户' : '显示影子账户'}</button>
        </div>
        <div className="t-right">
          <Status data={data} error={error} at={at} />
          <Fills fills={data?.fills || []} />
        </div>
      </main>
    </div>
  )
}
