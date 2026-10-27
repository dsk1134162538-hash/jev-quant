import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowUpRight, BookOpen, BrainCircuit, CalendarDays, CheckCircle2,
  ChevronRight, Circle, CircleAlert, CirclePause, Clock3, Database, Download,
  Eye, FileText, FlaskConical, LayoutDashboard, LineChart, ListChecks, Menu,
  Search, Server, ShieldCheck, SlidersHorizontal, WifiOff, X,
} from 'lucide-react'
import { decisions, experiments, markets, observations, reviews, snapshot, systemItems } from './data/realSnapshot.js'

const navItems = [
  { id: 'overview', label: '今日总览', icon: LayoutDashboard },
  { id: 'decisions', label: '决策记录', icon: ListChecks, count: decisions.length },
  { id: 'reviews', label: '复盘时间线', icon: Clock3 },
  { id: 'research', label: '策略验证', icon: FlaskConical },
  { id: 'system', label: '系统状态', icon: Server },
]
const pageNames = Object.fromEntries(navItems.map((item) => [item.id, item.label]))

function Badge({ children, tone = 'neutral', dot = false }) {
  return <span className={`badge badge-${tone}`}>{dot && <i />} {children}</span>
}

function CardHeader({ eyebrow, title, aside }) {
  return <div className="card-header"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h2>{title}</h2></div>{aside}</div>
}

function AccountCard() {
  return (
    <section className="account-card dark-card">
      <div className="account-top"><span className="eyebrow">实验账户 · AI EXPERIMENT</span><Badge tone="dark" dot>PAPER</Badge></div>
      <div className="account-value"><span>总资产 · 空仓现金口径</span><strong><small>¥</small> 10,000.00</strong></div>
      <div className="account-metrics"><div><span>可用资金</span><b>10,000.00</b></div><div><span>当前仓位</span><b>0%</b></div><div><span>今日盈亏</span><b>0.00</b></div></div>
      <div className="account-footer"><ShieldCheck size={15} /><span>liveTrading=false · 本次无模拟成交</span></div>
    </section>
  )
}

function StrategyCard({ onOpen }) {
  return (
    <section className="strategy-card card">
      <CardHeader eyebrow="TODAY'S PLAYBOOK" title="今日策略" aside={<Badge tone="amber">09:53 盘中决策</Badge>} />
      <div className="strategy-main"><div className="strategy-icon"><Eye size={24} /></div><div><h3>空仓防守，不抄样本补跌</h3><p>三只机器人观察样本全部下跌，今日指数与市场宽度又缺少有效数据。</p></div></div>
      <div className="condition-list">
        <div><span>01</span><p>当前无合格候选，不生成 quote.json，不调用 buy 或 sell。</p></div>
        <div><span>02</span><p>指数停止创新低、板块多股共振后，才评估小仓试错。</p></div>
        <div><span>03</span><p>买入前核验新鲜 Quote、100 股、费用、T+1 与失效位。</p></div>
      </div>
      <button className="text-button" onClick={onOpen}>查看完整决策依据 <ArrowUpRight size={16} /></button>
    </section>
  )
}

function MarketEvidence() {
  return (
    <section className="card market-panel">
      <CardHeader eyebrow="MARKET REGIME" title="大盘证据状态" aside={<Badge tone="amber">数据不完整</Badge>} />
      <div className="market-summary"><div><LineChart size={17} /><span>趋势</span><b>下跌趋势 · 暂沿用</b></div><div><Activity size={17} /><span>成交额</span><b>今日缺失</b></div><div><SlidersHorizontal size={17} /><span>市场宽度</span><b>今日缺失</b></div></div>
      <div className="data-gap"><Database size={28} /><h3>今日主要指数返回 EMPTY_DATA</h3><p>不能用三只股票的下跌替代大盘判断，也不能把 10 月 23 日历史收盘伪装成今日行情。</p><div className="data-gap-meta"><span>请求时间 09:53</span><span>完成获取 09:54:19</span><span>指数文件：无</span></div></div>
      <div className="chart-caption"><span><CircleAlert size={13} /> 下跌趋势置信度受数据缺口限制</span><em>iFinD 真实请求结果</em></div>
    </section>
  )
}

