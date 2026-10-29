import { useEffect, useMemo, useState } from 'react'
import {
  Activity, ArrowUpRight, BookOpen, BrainCircuit, CalendarDays, CheckCircle2,
  ChevronRight, Circle, CircleAlert, CirclePause, Clock3, Database, Download,
  Eye, FileText, FlaskConical, LayoutDashboard, LineChart, ListChecks, Menu,
  Search, Server, ShieldCheck, SlidersHorizontal, WifiOff, X,
} from 'lucide-react'
import { decisions as historicalDecisions, experiments, reviews } from './data/realSnapshot.js'
import { money, useDashboardData } from './data/useDashboardData.js'
import { useResearchStatus } from './data/useResearchStatus.js'

const navItems = [
  { id: 'overview', label: '今日总览', icon: LayoutDashboard },
  { id: 'decisions', label: '决策记录', icon: ListChecks },
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

function AccountCard({ account }) {
  return (
    <section className="account-card dark-card">
      <div className="account-top"><span className="eyebrow">实验账户 · AI EXPERIMENT</span><Badge tone="dark" dot>PAPER</Badge></div>
      <div className="account-value"><span>总资产 · {account.positions ? '含持仓市值' : '空仓现金口径'}</span><strong><small>¥</small> {money(account.total)}</strong></div>
      <div className="account-metrics"><div><span>可用资金</span><b>{money(account.cash)}</b></div><div><span>当前仓位</span><b>{account.positionPct.toFixed(1)}%</b></div><div><span>今日盈亏</span><b>{account.todayPnl == null ? '—' : money(account.todayPnl)}</b></div></div>
      <div className="account-footer"><ShieldCheck size={15} /><span>liveTrading=false · {account.source} · 今日模拟成交 {account.fillsToday} 笔</span></div>
    </section>
  )
}

function StrategyCard({ onOpen, live, decision }) {
  const current = decision || historicalDecisions[0]
  const persisted = Boolean(current.persisted)
  return (
    <section className="strategy-card card">
      <CardHeader eyebrow="TODAY'S PLAYBOOK" title="今日策略" aside={<Badge tone={persisted ? 'green' : 'amber'}>{persisted ? `${current.time} 盘中 AI` : '09:53 历史决策'}</Badge>} />
      <div className="strategy-main"><div className="strategy-icon"><Eye size={24} /></div><div><h3>{current.title}</h3><p>{persisted ? `基于全市场与行业广度生成，原始回答和证据已追加保存；行情截至 ${live.quoteAsOf}。` : `这是 09:53 留下的 AI 决策；${live.connected ? `下方行情已更新至 ${live.quoteAsOf}，尚未生成新的 AI 结论。` : '实时行情尚未连接，不更新当前判断。'}`}</p></div></div>
      <div className="condition-list">
        {persisted ? <><div><span>01</span><p>{current.market}</p></div><div><span>02</span><p>{current.sector}</p></div><div><span>03</span><p>{current.stock}</p></div></> : <><div><span>01</span><p>当前无合格候选，不生成 quote.json，不调用 buy 或 sell。</p></div><div><span>02</span><p>指数停止创新低、板块多股共振后，才评估小仓试错。</p></div><div><span>03</span><p>买入前核验新鲜 Quote、100 股、费用、T+1 与失效位。</p></div></>}
      </div>
      <button className="text-button" onClick={onOpen}>查看完整决策依据 <ArrowUpRight size={16} /></button>
    </section>
  )
}

function MarketEvidence({ evidence, live, market, sectors }) {
  const complete = Boolean(evidence.researchComplete)
  return (
    <section className="card market-panel">
      <CardHeader eyebrow="MARKET REGIME" title="大盘证据状态" aside={<Badge tone={complete ? 'green' : 'amber'}>{complete ? '广度已接入' : market.loading ? '正在载入广度' : '数据不完整'}</Badge>} />
      <div className="market-summary"><div><LineChart size={17} /><span>行业宽度</span><b>{sectors.available ? `${sectors.up} 涨 / ${sectors.down} 跌` : '等待行业数据'}</b></div><div><Activity size={17} /><span>全市场成交额</span><b>{market.available ? `${(market.amount / 100000000).toFixed(1)} 亿` : '等待全市场数据'}</b></div><div><SlidersHorizontal size={17} /><span>市场宽度</span><b>{market.available ? `${market.up} 涨 / ${market.down} 跌` : '等待全市场数据'}</b></div></div>
      <div className="data-gap"><Database size={28} /><h3>{complete ? '全市场与行业证据已更新' : '市场广度尚未完整返回'}</h3><p>{complete ? `覆盖沪深京 A 股 ${market.total} / ${market.universe} 只，红盘占比 ${market.upRatio}%；行业 ${sectors.total} 个。指数行情 ${live.quoteAsOf}，广度按分钟缓存。` : '首次载入需要拉取全市场清单和五千多只股票；失败时保留最后成功数据并明确标记，不用三只样本替代大盘。'}</p><div className="data-gap-meta"><span>指数返回 {evidence.indicesReceived} / {evidence.indicesTotal}</span><span>广度覆盖 {market.coverage || 0}%</span><span>60 秒缓存</span></div></div>
      <div className="chart-caption"><span><CircleAlert size={13} /> 数据完整不等于策略已验证，不产生自动交易</span><em>腾讯 / 新浪只读行情</em></div>
    </section>
  )
}

function SectorRank({ title, rows, tone }) {
  return <section className="card sector-panel"><CardHeader eyebrow="INDUSTRY BREADTH" title={title} aside={<Badge tone={tone}>{rows.length} 个行业</Badge>} /><div className="table-scroll"><table><thead><tr><th>行业</th><th>平均涨跌</th><th>公司家数</th><th>领涨股</th></tr></thead><tbody>{rows.map((row) => <tr key={row.name}><td><b>{row.name}</b></td><td className={row.change >= 0 ? 'positive' : 'negative'}>{row.change > 0 ? '+' : ''}{row.change}%</td><td>{row.companies}</td><td>{row.leaderName} · {row.leaderChange}%</td></tr>)}</tbody></table></div></section>
}

function Overview({ onNavigate, onDecision, data, decisions }) {
  const { snapshot, markets, observations, account, evidence, live, marketBreadth, sectorBreadth } = data
  const current = decisions[0] || historicalDecisions[0]
  return (
    <>
      <div className="page-heading"><div><span className="date-line"><CalendarDays size={15} /> 行情数据截至 {snapshot.displayTime}</span><h1>今日策略：{snapshot.mode || '防守观察'}</h1><p>先看证据是否完整，再决定是否出手；空仓也是主动决策。</p></div><button className="button button-secondary" onClick={() => onNavigate('reviews')}><BookOpen size={17} /> 查看今日复盘</button></div>
      <div className="hero-grid"><StrategyCard onOpen={() => onDecision(current)} live={live} decision={current} /><AccountCard account={account} /></div>

      <div className="market-strip">
        {markets.map((item) => <div className="market-quote" key={item.name}><span>{item.name}</span><strong>{item.value}</strong><em>{item.change}</em><small>{item.breadth}</small></div>)}
        <div className="market-regime"><Activity size={18} /><div><span>工作状态</span><strong>{snapshot.regime}</strong></div></div>
      </div>

      <div className="content-grid two-one">
        <MarketEvidence evidence={evidence} live={live} market={marketBreadth} sectors={sectorBreadth} />
        <section className="card evidence-panel">
          <CardHeader eyebrow="EVIDENCE" title="判断依据" />
          <div className="evidence-score"><div className="score-ring"><strong>—</strong><span>不评分</span></div><div><b>{evidence.researchComplete ? '证据已接入' : '等待完整证据'}</b><p>{evidence.researchComplete ? '仍需 AI 判断与样本外验证' : '全市场或行业广度尚未就绪'}</p></div></div>
          <div className="evidence-list"><div><span>全市场宽度</span><b>{marketBreadth.available ? `${marketBreadth.up} 涨 / ${marketBreadth.down} 跌` : '等待数据'}</b></div><div><span>行业宽度</span><b>{sectorBreadth.available ? `${sectorBreadth.up} 涨 / ${sectorBreadth.down} 跌` : '等待数据'}</b></div><div><span>涨跌停</span><b>{marketBreadth.available ? `${marketBreadth.limitUp} / ${marketBreadth.limitDown}` : '—'}</b></div><div><span>当前动作</span><b>{current.action}</b></div></div>
          <div className="note-box"><CircleAlert size={17} /><p>AI 判断和证据只追加保存，不覆盖历史，也不产生交易指令。</p></div>
        </section>
      </div>

      {sectorBreadth.available && <div className="content-grid two-one lower-grid"><SectorRank title="行业领涨" rows={sectorBreadth.leaders} tone="green" /><SectorRank title="行业领跌" rows={sectorBreadth.laggards} tone="amber" /></div>}

      <div className="content-grid two-one lower-grid">
        <section className="card sector-panel">
          <CardHeader eyebrow="OBSERVED SAMPLE · NOT BREADTH" title="机器人观察样本" aside={<Badge>非完整板块</Badge>} />
          <div className="table-scroll"><table><thead><tr><th>个股</th><th>价格</th><th>当日</th><th>短时变化</th><th>成交量（手）</th><th>当前结论</th></tr></thead><tbody>{observations.map((stock) => <tr key={stock.code}><td><b>{stock.name}</b></td><td>¥{stock.price}</td><td className={stock.change > 0 ? 'positive' : 'negative'}>{stock.change == null ? '—' : `${stock.change}%`}</td><td>{stock.minute}</td><td>{stock.volume}</td><td><Badge tone={stock.tone}>{stock.verdict}</Badge></td></tr>)}</tbody></table></div>
          <div className="table-note">重点行情每秒刷新；短时变化是相邻两次快照对比，不代表逐分钟 K 线，也不构成买卖结论。</div>
        </section>
        <section className="card timeline-panel">
          <CardHeader eyebrow="REVIEW RHYTHM" title="今日决策" aside={<button className="text-link" onClick={() => onNavigate('reviews')}>全部 <ChevronRight size={14} /></button>} />
          <div className="mini-timeline">{decisions.slice(0, 6).map((decision, index) => <button key={decision.id} onClick={() => onDecision(decision)}><span className={`timeline-marker ${index === 0 ? 'current' : ''}`}>{index === 0 ? <CheckCircle2 size={15} /> : <Circle size={13} />}</span><span className="timeline-time">{decision.time}</span><span><b>{decision.type}</b><small>{decision.title}</small></span><ChevronRight size={15} /></button>)}</div>
        </section>
      </div>
    </>
  )
}

function DecisionsPage({ onDecision, decisions }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('全部')
  const filters = ['全部', ...new Set(decisions.map((decision) => decision.action))]
  const filtered = useMemo(() => decisions.filter((decision) => `${decision.title}${decision.type}${decision.action}`.includes(query) && (filter === '全部' || decision.action === filter)), [decisions, query, filter])
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">DECISION JOURNAL</span><h1>每一次判断，都留下依据</h1><p>事实、推断和行动分开记录，不在收盘后改写当时的理由。</p></div><Badge tone="green">{decisions.length} 条真实记录</Badge></div>
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
  const { data, kronos, loading, error } = useResearchStatus()
  const heading = (
    <div className="page-heading"><div><span className="eyebrow">STRATEGY LAB · PIT BASELINE</span><h1>先证伪，再考虑上线</h1><p>假设、样本和基线公开，避免把事后解释当成策略能力。</p></div><Badge tone={data?.deploymentEligible ? 'green' : 'amber'}>{loading ? '正在读取' : data?.available ? 'PIT 基线已完成 · 不可部署' : '研究结果不可用'}</Badge></div>
  )
  if (loading) return <>{heading}<section className="research-state card"><FlaskConical size={28} /><h2>正在读取研究产物</h2><p>只读取已生成的 summary，不触发训练，也不连接交易执行器。</p></section></>
  if (error || !data?.available) return <>{heading}<section className="research-state card"><CircleAlert size={28} /><h2>{error ? '研究数据读取失败' : '尚未生成 PIT 基线结果'}</h2><p>{error || '研究产物当前不可用；面板不会用演示结果替代。'}</p></section></>

  const percent = (value) => `${(Number(value) * 100).toFixed(2)}%`
  const integer = (value) => Number(value).toLocaleString('zh-CN')
  const date = (value) => value ? String(value).slice(0, 10) : '—'
  const latestFold = data.folds[data.folds.length - 1]
  const maxQuantileReturn = Math.max(...data.quantileGrossReturns.map(Math.abs), 0)
  const walkForward = data.walkForward || {}
  return (
    <>
      {heading}
      <div className="research-banner dark-card"><FlaskConical size={28} /><div><h3>全 A PIT 基线已完成，尚未通过上线评估</h3><p>模型包含相对排序信息，但分组不单调、回撤较大且最近折衰减。结果仅供研究，不替换现有模型，不产生交易指令。</p></div><div className="research-stat"><strong>0</strong><span>通过上线评估</span></div></div>

      {kronos?.available && <section className="card kronos-status">
        <CardHeader eyebrow="KRONOS-SMALL · ZERO-SHOT CONTROL" title="Kronos 同口径对照" aside={<Badge tone={kronos.scope === 'full_walk_forward' ? 'green' : 'amber'}>{kronos.scope === 'full_walk_forward' ? '全量对照已完成' : '真实权重冒烟通过 · 全量未运行'}</Badge>} />
        <div className="kronos-status-grid">
          <div><span>当前范围</span><b>{kronos.scope === 'full_walk_forward' ? `五折 · ${integer(kronos.rankIcDays)} 个样本外日` : `${kronos.smoke.days} 日 × ${kronos.smoke.names_per_day} 只 · ${integer(kronos.smoke.rows)} 条`}</b></div>
          <div><span>全市场覆盖率</span><b>{percent(kronos.coverage)}</b></div>
          <div><span>信号口径</span><b>512 日上下文 → T+1 / T+6 开盘</b></div>
        </div>
        <p className="kronos-status-note">{kronos.scope === 'full_walk_forward' ? '全量零样本对照已生成；仍需独立稳健性审查，不自动替换基线。' : '仅验证固定官方权重、PIT 上下文、批量推理与评价链路。冒烟 RankIC 不作为模型优劣证据，因此不与上方 LightGBM 指标比较。'}</p>
      </section>}

      <section className="research-metrics" aria-label="样本外核心结果">
        <div className="research-metric card"><span>样本外 RankIC</span><strong>{data.rankIc.toFixed(4)}</strong><small>t 值 {data.rankIcTStat.toFixed(2)} · 相对排序</small></div>
        <div className="research-metric card"><span>样本外信号日</span><strong>{integer(data.rankIcDays)}</strong><small>{data.folds.length} 折滚动样本外</small></div>
        <div className="research-metric card"><span>预测覆盖率</span><strong>{percent(data.coverage)}</strong><small>{integer(data.sampleRows)} 行固定样本</small></div>
        <div className="research-metric card research-metric-risk"><span>最高组最大回撤</span><strong>{percent(data.maxDrawdown)}</strong><small>近似成本口径 · 非收益承诺</small></div>
      </section>

      <section className="card research-protocol">
        <CardHeader eyebrow="POINT-IN-TIME DATA CONTRACT" title="数据与标签口径" aside={<Badge tone="green">PIT 全市场</Badge>} />
        <div className="research-protocol-grid">
          <div><span>固定样本</span><b>{integer(data.sampleRows)} 行 · {integer(data.codes)} 只</b></div>
          <div><span>信号区间</span><b>{data.signalStart} — {data.signalEnd}</b></div>
          <div><span>信号日 / 特征完整率</span><b>{integer(data.signalDays)} 日 / {percent(data.featureCompleteRatio)}</b></div>
          <div><span>标签路径</span><b>T 收盘 → T+1 开盘 → T+6 开盘</b></div>
          <div><span>滚动窗口</span><b>训练 {walkForward.train_days} / 验证 {walkForward.validation_days} / 测试 {walkForward.test_days} 日</b></div>
          <div><span>信息隔离</span><b>{walkForward.embargo_days} 日隔离 · 5 日持有</b></div>
        </div>
      </section>

      <div className="research-evidence-grid">
        <section className="card research-quantiles">
          <CardHeader eyebrow="CROSS-SECTIONAL RANKING" title="五分组平均毛收益" aside={<Badge>未扣成本</Badge>} />
          <p className="research-section-note">Q1 至 Q5 按模型得分由低到高排列；柱长只用于同组对比，精确值见标签。</p>
          <div className="quantile-list">{data.quantileGrossReturns.map((value, index) => <div className={`quantile-row ${index === 4 ? 'quantile-top' : ''}`} key={`Q${index + 1}`}><span>Q{index + 1}</span><div className="quantile-track"><i style={{ width: `${maxQuantileReturn ? Math.abs(value) / maxQuantileReturn * 100 : 0}%` }} /></div><b>{percent(value)}</b></div>)}</div>
          <div className="research-callout"><CircleAlert size={17} /><p><b>Q5 不是最优组。</b>最高组毛收益 {percent(data.quantileGrossReturns[4])}，低于 Q2—Q4；五分组单调性仅 {data.quantileMonotonicity.toFixed(2)}。</p></div>
        </section>
        <section className="card research-performance">
          <CardHeader eyebrow="NON-OVERLAPPING PORTFOLIO" title="最高五分组组合" />
          <div className="performance-list">
            <div><span>累计净收益</span><b>{percent(data.netReturn)}</b></div>
            <div><span>年化净收益</span><b>{percent(data.annualizedNetReturn)}</b></div>
            <div className="performance-risk"><span>最大回撤</span><b>{percent(data.maxDrawdown)}</b></div>
            <div><span>期间均值 t 值</span><b>{data.periodMeanTStat.toFixed(2)}</b></div>
            <div><span>平均单边换手</span><b>{percent(data.averageTurnover)}</b></div>
            <div><span>平均持仓</span><b>{integer(Math.round(data.averagePositions))} 只</b></div>
          </div>
          <p className="research-footnote">历史回测含近似换手成本，不含市场冲击；年化数字不是未来收益目标或承诺。</p>
        </section>
      </div>

      <section className="card research-folds">
        <CardHeader eyebrow="WALK-FORWARD OUT-OF-SAMPLE" title="逐折样本外结果" aside={<Badge>{data.folds.length} 折 · 每折 126 日</Badge>} />
        <div className="table-scroll"><table><thead><tr><th>折次</th><th>测试信号区间</th><th className="number-cell">RankIC</th><th className="number-cell">最高组净收益</th><th className="number-cell">最佳迭代</th></tr></thead><tbody>{data.folds.map((fold) => <tr key={fold.fold}><td><b>Fold {fold.fold}</b></td><td>{date(fold.testSignalStart)} — {date(fold.testSignalEnd)}</td><td className="number-cell">{fold.rankIc.toFixed(4)}</td><td className={`number-cell ${fold.netReturn < 0 ? 'negative' : 'positive'}`}>{fold.netReturn > 0 ? '+' : ''}{percent(fold.netReturn)}</td><td className="number-cell">{fold.bestIteration ?? '—'}</td></tr>)}</tbody></table></div>
        <p className="table-note">每一折只使用该测试区间之前可见的数据；净收益为最高组非重叠持有组合口径。</p>
      </section>

      <section className="card research-risks">
        <CardHeader eyebrow="DEPLOYMENT GATE · FAILED" title="为什么现在不能上线" aside={<Badge tone="amber">deploymentEligible=false</Badge>} />
        <div className="risk-reason-grid">
          <div><span>01</span><p><b>分组不单调</b>Q5 弱于 Q2—Q4，得分最高并不等于收益最高。</p></div>
          <div><span>02</span><p><b>回撤不可忽略</b>最高组最大回撤 {percent(data.maxDrawdown)}，期间均值 t 值仅 {data.periodMeanTStat.toFixed(2)}。</p></div>
          <div><span>03</span><p><b>最近折明显衰减</b>Fold {latestFold.fold} RankIC 降至 {latestFold.rankIc.toFixed(4)}，净收益 {percent(latestFold.netReturn)}。</p></div>
          <div><span>04</span><p><b>执行现实未覆盖</b>日线研究不含涨跌停锁死、部分成交、市场冲击与最低佣金。</p></div>
        </div>
        <div className="research-decision"><ShieldCheck size={19} /><span>结论：保留为研究基线；不自动替换模型，不连接模拟或真实交易。</span></div>
      </section>

      <div className="research-history-heading"><span className="eyebrow">HISTORICAL HYPOTHESES</span><h2>历史假设与待验证方向</h2><p>以下 H1—H5 是早期实验记录，不属于本次已完成的 PIT 基线。</p></div>
      <div className="experiment-grid">{experiments.map((experiment) => <section className="card experiment-card" key={experiment.version}><div className="experiment-top"><Badge>{experiment.version}</Badge><Badge tone="amber">{experiment.status}</Badge></div><h2>{experiment.name}</h2><p>{experiment.description}</p><div className="experiment-meta"><div><span>有效标签 / 对照</span><b>{experiment.samples}</b></div><div><span>对照设计</span><b>{experiment.baseline}</b></div></div><small>保留原始样本切分，不为追求结论反复调参</small></section>)}</div>
      <section className="card validation-rules"><CardHeader eyebrow="VALIDATION CONTRACT" title="进入下一阶段的必要条件" /><div className="validation-grid">{['严格按时间划分训练与验证区间', '扣除最低佣金、滑点并遵守 T+1', '与当日等权和空仓基线同时比较', '保留失败样本，不只展示盈利案例'].map((rule, index) => <div key={rule}><span>0{index + 1}</span><p>{rule}</p></div>)}</div></section>
    </>
  )
}

function SystemPage({ items }) {
  return (
    <>
      <div className="page-heading"><div><span className="eyebrow">EXECUTION & SAFETY</span><h1>先确认系统知道自己在做什么</h1><p>面板只读展示；真实交易只能经过执行器，不由页面绕过。</p></div><Badge tone="green">真实委托未启用</Badge></div>
      <div className="system-grid">{items.map((item) => <section className="card system-card" key={item.name}><div className="system-icon">{item.name === '交易网关' ? <WifiOff size={21} /> : item.name.includes('行情') || item.name.includes('数据库') ? <Database size={21} /> : <ShieldCheck size={21} />}</div><div><span>{item.name}</span><strong>{item.value}</strong><p>{item.note}</p></div><i className={`status-dot ${item.state}`} /></section>)}</div>
      <div className="content-grid two-one"><section className="card"><CardHeader eyebrow="HARD GUARDS" title="AI 资金安全边界" /><div className="guard-list">{['本地服务只监听 127.0.0.1，不提供浏览器写接口', 'SQLite 使用 mode=ro，浏览器不能执行 SQL', '动态策略、模拟成交和风控拒绝均追加留痕', '独立重算现金、持仓、费用和 T+1，不一致即暂停', '真实委托未启用，AI 与旧策略账户分离'].map((guard) => <div key={guard}><CheckCircle2 size={17} /><span>{guard}</span></div>)}</div></section><section className="card next-check"><CardHeader eyebrow="NEXT CHECK" title="后续验证顺序" /><ol><li><span>1</span>积累动态策略模拟样本</li><li><span>2</span>逐笔核对成交、费用与对账记录</li><li><span>3</span>完成样本外验证和异常演练</li><li><span>4</span>现场复核后再评估是否申请实盘</li></ol><div className="note-box"><CirclePause size={17} /><p>模拟闭环已完成，也不等于自动开启实盘。</p></div></section></div>
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
  const dashboard = useDashboardData()
  const { snapshot, live, marketBreadth, sectorBreadth } = dashboard
  const allDecisions = useMemo(() => [...(dashboard.decisions || []), ...historicalDecisions], [dashboard.decisions])
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
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}><div className="brand"><div className="brand-mark">衡</div><div><strong>衡策</strong><span>AI 量化研究室</span></div></div><div className="workspace-label">工作空间</div><nav>{navItems.map((item) => <button key={item.id} className={active === item.id ? 'active' : ''} onClick={() => navigate(item.id)}><item.icon size={18} /><span>{item.label}</span>{item.id === 'decisions' && <em>{allDecisions.length}</em>}</button>)}</nav><div className="sidebar-bottom"><div className="safety-card"><ShieldCheck size={20} /><div><b>安全边界运行中</b><span>AI 仅限 DRYRUN</span></div></div><div className="profile"><div className="avatar">J</div><div><b>Jamo</b><span>实验账户 · ¥{money(dashboard.account.total)}</span></div><ChevronRight size={15} /></div></div></aside>
      <main className="main-area">
        <header className="topbar"><button className="mobile-menu icon-button" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Menu size={20} /></button><div className="breadcrumb"><span>AI 实验</span><ChevronRight size={14} /><b>{pageNames[active]}</b></div><div className="topbar-actions"><Badge tone={live.connected ? 'green' : 'amber'} dot>{live.connected ? '只读实时' : '静态快照'}</Badge><span className="market-closed">{snapshot.phase} · {snapshot.displayTime.split(' ')[1] || '等待连接'}</span></div></header>
        <div className="demo-notice"><CircleAlert size={16} /><span><b>{live.connected ? '真实数据 · 只读实时' : '真实数据 · 静态快照'}</b> · {live.connected ? `指数与观察样本截至 ${snapshot.displayTime}，重点行情每秒刷新；` : '尚未连接本地只读 API；'}全市场 {marketBreadth.available ? `${marketBreadth.total} 只已覆盖` : '载入中'}，行业 {sectorBreadth.available ? `${sectorBreadth.total} 个已接入` : '载入中'}；Kimi 动态策略只驱动隔离模拟账户，研究、成交和对账记录均留痕，不触发真实交易。{live.error ? ` 连接提示：${live.error}` : ''}</span></div>
        <div className="page-content">{active === 'overview' && <Overview onNavigate={navigate} onDecision={setSelectedDecision} data={dashboard} decisions={allDecisions} />}{active === 'decisions' && <DecisionsPage onDecision={setSelectedDecision} decisions={allDecisions} />}{active === 'reviews' && <ReviewsPage onExport={exportReview} />}{active === 'research' && <ResearchPage />}{active === 'system' && <SystemPage items={dashboard.systemItems} />}<footer><span>衡策 · AI 量化研究室</span><span>{live.connected ? '只读实时' : '真实快照'} · {snapshot.displayTime} · v0.4.0</span></footer></div>
      </main>
      {selectedDecision && <DecisionModal decision={selectedDecision} onClose={() => setSelectedDecision(null)} />}
      {toast && <div className="toast"><CheckCircle2 size={17} />{toast}</div>}
    </div>
  )
}
