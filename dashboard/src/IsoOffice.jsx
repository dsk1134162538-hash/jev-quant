import officeImg from './assets/office-iso.jpg'
import { SEATS, sceneOf, speechOf } from './Office.jsx'

// 等距视角的办公室（dj 2026-11-12 给了一张参考图：「类似于这样的」）。前两版手画的 SVG 都被嫌丑，这一版直接用这张图做底，
// 在上面叠活的东西：墙上的行情屏换成我们自己的数据、每个人头顶的名牌和状态灯、正在说话的人的气泡、点人弹出完整汇报。
// 图是画死的：人不会走动，也不会真的离开座位。下班 / 午休用整张图压暗、名牌变灰或显示 zz 来表示。
// 坐标都按原图 2000×1116 写，渲染时换成百分比；字号用 cqw（容器宽度的百分之一 = 原图 20 像素），跟着图一起缩放。
const IW = 2000
const IH = 1116

// 每个岗位对应图里的哪个人：[头顶的 x, y]。四张桌子：左上桌 = 汇报一组，中间桌 = 汇报二组（站着穿西装的是风控），右边桌 = 策略组，下面桌 = 交易组
const SPOTS = {
  manager: [300, 262],
  breadth: [1118, 292], ladder: [1425, 262], high: [1238, 372],
  movers: [1052, 498], scan: [790, 588], risk: [985, 405],
  book: [1108, 742],
  'strat:relay': [1520, 518], 'strat:wts': [1858, 470], 'strat:bz': [1650, 588],
  'trader:aiflex': [1352, 628], 'trader:aiflex2': [1498, 722],
}
const NAMES = { manager: '经理', ...Object.fromEntries(SEATS.map((s) => [s.role, s.name])) }
const COLORS = { manager: '#c9a13b', ...Object.fromEntries(SEATS.map((s) => [s.role, s.color])) }
const TABLES = [[1290, 232, '汇报一组'], [880, 470, '汇报二组'], [1700, 420, '策略组'], [1300, 600, '交易组']]
const LEVEL_DOT = { ok: '#5fd08a', warn: '#f0c36a', bad: '#ff6b60', calm: '#8fb7e0' }
const pos = (x, y) => ({ left: `${(x / IW) * 100}%`, top: `${(y / IH) * 100}%` })

// 这个岗位现在有没有数：决定状态灯的颜色（和原来屏幕亮不亮是同一套判断）
function toneOf(role, data) {
  if (role === 'manager') return data?.manager ? 'calm' : null
  if (role.startsWith('strat:')) {
    const t = (data?.strategyDesk?.traders || []).find((x) => `strat:${x.key}` === role)
    return !t ? null : t.error ? 'bad' : t.picks.length ? 'ok' : 'calm'
  }
  if (role.startsWith('trader:')) {
    const t = (data?.traders || []).find((x) => `trader:${x.sid}` === role)
    const today = t?.account?.today
    return !t ? null : today > 0 ? 'bad' : today < 0 ? 'ok' : 'calm'           // A 股红涨绿跌：赚钱亮红灯
  }
  const r = (data?.roles || []).find((x) => x.role === role)
  return r && r.level !== 'idle' ? r.level : null
}

function footOf(sc, phase, n) {
  if (sc.stage === 'closed') return '周末休市 · 没人上班（图是画死的，人不会走，灯关了就是下班）'
  if (sc.stage === 'empty') return sc.m < 525 ? '还没上班 · 08:45 开始到岗' : '下班了 · 灯关了，程序还在机柜里跑'
  if (sc.stage === 'arrive') return `盘前 · 陆续到岗 ${n}/13`
  if (sc.stage === 'lunch') return '午休 · 13:00 开工'
  if (sc.stage === 'review') return '收盘 · 经理在复盘'
  if (sc.stage === 'leave') return sc.meeting ? '周五 · 经理和两位交易员在开周复盘会，其他人陆续下班' : `收盘 · 陆续下班，还剩 ${n} 人`
  return `${phase} · 全员在岗`
}