function Overview({ onNavigate, onDecision }) {
  return (
    <>
      <div className="page-heading"><div><span className="date-line"><CalendarDays size={15} /> 2026 年 10 月 27 日 · 数据截至 09:53</span><h1>今日策略：防守观察</h1><p>先看证据是否完整，再决定是否出手；空仓也是主动决策。</p></div><button className="button button-secondary" onClick={() => onNavigate('reviews')}><BookOpen size={17} /> 查看今日复盘</button></div>
      <div className="hero-grid"><StrategyCard onOpen={() => onDecision(decisions[0])} /><AccountCard /></div>

      <div className="market-strip">
        {markets.map((item) => <div className="market-quote" key={item.name}><span>{item.name}</span><strong>{item.value}</strong><em>{item.change}</em><small>{item.breadth}</small></div>)}
        <div className="market-regime"><Activity size={18} /><div><span>工作状态</span><strong>{snapshot.regime}</strong></div></div>
      </div>

      <div className="content-grid two-one">
        <MarketEvidence />
        <section className="card evidence-panel">
          <CardHeader eyebrow="EVIDENCE" title="判断依据" />
          <div className="evidence-score"><div className="score-ring"><strong>—</strong><span>不评分</span></div><div><b>证据不足</b><p>关键数据缺失，不做伪精确评分</p></div></div>
          <div className="evidence-list"><div><span>今日指数</span><b className="amber-text">未返回</b></div><div><span>观察样本</span><b className="negative">3 / 3 下跌</b></div><div><span>完整板块广度</span><b className="amber-text">缺失</b></div><div><span>当前动作</span><b>维持空仓</b></div></div>
          <div className="note-box"><CircleAlert size={17} /><p>防守不是预测今天一定下跌，而是当前证据不足以支持买入。</p></div>
        </section>
      </div>

      <div className="content-grid two-one lower-grid">
        <section className="card sector-panel">
          <CardHeader eyebrow="OBSERVED SAMPLE · NOT BREADTH" title="机器人观察样本" aside={<Badge>非完整板块</Badge>} />
          <div className="table-scroll"><table><thead><tr><th>个股</th><th>价格</th><th>当日</th><th>单分钟</th><th>成交量</th><th>当前结论</th></tr></thead><tbody>{observations.map((stock) => <tr key={stock.code}><td><b>{stock.name}</b></td><td>¥{stock.price}</td><td className="negative">{stock.change}%</td><td>{stock.minute}</td><td>{stock.volume}</td><td><Badge tone={stock.tone}>{stock.verdict}</Badge></td></tr>)}</tbody></table></div>
          <div className="table-note">成交量与成交额保留接口原值；09:53 单分钟变化不能确认持续修复。</div>
        </section>
        <section className="card timeline-panel">
          <CardHeader eyebrow="REVIEW RHYTHM" title="今日决策" aside={<button className="text-link" onClick={() => onNavigate('reviews')}>全部 <ChevronRight size={14} /></button>} />
          <div className="mini-timeline">{decisions.map((decision, index) => <button key={decision.id} onClick={() => onDecision(decision)}><span className={`timeline-marker ${index === 0 ? 'current' : ''}`}>{index === 0 ? <CheckCircle2 size={15} /> : <Circle size={13} />}</span><span className="timeline-time">{decision.time}</span><span><b>{decision.type}</b><small>{decision.title}</small></span><ChevronRight size={15} /></button>)}</div>
        </section>
      </div>
    </>
  )
}

function DecisionsPage({ onDecision }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('全部')
  const filters = ['全部', ...new Set(decisions.map((decision) => decision.action))]
  const filtered = useMemo(() => decisions.filter((decision) => `${decision.title}${decision.type}${decision.action}`.includes(query) && (filter === '全部' || decision.action === filter)), [query, filter])
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">DECISION JOURNAL</span><h1>每一次判断，都留下依据</h1><p>事实、推断和行动分开记录，不在收盘后改写当时的理由。</p></div><Badge tone="green">3 条真实记录</Badge></div>
      <div className="toolbar card"><label className="search-box"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索决策、动作或类型" /></label><div className="segmented">{filters.map((name) => <button className={filter === name ? 'active' : ''} key={name} onClick={() => setFilter(name)}>{name}</button>)}</div></div>
      <div className="decision-list">{filtered.map((decision) => <button className="decision-row card" key={decision.id} onClick={() => onDecision(decision)}><span className="decision-time">{decision.time}<small>{decision.type}</small></span><span className="decision-content"><span className="decision-id">{decision.id}</span><strong>{decision.title}</strong><p>{decision.market}</p><span className="decision-tags"><Badge tone="amber">{decision.action}</Badge><Badge>{decision.risk}</Badge><Badge>{decision.status}</Badge></span></span><span className="decision-open"><ChevronRight size={20} /></span></button>)}{filtered.length === 0 && <div className="empty-state card"><Search size={28} /><h3>没有匹配的决策</h3><p>试试清空搜索或切换动作筛选。</p></div>}</div>
    </>
  )
}

