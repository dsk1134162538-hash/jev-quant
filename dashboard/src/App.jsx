import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowDownRight, ArrowUpRight, Bell, BookOpen, BrainCircuit,
  CalendarDays, CheckCircle2, ChevronRight, Circle, CircleAlert, CirclePause,
  Clock3, Database, Download, Eye, FileText, FlaskConical, LayoutDashboard,
  LineChart, ListChecks, Menu, Search, Server, ShieldCheck, SlidersHorizontal,
  WalletCards, WifiOff, X,
} from 'lucide-react'
import { decisions, experiments, marketSeries, markets, reviews, sectors, systemItems } from './data/demo.js'

const navItems = [
  { id: 'overview', label: '今日总览', icon: LayoutDashboard },
  { id: 'decisions', label: '决策记录', icon: ListChecks, count: 3 },
  { id: 'reviews', label: '复盘时间线', icon: Clock3 },
  { id: 'research', label: '策略验证', icon: FlaskConical },
  { id: 'system', label: '系统状态', icon: Server },
]

const pageNames = Object.fromEntries(navItems.map((item) => [item.id, item.label]))

function Badge({ children, tone = 'neutral', dot = false }) {
  return <span className={`badge badge-${tone}`}>{dot && <i />} {children}</span>
}

function CardHeader({ eyebrow, title, aside, children }) {
  return (
    <div className="card-header">
      <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h2>{title}</h2>{children}</div>
      {aside}
    </div>
  )
}

function MarketChart({ market }) {
  const values = marketSeries[market]
  const min = Math.min(...values) - 8
  const max = Math.max(...values) + 8
  const points = values.map((value, index) => {
    const x = 16 + index * (568 / (values.length - 1))
    const y = 168 - ((value - min) / (max - min)) * 130
    return [x, y]
  })
  const path = points.map(([x, y], index) => `${index ? 'L' : 'M'} ${x} ${y}`).join(' ')
  const area = `${path} L ${points.at(-1)[0]} 190 L ${points[0][0]} 190 Z`
  const bars = [44, 62, 51, 78, 57, 69, 48, 55]
  return (
    <div className="chart-wrap">
      <div className="chart-ylabels"><span>{max.toFixed(0)}</span><span>{((max + min) / 2).toFixed(0)}</span><span>{min.toFixed(0)}</span></div>
      <svg className="market-chart" viewBox="0 0 600 238" role="img" aria-label={`${market}演示走势`}>
        {[38, 103, 168].map((y) => <line key={y} x1="16" x2="584" y1={y} y2={y} className="grid-line" />)}
        {bars.map((height, index) => <rect key={index} x={10 + index * 81.1} y={230 - height / 3} width="28" height={height / 3} rx="3" className="volume-bar" />)}
        <path d={area} className="chart-area" />
        <path d={path} className="chart-line" />
        {points.map(([x, y], index) => index === points.length - 1 && <g key={x}><circle cx={x} cy={y} r="8" className="chart-dot-halo" /><circle cx={x} cy={y} r="3.5" className="chart-dot" /></g>)}
      </svg>
      <div className="chart-xlabels"><span>10/20</span><span>10/22</span><span>10/24</span><span>10/27</span></div>
    </div>
  )
}

function AccountCard() {
  return (
    <section className="account-card dark-card">
      <div className="account-top"><span className="eyebrow">实验账户 · AI EXPERIMENT</span><Badge tone="dark" dot>DRYRUN</Badge></div>
      <div className="account-value"><span>总资产</span><strong><small>¥</small> 10,000.00</strong></div>
      <div className="account-metrics">
        <div><span>可用资金</span><b>10,000.00</b></div>
        <div><span>当前仓位</span><b>0%</b></div>
        <div><span>今日盈亏</span><b>0.00</b></div>
      </div>
      <div className="account-footer"><ShieldCheck size={15} /><span>空仓也是策略 · 不为收益目标强制交易</span></div>
    </section>
  )
}