export function IsoOffice({ data, active, picked, event, phase, onPick }) {
  const sc = sceneOf(data?.now)
  const working = ['work', 'arrive', 'review', 'leave'].includes(sc.stage)
  const dim = working ? 0 : sc.stage === 'lunch' ? 0.28 : 0.62
  const m = data?.manager
  const sd = data?.strategyDesk
  const todo = (sd?.final?.items || []).filter((i) => i.do)
  const roles = Object.fromEntries((data?.roles || []).map((r) => [r.role, r]))
  const nums = (roles.breadth?.line || '').match(/涨 (\d+) \/ 跌 (\d+)/)
  const share = nums ? +nums[1] / (+nums[1] + +nums[2] || 1) : 0.5
  const ticker = [
    m ? `经理 ${m.slot} ${m.regime}` : '经理 09:26 开会',
    ...(data?.traders || []).filter((t) => t.account?.today != null).map((t) => `${t.label} ${t.account.today > 0 ? '+' : ''}${Number(t.account.today).toFixed(2)}%`),
    data?.mgr?.account?.today != null ? `经理账户 ${data.mgr.account.today > 0 ? '+' : ''}${Number(data.mgr.account.today).toFixed(2)}%` : null,
    data?.stance ? `风险档位 ${data.stance.level}` : null,
  ].filter(Boolean).join('  ◆  ')

  const speech = active ? speechOf(active, data) : null
  const spot = active ? SPOTS[active] : null
  const stateOf = (role) => sc.where(role)
  const away = active && stateOf(active) === 'gone'
  const bubble = !speech ? '' : event && event.role === active ? event.text : away ? `（下班了）走之前说的：${speech.short}` : speech.short

  return (
    <div className="iso">
      <div className="iso-stage">
        <img src={officeImg} alt="交易台办公室：左边是经理办公室和会议室，右边大厅四张桌子，墙上是行情屏和机柜" draggable="false" />
        {/* 墙上的行情屏：盖住原图的字，换成我们自己的数 */}
        <div className="iso-led" style={pos(826, 158)}>
          <div className="row big">{nums ? <><b className="up">涨 {nums[1]}</b><b className="down">跌 {nums[2]}</b></> : <b className="dimmed">{roles.breadth?.line || '宽度 · 等开盘'}</b>}
            <i><u style={{ width: `${share * 100}%` }} /></i></div>
          <div className="row">{roles.ladder ? roles.ladder.line.split('｜')[0] : '梯队 · 等开盘'}</div>
          <div className="row amber">{sd ? (todo.length ? `今日可做 ${todo.length} 只：${todo.map((i) => i.name).join('、')}` : `${sd.slot} 没有应该做的票`) : '今日可做 · 09:26 汇总'}</div>
          <div className="row run"><span key={ticker} style={{ animationDuration: `${Math.max(14, ticker.length * 0.45)}s` }}>{ticker}</span></div>
        </div>
        {/* 每张桌子的组名 */}
        {TABLES.map(([x, y, name]) => <span key={name} className="iso-table" style={pos(x, y)}>{name}</span>)}
        <span className="iso-table room" style={pos(118, 120)}>经理办公室</span>
        <span className="iso-table room" style={pos(96, 668)}>会议室 · 周五复盘</span>
        {/* 关灯 / 午休：整张图压暗 */}
        {dim > 0 && <div className="iso-dim" style={{ opacity: dim, background: sc.stage === 'lunch' ? '#2a241d' : '#070b1c' }} />}
        {/* 每个人头顶的名牌：点了看完整汇报 */}
        {Object.entries(SPOTS).map(([role, [x, y]]) => {
          const st = stateOf(role)
          const tone = toneOf(role, data)
          const here = st !== 'gone'
          return (
            <button key={role} className={`iso-tag ${active === role ? 'is-on' : ''} ${here ? '' : 'gone'}`} style={pos(x, y)} onClick={() => onPick(role)}
              title={`${NAMES[role]}：点开看完整汇报`}>
              <i style={{ background: COLORS[role] }} />
              <span>{NAMES[role]}</span>
              {st === 'nap' || st === 'sofa' ? <em className="zz">zz</em>
                : <b className={here && tone ? 'on' : ''} style={{ background: here && tone ? LEVEL_DOT[tone] : '#5a5f68' }} />}
            </button>
          )
        })}
        {picked && speech ? (
          <div className={`d-sheet ${spot[0] < IW / 2 ? 'right' : 'left'}`} role="dialog" aria-label={`${NAMES[active]}的汇报`}>
            <header><b>{NAMES[active]}</b><span>{away ? '下班了 · 这是走之前说的' : '当面汇报'}</span><button onClick={() => onPick(active)} aria-label="关闭">×</button></header>
            <div className="d-sheet-body">
              <p className="lead">{event && event.role === active ? event.text : speech.short}</p>
              {speech.full.map((line, k) => <p key={k}>{line}</p>)}
              {!speech.full.length && <p className="dim">就这些，没有更多要报的。</p>}
            </div>
          </div>
        ) : bubble && (
          <>
            <div className="d-bubble" style={{ left: `${Math.min(84, Math.max(16, (spot[0] / IW) * 100))}%`, top: `${((spot[1] - 34) / IH) * 100}%` }}>{bubble}</div>
            <div className="d-tail" style={pos(spot[0], spot[1] - 34)} />
          </>
        )}
      </div>
      <div className="d-office-foot">
        <span className={`d-dot ${working ? 'work' : sc.stage === 'lunch' ? 'nap' : ''}`} />
        <span>{footOf(sc, phase, sc.present.length)}</span>
        <span className="d-office-hint">点名牌看完整汇报，滚轮往下翻；再点一次收起</span>
      </div>
    </div>
  )
}