function ReviewsPage({ onExport }) {
  const [tab, setTab] = useState('盘中')
  const review = reviews[tab]
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">MORNING · MIDDAY · CLOSE</span><h1>复盘决策质量，而非只看盈亏</h1><p>当时的判断不可事后覆盖，新发现只能追加。</p></div><button className="button button-primary" onClick={() => onExport(tab)}><Download size={17} /> 导出{tab}复盘</button></div>
      <div className="review-layout"><aside className="review-nav card">{Object.keys(reviews).map((name, index) => <button key={name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}><span>0{index + 1}</span><div><b>{name}复盘</b><small>{reviews[name].label.split(' · ')[0]}</small></div><ChevronRight size={16} /></button>)}<div className="review-nav-note"><FileText size={18} /><p>每篇复盘关联当时可用的数据；数据缺口也写入记录。</p></div></aside><section className="review-document card"><div className="document-head"><Badge tone="green">{review.label}</Badge><span>{review.recordStatus}</span></div><h2>{review.title}</h2><ReviewSection number="01" title="事实 · 当时知道什么" items={review.facts} /><ReviewSection number="02" title="推断 · 我如何理解" items={review.inferences} /><ReviewSection number="03" title="行动 · 接下来怎么做" items={review.actions} /><div className="review-principle"><BrainCircuit size={22} /><div><b>复盘原则</b><p>盈利的交易也可能来自坏决策，亏损的交易也可能是合理风控。</p></div></div></section></div>
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
      <div className="research-banner dark-card"><FlaskConical size={28} /><div><h3>当前没有通过验证的 AI 策略</h3><p>样本外检验已经执行，但 H1—H3 均为数据不足；H4 盘中低吸、H5 历史选股尚未测试。</p></div><div className="research-stat"><strong>0</strong><span>通过验证策略</span></div></div>
      <div className="experiment-grid">{experiments.map((experiment) => <section className="card experiment-card" key={experiment.version}><div className="experiment-top"><Badge>{experiment.version}</Badge><Badge tone="amber">{experiment.status}</Badge></div><h2>{experiment.name}</h2><p>{experiment.description}</p><div className="experiment-meta"><div><span>有效标签 / 对照</span><b>{experiment.samples}</b></div><div><span>对照设计</span><b>{experiment.baseline}</b></div></div><div className="progress-track"><i style={{ width: '18%' }} /></div><small>保留原始样本切分，不为追求结论反复调参</small></section>)}</div>
      <section className="card validation-rules"><CardHeader eyebrow="VALIDATION CONTRACT" title="进入下一阶段的必要条件" /><div className="validation-grid">{['严格按时间划分训练与验证区间', '扣除最低佣金、滑点并遵守 T+1', '与当日等权和空仓基线同时比较', '保留失败样本，不只展示盈利案例'].map((rule, index) => <div key={rule}><span>0{index + 1}</span><p>{rule}</p></div>)}</div></section>
    </>
  )
}