function StrategyCard({ onOpen }) {
  return (
    <section className="strategy-card card">
      <CardHeader eyebrow="TODAY'S PLAYBOOK" title="今日策略" aside={<Badge tone="amber">盘前假设</Badge>} />
      <div className="strategy-main">
        <div className="strategy-icon"><Eye size={24} /></div>
        <div><h3>观望，等待确认</h3><p>指数未企稳前，不把“今天必须赚钱”当作买入理由。</p></div>
      </div>
      <div className="condition-list">
        <div><span>01</span><p>指数回踩后不再创新低，出现可验证的承接。</p></div>
        <div><span>02</span><p>候选板块持续强于指数，且不是单股脉冲。</p></div>
        <div><span>03</span><p>个股重新站上均价，失效位与次日退出明确。</p></div>
      </div>
      <button className="text-button" onClick={onOpen}>查看完整决策依据 <ArrowUpRight size={16} /></button>
    </section>
  )
}

function Overview({ onNavigate, onDecision }) {
  const [market, setMarket] = useState('上证指数')
  return (
    <>
      <div className="page-heading">
        <div><span className="date-line"><CalendarDays size={15} /> 2026 年 10 月 27 日 · 周二</span><h1>早上好，Jamo</h1><p>先验证，再交易。今天没有必须出手的任务。</p></div>
        <button className="button button-secondary" onClick={() => onNavigate('reviews')}><BookOpen size={17} /> 查看今日复盘</button>
      </div>
      <div className="hero-grid"><StrategyCard onOpen={() => onDecision(decisions[0])} /><AccountCard /></div>

      <div className="market-strip">
        {markets.map((item) => <div className="market-quote" key={item.name}><span>{item.name}</span><strong>{item.value}</strong><em className={item.direction}>{item.change}</em><small>{item.breadth}</small></div>)}
        <div className="market-regime"><Activity size={18} /><div><span>市场状态</span><strong>弱修复 · 未确认</strong></div></div>
      </div>

      <div className="content-grid two-one">
        <section className="card market-panel">
          <CardHeader eyebrow="MARKET REGIME" title="大盘研判" aside={<div className="segmented small">{Object.keys(marketSeries).map((name) => <button key={name} className={market === name ? 'active' : ''} onClick={() => setMarket(name)}>{name.replace('指数', '')}</button>)}</div>} />
          <div className="market-summary"><div><LineChart size={17} /><span>趋势</span><b>低点仍待抬高</b></div><div><Activity size={17} /><span>量能</span><b>缩量修复</b></div><div><SlidersHorizontal size={17} /><span>市场宽度</span><b>38%</b></div></div>
          <MarketChart market={market} />
          <div className="chart-caption"><span><i className="legend-line" /> 指数走势</span><span><i className="legend-bar" /> 相对成交额</span><em>仅展示界面演示数据</em></div>
        </section>
        <section className="card evidence-panel">
          <CardHeader eyebrow="EVIDENCE" title="判断依据" />
          <div className="evidence-score"><div className="score-ring"><strong>42</strong><span>/ 100</span></div><div><b>证据不足</b><p>未达到主动出手条件</p></div></div>
          <div className="evidence-list">
            <div><span>指数趋势</span><b className="negative">偏弱</b></div>
            <div><span>板块持续</span><b className="amber-text">待确认</b></div>
            <div><span>成交配合</span><b className="negative">不足</b></div>
            <div><span>交易赔率</span><b>待计算</b></div>
          </div>
          <div className="note-box"><CircleAlert size={17} /><p>评分只是界面示例，不是已验证的买卖信号。</p></div>
        </section>
      </div>

      <div className="content-grid two-one lower-grid">
        <section className="card sector-panel">
          <CardHeader eyebrow="SECTOR STRENGTH" title="板块强度与持续性" />
          <div className="table-scroll"><table><thead><tr><th>板块</th><th>近 5 日</th><th>当日</th><th>量能</th><th>持续性</th><th>当前结论</th></tr></thead><tbody>{sectors.map((sector) => <tr key={sector.name}><td><b>{sector.name}</b></td><td className={sector.fiveDay >= 0 ? 'positive' : 'negative'}>{sector.fiveDay > 0 ? '+' : ''}{sector.fiveDay}%</td><td className="positive">+{sector.today}%</td><td>{sector.volume}</td><td>{sector.continuity}</td><td><Badge tone={sector.tone}>{sector.verdict}</Badge></td></tr>)}</tbody></table></div>
        </section>
        <section className="card timeline-panel">
          <CardHeader eyebrow="REVIEW RHYTHM" title="今日复盘" aside={<button className="text-link" onClick={() => onNavigate('reviews')}>全部 <ChevronRight size={14} /></button>} />
          <div className="mini-timeline">
            {decisions.map((decision, index) => <button key={decision.id} onClick={() => onDecision(decision)}><span className={`timeline-marker ${index === 0 ? 'current' : ''}`}>{index === 0 ? <CheckCircle2 size={15} /> : <Circle size={13} />}</span><span className="timeline-time">{decision.time}</span><span><b>{decision.type}</b><small>{decision.title}</small></span><ChevronRight size={15} /></button>)}
          </div>
        </section>
      </div>
    </>
  )
}

