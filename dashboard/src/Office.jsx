import { memo } from 'react'

// 像素办公室（dj 2026-11-10：「像现实中真的办公区域」）。viewBox 400×176，一个 SVG 单位就是一个像素格。
// 后墙从左到右：茶水间 + 挂钟、窗、行情大屏、窗；地板上第一排是 7 个汇报席，第二排是 3 个策略席 + 2 个交易席 + 机柜；
// 右边隔出经理室。小人坐在桌子后面（先画人、再画桌子、最后画桌上的东西），所以渲染顺序不能随便调。

const W = 400
const H = 176
const ROW_A = 72
const ROW_B = 126
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
].map(([role, name, color, hair, hairColor, skin, glasses], i) => ({ role, name, color, hair, hairColor, skin, glasses, kind: 'report', x: 8 + i * 39, y: ROW_A, w: 39 }))
const STRAT_SEATS = [
  ['strat:relay', '接力', '#e0574a', 'side', '#2a2420', 1],
  ['strat:wts', '弱转强', '#4a8fd1', 'long', '#3a2a1e', 2],
  ['strat:bz', '补涨', '#6aa84f', 'short', '#4a3424', 0],
].map(([role, name, color, hair, hairColor, skin], i) => ({ role, name, color, hair, hairColor, skin, body: 'vest', kind: 'strat', x: 8 + i * 39, y: ROW_B, w: 39 }))
const TRADER_SEATS = [
  ['trader:aiflex', '交易员1', '#d9a21b', 'short', '#1d1a16', 1],
  ['trader:aiflex2', '交易员2', '#2f8a6c', 'long', '#5a3a22', 0],
].map(([role, name, color, hair, hairColor, skin], i) => ({ role, name, color, hair, hairColor, skin, headset: true, kind: 'trader', x: 146 + i * 48, y: ROW_B, w: 48 }))
export const SEATS = [...REPORT_SEATS, ...STRAT_SEATS, ...TRADER_SEATS]
const MANAGER = { color: '#2c3550', hair: 'side', hairColor: '#3a3632', skin: 1, glasses: true, body: 'suit' }
const MGR = { x: 344, y: 94 }

export const STRAT_INFO = [['relay', 'RELAY', '高标接力官'], ['wts', 'WTS', '弱转强官'], ['bz', 'BZ', '补涨官']]

export function traderLine(t) {
  const r = t.rounds?.[t.rounds.length - 1]
  if (!r) return '今天还没跑'
  if (r.skipped) return `${r.slot} ${r.skipped}`
  return `${r.slot} ${r.stage || '—'}｜${r.plan || ''}`
}