function SystemPage() {
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">EXECUTION & SAFETY</span><h1>先确认系统知道自己在做什么</h1><p>面板只读展示；真实交易只能经过执行器，不由页面绕过。</p></div><Badge tone="green">真实委托未启用</Badge></div>
      <div className="system-grid">{systemItems.map((item) => <section className="card system-card" key={item.name}><div className="system-icon">{item.name === '交易网关' ? <WifiOff size={21} /> : item.name === '行情快照' ? <Database size={21} /> : <ShieldCheck size={21} />}</div><div><span>{item.name}</span><strong>{item.value}</strong><p>{item.note}</p></div><i className={`status-dot ${item.state}`} /></section>)}</div>
      <div className="content-grid two-one"><section className="card"><CardHeader eyebrow="HARD GUARDS" title="AI 资金安全边界" /><div className="guard-list">{['非 dryrun：AI 买卖均被硬阻断', '单笔资金：券商可用、一万元与 AI 决策预算取最小', '持仓映射：AI 与旧策略账户分离', '读取异常：连续失败后自动暂停', '意图桥接：游标与写入同事务，避免重复'].map((guard) => <div key={guard}><CheckCircle2 size={17} /><span>{guard}</span></div>)}</div></section><section className="card next-check"><CardHeader eyebrow="NEXT CHECK" title="家里电脑待验证" /><ol><li><span>1</span>网关健康与登录状态</li><li><span>2</span>资金、持仓只读接口</li><li><span>3</span>自动验证码现场表现</li><li><span>4</span>真实数据库 dryrun 链路</li></ol><div className="note-box"><CirclePause size={17} /><p>完成这些检查，也不等于自动开启实盘。</p></div></section></div>
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
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className="decision-modal" role="dialog" aria-modal="true" aria-label="决策详情"><div className="modal-head"><div><span className="eyebrow">{decision.id}</span><h2>{decision.title}</h2><div className="decision-tags"><Badge tone="amber">{decision.action}</Badge><Badge>{decision.time} · {decision.type}</Badge><Badge>{decision.risk}</Badge></div></div><button className="icon-button" onClick={onClose} aria-label="关闭决策详情"><X size={20} /></button></div><div className="modal-body">{sections.map(([title, content]) => <div className="modal-section" key={title}><h3>{title}</h3><p>{content}</p></div>)}</div><div className="modal-footer"><ShieldCheck size={16} /><span>真实决策记录，不构成真实委托或收益承诺</span></div></section></div>
}

export default function App() {
  const [active, setActive] = useState('overview')
  const [mobileOpen, setMobileOpen] = useState(false)
  const [selectedDecision, setSelectedDecision] = useState(null)
  const [toast, setToast] = useState('')
  const navigate = (page) => { setActive(page); setMobileOpen(false); window.scrollTo({ top: 0, behavior: 'smooth' }) }
  const exportReview = (tab) => {
    const review = reviews[tab]
    const text = [`# ${review.label}`, '', `## ${review.title}`, '', '## 事实', ...review.facts.map((x) => `- ${x}`), '', '## 推断', ...review.inferences.map((x) => `- ${x}`), '', '## 行动', ...review.actions.map((x) => `- ${x}`), '', `> ${review.recordStatus}；行情快照截至 ${snapshot.displayTime}。`].join('\n')
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
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}><div className="brand"><div className="brand-mark">衡</div><div><strong>衡策</strong><span>AI 量化研究室</span></div></div><div className="workspace-label">工作空间</div><nav>{navItems.map((item) => <button key={item.id} className={active === item.id ? 'active' : ''} onClick={() => navigate(item.id)}><item.icon size={18} /><span>{item.label}</span>{item.count && <em>{item.count}</em>}</button>)}</nav><div className="sidebar-bottom"><div className="safety-card"><ShieldCheck size={20} /><div><b>安全边界运行中</b><span>AI 仅限 DRYRUN</span></div></div><div className="profile"><div className="avatar">J</div><div><b>Jamo</b><span>实验账户 · ¥10,000</span></div><ChevronRight size={15} /></div></div></aside>
      <main className="main-area">
        <header className="topbar"><button className="mobile-menu icon-button" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Menu size={20} /></button><div className="breadcrumb"><span>AI 实验</span><ChevronRight size={14} /><b>{pageNames[active]}</b></div><div className="topbar-actions"><Badge tone="green" dot>真实快照</Badge><span className="market-closed">盘中 · 09:53</span></div></header>
        <div className="demo-notice"><CircleAlert size={16} /><span><b>真实数据 · 非实时</b> · 个股与模拟账户截至 09:53；今日指数、市场宽度和完整板块数据缺失。</span></div>
        <div className="page-content">{active === 'overview' && <Overview onNavigate={navigate} onDecision={setSelectedDecision} />}{active === 'decisions' && <DecisionsPage onDecision={setSelectedDecision} />}{active === 'reviews' && <ReviewsPage onExport={exportReview} />}{active === 'research' && <ResearchPage />}{active === 'system' && <SystemPage />}<footer><span>衡策 · AI 量化研究室</span><span>真实快照 · {snapshot.displayTime} · v0.2.0</span></footer></div>
      </main>
      {selectedDecision && <DecisionModal decision={selectedDecision} onClose={() => setSelectedDecision(null)} />}
      {toast && <div className="toast"><CheckCircle2 size={17} />{toast}</div>}
    </div>
  )
}