function DecisionsPage({ onDecision }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('全部')
  const filtered = useMemo(() => decisions.filter((decision) => {
    const matchesQuery = `${decision.title}${decision.type}${decision.action}`.includes(query)
    return matchesQuery && (filter === '全部' || decision.action === filter)
  }), [query, filter])
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">DECISION JOURNAL</span><h1>每一次判断，都留下依据</h1><p>事实、推断和行动分开记录，不在收盘后改写当时的理由。</p></div><Badge tone="green">3 条演示记录</Badge></div>
      <div className="toolbar card"><label className="search-box"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索决策、动作或类型" /></label><div className="segmented">{['全部', '观望', '等待', '复盘'].map((name) => <button className={filter === name ? 'active' : ''} key={name} onClick={() => setFilter(name)}>{name}</button>)}</div></div>
      <div className="decision-list">
        {filtered.map((decision) => <button className="decision-row card" key={decision.id} onClick={() => onDecision(decision)}><span className="decision-time">{decision.time}<small>{decision.type}</small></span><span className="decision-content"><span className="decision-id">{decision.id}</span><strong>{decision.title}</strong><p>{decision.market}</p><span className="decision-tags"><Badge tone={decision.action === '复盘' ? 'neutral' : 'amber'}>{decision.action}</Badge><Badge>风险 · {decision.risk}</Badge><Badge>{decision.status}</Badge></span></span><span className="decision-open"><ChevronRight size={20} /></span></button>)}
        {filtered.length === 0 && <div className="empty-state card"><Search size={28} /><h3>没有匹配的决策</h3><p>试试清空搜索或切换动作筛选。</p></div>}
      </div>
    </>
  )
}

function ReviewsPage({ onExport }) {
  const [tab, setTab] = useState('盘前')
  const review = reviews[tab]
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">MORNING · MIDDAY · CLOSE</span><h1>复盘决策质量，而非只看盈亏</h1><p>当时的判断不可事后覆盖，新发现只能追加。</p></div><button className="button button-primary" onClick={() => onExport(tab)}><Download size={17} /> 导出{tab}复盘</button></div>
      <div className="review-layout">
        <aside className="review-nav card">{Object.keys(reviews).map((name, index) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}><span>0{index + 1}</span><div><b>{name}复盘</b><small>{reviews[name].label.split(' · ')[0]}</small></div><ChevronRight size={16} /></button>)}<div className="review-nav-note"><FileText size={18} /><p>真实接入后，每次复盘将关联当时的行情、决策与委托快照。</p></div></aside>
        <section className="review-document card">
          <div className="document-head"><Badge tone="green">{review.label}</Badge><span>演示稿件</span></div><h2>{review.title}</h2>
          <ReviewSection number="01" title="事实 · 当时知道什么" items={review.facts} />
          <ReviewSection number="02" title="推断 · 我如何理解" items={review.inferences} />
          <ReviewSection number="03" title="行动 · 接下来怎么做" items={review.actions} />
          <div className="review-principle"><BrainCircuit size={22} /><div><b>复盘原则</b><p>盈利的交易也可能来自坏决策，亏损的交易也可能是合理风控。</p></div></div>
        </section>
      </div>
    </>
  )
}