export function stratLine(t) {
  if (!t) return '等 09:26：规则先放行候选，我只能否决'
  if (t.error) return `出错：${t.error}`
  const head = `候选 ${t.candidates} · 放行 ${t.passed} · 荐 ${t.picks.length} · 否 ${t.vetoed.length}`
  return t.picks.length ? `${head}｜荐 ${t.picks.map((p) => p.name).join('、')}` : head
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

function Person({ look, x, y, mode, active, i }) {
  const skin = SKINS[look.skin]
  const pal = { h: look.hairColor, s: skin, m: shade(skin, 0.78), e: '#1d1a16', c: look.color, d: shade(look.color, 0.76), w: '#fbf8f2',
    k: look.glasses ? '#8a6238' : '#2b2f36', n: '#2c3550', N: '#1f2740', r: '#c8322b' }
  const head = look.glasses ? HEADS[look.hair].map((r, j) => (j === 4 ? GLASSES_ROW : r)) : HEADS[look.hair]
  const eyes = look.glasses ? [3, 8] : [4, 7]
  const napping = mode === 'nap' && !active
  return (
    <g transform={`translate(${x},${y})`}>
      <g className={`d-char ${active ? 'is-active' : napping ? 'nap' : 'look'}`} style={{ animationDelay: `${-((i * 1.7) % 9)}s` }}>
        <Pixels rows={head} pal={pal} />
        <Pixels rows={BODIES[look.body || 'shirt']} pal={pal} y={7} />
        {look.hair === 'long' && <Pixels rows={LONG_TAIL} pal={pal} y={7} />}
        {look.headset && <Pixels rows={HEADSET} pal={pal} />}
        {eyes.map((c) => (
          <rect key={c} className={napping ? 'd-lid shut' : 'd-lid'} style={{ animationDelay: `${-((i * 0.83) % 4.6)}s` }}
            x={c} y="4" width="1" height="1" fill={napping ? pal.m : skin} />
        ))}
      </g>
    </g>
  )
}

const TONES = { ok: ['#12261d', '#8fe0b4'], warn: ['#2a2212', '#f0c36a'], bad: ['#2a1412', '#ff8076'], calm: ['#131c26', '#8fb7e0'] }

function Monitor({ x, y, tone, i = 0, text }) {
  const t = TONES[tone]
  return (
    <g transform={`translate(${x},${y})`}>
      <rect width="14" height="10" fill="#2b2f36" />
      <rect x="1" y="1" width="12" height="8" fill={t ? t[0] : '#3b444c'} />
      {t && !text && [3, 6, 4].map((h, k) => (
        <rect key={k} className="d-bar" style={{ animationDelay: `${-(i * 0.37 + k * 0.53)}s` }} x={2 + k * 3} y={9 - h} width="2" height={h} fill={t[1]} />
      ))}
      {t && !text && <rect x="11" y="2" width="1" height="1" fill={t[1]} />}
      {t && text && <text x="7" y="7.4" textAnchor="middle" className="d-svg-mon" fill={t[1]}>{text}</text>}
      {!t && <rect x="2" y="2" width="3" height="1" fill="#55606a" />}
      <rect x="5" y="10" width="4" height="1" fill="#22252b" /><rect x="3" y="11" width="8" height="1" fill="#22252b" />
    </g>
  )
}

const LIGHT_DESK = { top: '#f6f1e7', hi: '#fffdf7', edge: '#cfc5b2', panel: '#e6ddcc', foot: '#bfb39c' }
const WALNUT_DESK = { top: '#9a6842', hi: '#b07e55', edge: '#6d4128', panel: '#7d4d30', foot: '#55301c' }

function Bench({ x, y, w, c = LIGHT_DESK, seams = [] }) {
  return (
    <g>
      <rect x={x + 2} y={y + 17} width={w} height="2" fill="#2a241d" opacity="0.16" />
      <rect x={x} y={y} width={w} height="8" fill={c.top} />
      <rect x={x} y={y} width={w} height="1" fill={c.hi} />
      <rect x={x} y={y + 8} width={w} height="1" fill={c.edge} />
      <rect x={x} y={y + 9} width={w} height="8" fill={c.panel} />
      <rect x={x} y={y + 16} width={w} height="1" fill={c.foot} />
      {seams.map((sx) => <rect key={sx} x={sx} y={y + 1} width="1" height="15" fill={c.edge} opacity="0.7" />)}
    </g>
  )
}

function Chair({ x, y, tall }) {
  return tall
    ? <g><rect x={x - 3} y={y - 2} width="18" height="15" fill="#3a2f2a" /><rect x={x - 2} y={y - 3} width="16" height="1" fill="#3a2f2a" /><rect x={x - 2} y={y - 1} width="16" height="1" fill="#51423a" /></g>
    : <g><rect x={x - 1} y={y + 5} width="14" height="8" fill="#4a4f57" /><rect x={x} y={y + 4} width="12" height="1" fill="#4a4f57" /><rect x={x} y={y + 5} width="12" height="1" fill="#5d636c" /></g>
}

// 桌上的东西：显示器、键盘、手、杯子 / 递上去的纸、名牌
function DeskItems({ s, info, mode, i }) {
  const skin = SKINS[s.skin]
  return (
    <g className={mode === 'work' && info.tone ? 'd-typing' : undefined}>
      <Monitor x={s.x + 3} y={s.y - 1} tone={info.tone} i={i} />
      {s.kind === 'trader' && (
        <Monitor x={s.x + 33} y={s.y - 1} tone={info.gate === '开' ? 'ok' : info.gate === '关' ? 'bad' : info.tone ? 'calm' : null}
          text={info.gate || (info.tone ? '—' : null)} />
      )}
      {info.alert && <rect className="d-led" x={s.x + 15} y={s.y - 3} width="2" height="2" fill="#e5463c" />}
      <rect x={s.x + 21} y={s.y + 13} width="10" height="3" fill="#d6cebf" /><rect x={s.x + 21} y={s.y + 15} width="10" height="1" fill="#bdb3a0" />
      <rect className="d-hand" style={{ animationDelay: `${-(i * 0.07)}s` }} x={s.x + 22} y={s.y + 12} width="2" height="2" fill={skin} />
      <rect className="d-hand b" style={{ animationDelay: `${-(i * 0.07 + 0.16)}s` }} x={s.x + 28} y={s.y + 12} width="2" height="2" fill={skin} />
      {s.kind === 'report' && <g><rect x={s.x + 34} y={s.y + 13} width="2" height="3" fill={shade(s.color, 0.9)} /><rect x={s.x + 36} y={s.y + 14} width="1" height="1" fill={shade(s.color, 0.9)} /></g>}
      {s.kind === 'strat' && Array.from({ length: Math.min(info.picks || 0, 3) }, (_, k) => (
        <g key={k}><rect x={s.x + 33 - (k % 2)} y={s.y + 16 - k * 2} width="5" height="1" fill="#ffffff" /><rect x={s.x + 33 - (k % 2)} y={s.y + 17 - k * 2} width="5" height="1" fill="#c9c0ae" /></g>
      ))}
      <rect x={s.x + 4} y={s.y + 22} width="2" height="5" fill={s.color} />
      <text x={s.x + 8} y={s.y + 26.4} className="d-svg-name">{s.name}</text>
    </g>
  )
}

// ---------- 静态背景（只跟天色有关，memo 掉） ----------
const SKY = {
  day: { bands: ['#8cc6ea', '#a9d6f0', '#c9e6f5'], far: '#a9c2d2', near: '#7f9db5', lit: null },
  dusk: { bands: ['#6f7fb8', '#e89a7a', '#f6cf9a'], far: '#9a7f9c', near: '#5d4f72', lit: '#ffd76a' },
  night: { bands: ['#141b33', '#1c2644', '#27335a'], far: '#1e2540', near: '#10152a', lit: '#ffd76a' },
}
const TOWERS = [[0, 7, 12], [8, 5, 18], [14, 8, 9], [23, 6, 22], [30, 9, 14], [40, 5, 19], [46, 8, 11]]

function Window({ x, y, w, h, id, sky, seed = 0, sun }) {
  const s = SKY[sky]
  const band = Math.ceil(h / 3)
  const towers = TOWERS.filter(([dx]) => dx < w)
  return (
    <g>
      <clipPath id={id}><rect x={x} y={y} width={w} height={h} /></clipPath>
      <rect x={x - 3} y={y - 3} width={w + 6} height={h + 6} fill="#b9a98c" />
      <rect x={x - 2} y={y - 2} width={w + 4} height={h + 4} fill="#fbf8f2" />
      <g clipPath={`url(#${id})`}>
        {s.bands.map((c, k) => <rect key={c} x={x} y={y + k * band} width={w} height={band} fill={c} />)}
        {sun && sky === 'day' && <rect x={x + w - 11} y={y + 7} width="5" height="5" fill="#fff6c0" />}
        {sun && sky === 'dusk' && <rect x={x + w - 13} y={y + h - 19} width="6" height="6" fill="#ffb347" />}
        {sun && sky === 'night' && <g><rect x={x + w - 11} y={y + 7} width="4" height="4" fill="#f4f0d8" /><rect x={x + w - 8} y={y + 7} width="1" height="1" fill={s.bands[0]} /></g>}
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

const Backdrop = memo(function Backdrop({ sky }) {
  const pool = sky === 'day' ? ['#fff6d0', 0.3] : sky === 'dusk' ? ['#ffb870', 0.22] : null
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
        <clipPath id="d-screen-clip"><rect x="122" y="7" width="108" height="46" /></clipPath>
      </defs>
      {/* 墙 */}
      <rect width={W} height="64" fill="#ece3d2" />
      <rect x="295" y="0" width="105" height="64" fill="#e2d6bf" />
      <rect width={W} height="3" fill="#d7cbb5" />
      {[74, 164, 254, 336].map((x) => <rect key={x} x={x} y="1" width="24" height="2" fill={sky === 'day' ? '#f6efdc' : '#fff8de'} />)}
      <rect y="54" width={W} height="10" fill="#e0d5c0" /><rect y="54" width={W} height="1" fill="#cfc2aa" /><rect y="62" width={W} height="2" fill="#b9a98c" />
      {/* 地板 + 经理室地毯 */}
      <rect y="64" width="292" height={H - 64} fill="url(#d-wood)" />
      <rect x="295" y="64" width="105" height={H - 64} fill="url(#d-carpet)" />
      <rect y="64" width={W} height="2" fill="#2a241d" opacity="0.1" />
      {pool && [[60, 52], [240, 44]].map(([x, w]) => (
        <polygon key={x} points={`${x + 4},66 ${x + w + 4},66 ${x + w + 24},124 ${x + 24},124`} fill={pool[0]} opacity={pool[1]} />
      ))}
      {pool && <polygon points="366,66 396,66 400,110 378,110" fill={pool[0]} opacity={pool[1] * 0.7} />}
      <text x="20" y="113" className="d-svg-zone">汇报席 · 代码算数，不做判断</text>
      <text x="20" y="169" className="d-svg-zone">策略席 · 规则放行，AI 只能否决</text>
      <text x="146" y="169" className="d-svg-zone">交易席 · AI 模拟盘</text>
      {/* 茶水间 */}
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
      <Window x={60} y={12} w={52} h={36} id="d-win-1" sky={sky} seed={1} sun />
      <Window x={240} y={12} w={44} h={36} id="d-win-2" sky={sky} seed={2} />
      <Window x={364} y={12} w={30} h={36} id="d-win-3" sky={sky} seed={3} />
      {/* 行情大屏 */}
      <rect x="119" y="4" width="114" height="52" fill="#3a3632" /><rect x="120" y="5" width="112" height="50" fill="#1d1a16" />
      <rect x="122" y="7" width="108" height="46" fill="#0f1a16" /><rect x="122" y="45" width="108" height="8" fill="#18291f" />
      <rect x="174" y="54" width="4" height="1" fill="#c8322b" />
      {/* 经理室：白板、门牌、沙发、绿植 */}
      <rect x="302" y="9" width="52" height="40" fill="#b9b4aa" /><rect x="304" y="11" width="48" height="36" fill="#fbfaf5" />
      <rect x="310" y="49" width="36" height="2" fill="#b9b4aa" />
      <rect x="314" y="48" width="4" height="1" fill="#c8322b" /><rect x="320" y="48" width="4" height="1" fill="#3b78b5" /><rect x="326" y="48" width="4" height="1" fill="#2a241d" />
      <rect x="312" y="152" width="54" height="14" fill="#7a4a3a" /><rect x="313" y="152" width="52" height="2" fill="#93604c" />
      <rect x="338" y="154" width="1" height="11" fill="#653a2d" /><rect x="309" y="156" width="4" height="13" fill="#653a2d" /><rect x="365" y="156" width="4" height="13" fill="#653a2d" />
      <rect x="312" y="166" width="54" height="3" fill="#55301f" /><rect x="311" y="169" width="58" height="2" fill="#2a241d" opacity="0.18" />
      <Pixels rows={PLANT} pal={PLANT_PAL} x={384} y={150} />
      {/* 玻璃隔断，中间留门 */}
      <rect x="291" y="56" width="4" height="44" fill="#8a7f70" /><rect x="292" y="58" width="2" height="40" fill="#cfe4ea" />
      <rect x="291" y="124" width="4" height={H - 124} fill="#8a7f70" /><rect x="292" y="126" width="2" height={H - 128} fill="#cfe4ea" />
      <rect x="290" y="98" width="6" height="2" fill="#5d554a" /><rect x="290" y="124" width="6" height="2" fill="#5d554a" /><rect x="291" y="100" width="4" height="24" fill="#c7b596" />
      <rect x="297" y="51" width="20" height="7" fill="#2a241d" /><text x="307" y="56.4" textAnchor="middle" className="d-svg-plate">经理室</text>
      {/* 机柜、复印机、墙角绿植 */}
      <rect x="254" y="156" width="18" height="2" fill="#2a241d" opacity="0.16" />
      <rect x="252" y="122" width="18" height="34" fill="#2b2f36" /><rect x="252" y="122" width="18" height="2" fill="#434851" />
      {[0, 1, 2, 3].map((i) => (
        <g key={i}>
          <rect x="254" y={126 + i * 7} width="14" height="5" fill="#181b20" />
          <rect className="d-led" style={{ animationDelay: `${-i * 0.31}s` }} x="255" y={128 + i * 7} width="1" height="1" fill="#7fd1a5" />
          <rect className="d-led fast" style={{ animationDelay: `${-i * 0.47}s` }} x="257" y={128 + i * 7} width="1" height="1" fill={i === 2 ? '#f0c36a' : '#7fd1a5'} />
          <rect x="260" y={128 + i * 7} width="7" height="1" fill="#2f353d" />
        </g>
      ))}
      <rect x="276" y="156" width="15" height="2" fill="#2a241d" opacity="0.16" />
      <rect x="274" y="136" width="15" height="20" fill="#ebe5d8" /><rect x="274" y="132" width="15" height="4" fill="#cfc8ba" /><rect x="274" y="132" width="15" height="1" fill="#ded7c9" />
      <rect x="276" y="140" width="11" height="1" fill="#a79d8b" /><rect x="278" y="139" width="7" height="2" fill="#ffffff" />
      <rect x="276" y="146" width="11" height="4" fill="#d6cfc1" /><rect className="d-led" x="286" y="137" width="1" height="1" fill="#7fd1a5" />
      <Pixels rows={PLANT} pal={PLANT_PAL} x={279} y={52} />
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
  ;(data?.roles || []).filter((r) => r.level === 'bad' || r.level === 'warn').forEach((r) => parts.push(`${r.name}：${r.line}`))
  const text = parts.join('   ◆   ')
  const width = [...text].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? 5 : 2.8), 0)
  return { text, width: Math.ceil(width / 20) * 20 }
}

export function Office({ data, active, phase, onPick }) {
  const roles = Object.fromEntries((data?.roles || []).map((r) => [r.role, r]))
  const traders = Object.fromEntries((data?.traders || []).map((t) => [`trader:${t.sid}`, t]))
  const sd = data?.strategyDesk
  const strats = Object.fromEntries((sd?.traders || []).map((t) => [`strat:${t.key}`, t]))
  const m = data?.manager
  const mode = phase === '盘中' || phase === '盘前' ? 'work' : phase === '午休' ? 'nap' : 'idle'
  const hh = data?.now ? +data.now.slice(11, 13) : 12
  const mm = data?.now ? +data.now.slice(14, 16) : 0
  const sky = hh >= 6 && hh < 17 ? 'day' : hh >= 17 && hh < 19 ? 'dusk' : 'night'
  const todo = (sd?.final?.items || []).filter((i) => i.do)

  const infoOf = (s) => {
    if (s.kind === 'report') {
      const r = roles[s.role]
      return { tone: r && r.level !== 'idle' ? r.level : null, alert: r?.level === 'bad', tip: r?.line || '经理开会时汇报' }
    }
    if (s.kind === 'strat') {
      const t = strats[s.role]
      return { tone: !t ? null : t.error ? 'bad' : t.picks.length ? 'ok' : 'calm', alert: !!t?.error, picks: t?.picks.length || 0, tip: stratLine(t) }
    }
    const t = traders[s.role]
    const d = t?.directive
    return { tone: t ? (t.rounds?.length ? 'ok' : 'calm') : null, gate: d ? (d.buy ? '开' : '关') : null, tip: t ? traderLine(t) : '今天还没跑' }
  }
  const infos = Object.fromEntries(SEATS.map((s) => [s.role, infoOf(s)]))
  const managerTip = m ? `${m.regime}｜${m.watch || m.read}` : '等 09:26 开会'

  const seat = SEATS.find((s) => s.role === active)
  const bubble = active === 'manager' ? managerTip : seat ? infos[seat.role].tip : ''
  const ax = active === 'manager' ? MGR.x + 6 : seat ? seat.x + 26 : 0
  const ay = active === 'manager' ? MGR.y - 4 : seat ? seat.y - 3 : 0

  const breadth = roles.breadth
  const nums = (breadth?.line || '').match(/涨 (\d+) \/ 跌 (\d+)/)
  const up = nums ? +nums[1] : 0
  const down = nums ? +nums[2] : 0
  const share = up + down ? up / (up + down) : 0.5
  const ticker = tickerOf(data)
  const hand = (deg, len) => ({ x2: 28 + Math.sin((deg * Math.PI) / 180) * len, y2: 13 - Math.cos((deg * Math.PI) / 180) * len })
  const walking = mode === 'work'
  const carrying = (sd?.traders || []).some((t) => t.picks.length)   // 有策略交易员递了票，跑单的手里才有纸

  const rowA = SEATS.filter((s) => s.y === ROW_A)
  const rowB = SEATS.filter((s) => s.y === ROW_B)
  const people = (list) => list.map((s) => {
    const i = SEATS.indexOf(s)
    return (
      <g key={s.role}>
        <Chair x={s.x + 20} y={s.y} />
        <Person look={s} x={s.x + 20} y={s.y} mode={mode} active={active === s.role} i={i} />
        {mode === 'nap' && active !== s.role && i % 2 === 0 && (
          <text className="d-zz" style={{ animationDelay: `${-i * 0.45}s` }} x={s.x + 33} y={s.y + 1}>z</text>
        )}
      </g>
    )
  })
  const items = (list) => list.map((s) => <DeskItems key={s.role} s={s} info={infos[s.role]} mode={mode} i={SEATS.indexOf(s)} />)

  return (
    <div className="d-office">
      <svg viewBox={`0 0 ${W} ${H}`} shapeRendering="crispEdges" role="img" aria-label="交易台办公室：汇报席、策略席、交易席和经理室，正在说话的岗位头顶有气泡">
        <Backdrop sky={sky} />
        {/* 挂钟指针 */}
        <line x1="28" y1="13" {...hand(((hh % 12) + mm / 60) * 30, 3.2)} stroke="#2a241d" strokeWidth="1.2" />
        <line x1="28" y1="13" {...hand(mm * 6, 5)} stroke="#2a241d" strokeWidth="0.7" />
        <rect x="27.5" y="12.5" width="1" height="1" fill="#c8322b" />
        {/* 大屏上的字 */}
        <g clipPath="url(#d-screen-clip)">
          {nums ? (
            <text x="126" y="17" className="d-svg-screen">
              <tspan className="up">涨 {up}</tspan><tspan dx="4" className="down">跌 {down}</tspan>
              <tspan dx="4" className="dim">{breadth.line.split('·').slice(1, 2).join('').trim()}</tspan>
            </text>
          ) : <text x="126" y="17" className="d-svg-screen dim">{breadth?.line || '宽度 · 等开盘'}</text>}
          <rect x="126" y="21" width="100" height="4" fill="#2f8a4c" />
          <rect x="126" y="21" width={Math.round(100 * share)} height="4" fill="#d8443b" />
          <text x="126" y="33" className="d-svg-screen small">{roles.ladder ? roles.ladder.line.split('｜')[0] : '梯队 · 等开盘'}</text>
          <text x="126" y="41" className="d-svg-screen small amber">
            {sd ? (todo.length ? `今日可做 ${todo.length} 只：${todo.map((i) => i.name).join('、')}` : `${sd.slot} 没有应该做的票`) : '今日可做 · 09:26 汇总'}
          </text>
          <text key={ticker.width} x="122" y="50.6" className="d-svg-screen small d-ticker"
            style={{ '--tw': `${ticker.width}px`, animationDuration: `${Math.round((110 + ticker.width) / 11)}s` }}>{ticker.text}</text>
        </g>
        {/* 白板 */}
        <text x="307" y="19" className="d-svg-board">{data?.now ? data.now.slice(5, 10).replace('-', '/') : '—'} 晨会</text>
        <text x="307" y="32" className={`d-svg-board big ${REGIME_TONE[m?.regime] || ''}`}>{m ? m.regime : '未开会'}</text>
        <text x="307" y="42" className="d-svg-board">{m ? `${m.slot} · 可做 ${sd ? todo.length : '—'} 只` : '09:26 开会'}</text>
        <text x="261" y="163" textAnchor="middle" className="d-svg-zone">DeepSeek ×{data?.calls ?? 0}</text>

        {/* 第一排：汇报席 */}
        {people(rowA)}
        <Bench x={6} y={ROW_A + 11} w={275} seams={rowA.slice(1).map((s) => s.x - 1)} />
        {items(rowA)}

        {/* 经理 */}
        <Chair x={MGR.x} y={MGR.y} tall />
        <Person look={MANAGER} x={MGR.x} y={MGR.y} mode={mode} active={active === 'manager'} i={13} />
        <Bench x={320} y={MGR.y + 11} w={60} c={WALNUT_DESK} />
        <Monitor x={324} y={MGR.y - 1} tone={m ? 'calm' : null} i={5} />
        <rect x="345" y={MGR.y + 13} width="10" height="3" fill="#2b2f36" /><rect x="340" y={MGR.y + 13} width="3" height="3" fill="#22252b" />
        <rect x="366" y={MGR.y + 4} width="9" height="3" fill="#2f8a6c" /><rect x="367" y={MGR.y + 4} width="7" height="1" fill="#57b592" />
        <rect x="370" y={MGR.y + 7} width="1" height="5" fill="#c9a13b" /><rect x="367" y={MGR.y + 12} width="7" height="1" fill="#c9a13b" />
        {Array.from({ length: Math.min(todo.length, 3) }, (_, k) => (
          <g key={k}><rect x={357 + (k % 2)} y={MGR.y + 16 - k * 2} width="6" height="1" fill="#ffffff" /><rect x={357 + (k % 2)} y={MGR.y + 17 - k * 2} width="6" height="1" fill="#c9c0ae" /></g>
        ))}
        <rect x="343" y={MGR.y + 22} width="14" height="6" fill="#c9a13b" /><text x="350" y={MGR.y + 26.8} textAnchor="middle" className="d-svg-name dark">经理</text>

        {/* 跑单的：盘中拿着票在策略席和经理室之间来回，其余时间端着咖啡站着 */}
        <g transform="translate(128,103)">
          <g className={walking ? 'd-runner' : undefined}>
            <g className={walking ? 'd-runner-in' : undefined}>
              <Pixels rows={RUNNER} pal={RUNNER_PAL} />
              {walking ? (
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

        {/* 第二排：策略席 + 交易席 */}
        {people(rowB)}
        <Bench x={6} y={ROW_B + 11} w={118} seams={[46, 85]} />
        <Bench x={144} y={ROW_B + 11} w={98} seams={[193]} />
        {items(rowB)}
        <Pixels rows={PLANT} pal={PLANT_PAL} x={4} y={158} />
        {sky === 'night' && <rect width={W} height={H} fill="#141b33" opacity="0.14" pointerEvents="none" />}

        {/* 点击区：整块工位都能点 */}
        {SEATS.map((s) => (
          <rect key={s.role} className={`d-hit ${active === s.role ? 'is-on' : ''}`} x={s.x} y={s.y - 3} width={s.w - 1} height="33" onClick={() => onPick(s.role)}>
            <title>{`${s.name}：${infos[s.role].tip}`}</title>
          </rect>
        ))}
        <rect className={`d-hit ${active === 'manager' ? 'is-on' : ''}`} x="320" y={MGR.y - 4} width="60" height="34" onClick={() => onPick('manager')}>
          <title>{`经理：${managerTip}`}</title>
        </rect>
      </svg>
      {bubble && (
        <>
          <div className="d-bubble" style={{ left: `${Math.min(79, Math.max(21, (ax / W) * 100))}%`, top: `${(ay / H) * 100}%` }}>{bubble}</div>
          <div className="d-tail" style={{ left: `${(ax / W) * 100}%`, top: `${(ay / H) * 100}%` }} />
        </>
      )}
      <div className="d-office-foot">
        <span className={`d-dot ${mode}`} />
        <span>{mode === 'work' ? `${phase} · 全员在岗` : mode === 'nap' ? '午休 · 13:00 开工' : `${phase} · 值守`}</span>
        <span className="d-office-hint">点任意工位，看 TA 在说什么；再点一次取消</span>
      </div>
    </div>
  )
}

const REGIME_TONE = { 打板日: 'up', 趋势日: 'amber', 混沌日: 'flat', 空仓日: 'down' }