function ReviewSection({ number, title, items }) {
  return <div className="review-section"><div className="review-section-title"><span>{number}</span><h3>{title}</h3></div><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></div>
}

function ResearchPage() {
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">STRATEGY LAB</span><h1>先证伪，再考虑上线</h1><p>假设、样本和基线公开，避免把事后解释当成策略能力。</p></div><Badge tone="amber">INSUFFICIENT DATA</Badge></div>
      <div className="research-banner dark-card"><FlaskConical size={28} /><div><h3>当前没有通过验证的 AI 策略</h3><p>工程链路测试通过，不代表低吸、主线识别或任何交易规则有效。</p></div><div className="research-stat"><strong>0</strong><span>有效样本</span></div></div>
      <div className="experiment-grid">{experiments.map((experiment) => <section className="card experiment-card" key={experiment.version}><div className="experiment-top"><Badge>{experiment.version}</Badge><Badge tone="amber">{experiment.status}</Badge></div><h2>{experiment.name}</h2><p>{experiment.description}</p><div className="experiment-meta"><div><span>有效样本</span><b>{experiment.samples}</b></div><div><span>对照基线</span><b>{experiment.baseline}</b></div></div><div className="progress-track"><i style={{ width: '3%' }} /></div><small>等待足够的全市场历史数据与时间隔离验证</small></section>)}</div>
      <section className="card validation-rules"><CardHeader eyebrow="VALIDATION CONTRACT" title="进入下一阶段的必要条件" /><div className="validation-grid">{['严格按时间划分训练与验证区间', '扣除最低佣金、滑点并遵守 T+1', '与当日等权和空仓基线同时比较', '保留失败样本，不只展示盈利案例'].map((rule, index) => <div key={rule}><span>0{index + 1}</span><p>{rule}</p></div>)}</div></section>
    </>
  )
}

function SystemPage() {
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">EXECUTION & SAFETY</span><h1>先确认系统知道自己在做什么</h1><p>面板只读展示；真实交易只能经过执行器，不由页面绕过。</p></div><Badge tone="green">真实委托未启用</Badge></div>
      <div className="system-grid">{systemItems.map((item) => <section className="card system-card" key={item.name}><div className="system-icon">{item.name === '交易网关' ? <WifiOff size={21} /> : item.name === '行情与数据库' ? <Database size={21} /> : <ShieldCheck size={21} />}</div><div><span>{item.name}</span><strong>{item.value}</strong><p>{item.note}</p></div><i className={`status-dot ${item.state}`} /></section>)}</div>
      <div className="content-grid two-one">
        <section className="card"><CardHeader eyebrow="HARD GUARDS" title="AI 资金安全边界" /><div className="guard-list">{['非 dryrun：AI 买卖均被硬阻断', '单笔资金：券商可用、一万元与 AI 决策预算取最小', '持仓映射：AI 与旧策略账户分离', '读取异常：连续失败后自动暂停', '意图桥接：游标与写入同事务，避免重复'].map((guard) => <div key={guard}><CheckCircle2 size={17} /><span>{guard}</span></div>)}</div></section>
        <section className="card next-check"><CardHeader eyebrow="NEXT CHECK" title="家里电脑待验证" /><ol><li><span>1</span>网关健康与登录状态</li><li><span>2</span>资金、持仓只读接口</li><li><span>3</span>自动验证码现场表现</li><li><span>4</span>真实数据库 dryrun 链路</li></ol><div className="note-box"><CirclePause size={17} /><p>完成这些检查，也不等于自动开启实盘。</p></div></section>
      </div>
    </>
  )
}

function DecisionModal({ decision, onClose }) {
  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  if (!decision) return null
  const sections = [['大盘判断', decision.market], ['板块判断', decision.sector], ['个股与执行', decision.stock], ['触发条件', decision.trigger], ['失效条件', decision.invalidation]]
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className="decision-modal" role="dialog" aria-modal="true" aria-label="决策详情"><div className="modal-head"><div><span className="eyebrow">{decision.id}</span><h2>{decision.title}</h2><div className="decision-tags"><Badge tone="amber">{decision.action}</Badge><Badge>{decision.time} · {decision.type}</Badge><Badge>风险 · {decision.risk}</Badge></div></div><button className="icon-button" onClick={onClose} aria-label="关闭决策详情"><X size={20} /></button></div><div className="modal-body">{sections.map(([title, content]) => <div className="modal-section" key={title}><h3>{title}</h3><p>{content}</p></div>)}</div><div className="modal-footer"><ShieldCheck size={16} /><span>演示记录，不构成真实委托或收益承诺</span></div></section></div>
}

export default function App() {
  const [active, setActive] = useState('overview')
  const [mobileOpen, setMobileOpen] = useState(false)
  const [selectedDecision, setSelectedDecision] = useState(null)
  const [toast, setToast] = useState('')

  const navigate = (page) => {
    setActive(page)
    setMobileOpen(false)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const exportReview = (tab) => {
    const review = reviews[tab]
    const text = [`# ${review.label}`, '', `## ${review.title}`, '', '## 事实', ...review.facts.map((x) => `- ${x}`), '', '## 推断', ...review.inferences.map((x) => `- ${x}`), '', '## 行动', ...review.actions.map((x) => `- ${x}`), '', '> 演示稿件，未接入真实行情。'].join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `AI-${tab}复盘-2026-10-27.md`
    link.click()
    URL.revokeObjectURL(url)
    setToast(`${tab}复盘已导出`)
    window.setTimeout(() => setToast(''), 2400)
  }

  return (
    <div className="app-shell">
      {mobileOpen && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}>
        <div className="brand"><div className="brand-mark">衡</div><div><strong>衡策</strong><span>AI 量化研究室</span></div></div>
        <div className="workspace-label">工作空间</div>
        <nav>{navItems.map((item) => <button key={item.id} className={active === item.id ? 'active' : ''} onClick={() => navigate(item.id)}><item.icon size={18} /><span>{item.label}</span>{item.count && <em>{item.count}</em>}</button>)}</nav>
        <div className="sidebar-bottom"><div className="safety-card"><ShieldCheck size={20} /><div><b>安全边界运行中</b><span>AI 仅限 DRYRUN</span></div></div><div className="profile"><div className="avatar">J</div><div><b>Jamo</b><span>实验账户 · ¥10,000</span></div><ChevronRight size={15} /></div></div>
      </aside>
      <main className="main-area">
        <header className="topbar"><button className="mobile-menu icon-button" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Menu size={20} /></button><div className="breadcrumb"><span>AI 实验</span><ChevronRight size={14} /><b>{pageNames[active]}</b></div><div className="topbar-actions"><Badge tone="amber" dot>演示数据</Badge><span className="market-closed">盘前 · 未接行情</span></div></header>
        <div className="demo-notice"><CircleAlert size={16} /><span><b>首版界面预览</b> · 所有行情、评分与复盘内容均为演示，尚未接入家里交易电脑。</span></div>
        <div className="page-content">
          {active === 'overview' && <Overview onNavigate={navigate} onDecision={setSelectedDecision} />}
          {active === 'decisions' && <DecisionsPage onDecision={setSelectedDecision} />}
          {active === 'reviews' && <ReviewsPage onExport={exportReview} />}
          {active === 'research' && <ResearchPage />}
          {active === 'system' && <SystemPage />}
          <footer><span>衡策 · AI 量化研究室</span><span>先验证，再交易 · v0.1.0</span></footer>
        </div>
      </main>
      {selectedDecision && <DecisionModal decision={selectedDecision} onClose={() => setSelectedDecision(null)} />}
      {toast && <div className="toast"><CheckCircle2 size={17} />{toast}</div>}
    </div>
  )
}
