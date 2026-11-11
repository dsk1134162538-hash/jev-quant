import { SEATS, sceneOf, skyOf, speechOf } from './Office.jsx'

// 等距视角的像素办公室，第二版（dj 2026-11-12：「让你做一张这样的布局，没让你做一模一样的，用你的像素风做成动画，人物做可爱点」）。
// 上一版直接拿参考图当底图，人不会动——错了。这一版只借参考图的布局：左边一竖条是经理室（上）和会议室（下），用玻璃隔开；
// 右边大厅四张桌子，每张桌子两边对坐；右后墙挂行情屏、窗和机柜。所有东西都是自己画的 SVG，人是大头 Q 版小人，会眨眼、打字、
// 午睡、去饮水机聊天、周五进会议室。
// 坐标：地板是 NX×NY 的格子，(gx, gy) → 屏幕用 iso()；gx 往右下走，gy 往左下走，z 往上。一格半宽 HW、半高 HH。
// 画的顺序就是遮挡顺序：地板 → 墙 → 经理室 → 两间之间的玻璃 → 会议室 → 大厅前的玻璃 → 大厅（后排桌 → 过道 → 前排桌）。
const W = 600
const H = 344
const HW = 18
const HH = 9
const OX = 246
const OY = 64
const NX = 18
const NY = 12
const RX = 6            // gx < RX 是左边那一竖条房间
const RY = 6            // 房间里 gy < RY 是经理室，其余是会议室
const WALL = 54
const GLASS = 34

const iso = (gx, gy, z = 0) => [OX + (gx - gy) * HW, OY + (gx + gy) * HH - z]
const pts = (...p) => p.map(([x, y]) => `${x},${y}`).join(' ')
// 竖直的面：planeX 沿 gx 方向（面朝左下，本地 X = gx*HW），planeY 沿 gy 方向（面朝右下，本地 X = -gy*HW）；本地 Y = -z
const planeX = (gy) => `matrix(1 0.5 0 1 ${OX - gy * HW} ${OY + gy * HH})`
const planeY = (gx) => `matrix(1 -0.5 0 1 ${OX + gx * HW} ${OY + gx * HH})`
const FLOOR = `matrix(1 0.5 -1 0.5 ${OX} ${OY})`          // 贴在地上的字：本地 (gx*HW, gy*HW)
const shade = (hex, f) => `#${[1, 3, 5].map((i) => Math.max(0, Math.min(255, Math.round(parseInt(hex.slice(i, i + 2), 16) * f))).toString(16).padStart(2, '0')).join('')}`

const SKINS = ['#f6d2ad', '#ecc096', '#fbe0c3']
const MANAGER = { role: 'manager', name: '经理', color: '#2c3550', hair: 'side', hairColor: '#3a3632', skin: 1, glasses: true, body: 'suit' }
const RUNNER = { hair: 'short', hairColor: '#3a2a1e', skin: 0, color: '#5f86ad' }
const LOOKS = { manager: MANAGER, ...Object.fromEntries(SEATS.map((s) => [s.role, s])) }

// 四张桌子：x,y 是桌子靠后的那个角，桌面 4×2 格。far 一侧的人脸朝我们，near 一侧背对我们（屏幕朝我们，亮着 = 在干活）
const PODS = {
  A: { x: 7.6, y: 2.0, name: '汇报一组', tint: '#c8322b' }, B: { x: 13, y: 2.0, name: '汇报二组', tint: '#3b78b5' },
  C: { x: 7.6, y: 8.1, name: '策略组', tint: '#6aa84f' }, D: { x: 13, y: 8.1, name: '交易组', tint: '#d9a21b' },
}
const PLACE = {
  breadth: ['A', 'far', 0], ladder: ['A', 'far', 1], high: ['A', 'near', 0], movers: ['A', 'near', 1],
  scan: ['B', 'far', 0], book: ['B', 'far', 1], risk: ['B', 'near', 0],
  'strat:relay': ['C', 'far', 0], 'strat:wts': ['C', 'far', 1], 'strat:bz': ['C', 'near', 0],
  'trader:aiflex': ['D', 'near', 0], 'trader:aiflex2': ['D', 'near', 1],
}
const SPARE = [['B', 1], ['C', 1]]                         // near 一侧没人常坐的位子
const seatAt = (pod, side, col) => ({ gx: PODS[pod].x + 1 + col * 2, gy: side === 'far' ? PODS[pod].y - 0.45 : PODS[pod].y + 2.45, back: side === 'near' })
const HOME = { manager: { gx: 3.5, gy: 1.45, back: false }, ...Object.fromEntries(Object.entries(PLACE).map(([r, [p, s, c]]) => [r, seatAt(p, s, c)])) }
const MEET = { manager: { gx: 3.1, gy: 7.75 }, 'trader:aiflex': { gx: 2.3, gy: 10.3, back: true }, 'trader:aiflex2': { gx: 3.9, gy: 10.3, back: true } }
const BOARD = { gx: 1.0, gy: 2.7, back: true, stand: true }
const SOFA = { gx: 0.8, gy: 5.0, nap: true }
const CHAT = [{ gx: 16.0, gy: 6.1, stand: true, cup: true }, { gx: 16.9, gy: 6.75, stand: true, cup: true }]
const CHAT_PAIRS = [[0, 1], [2, 3], [4, 5], [7, 8], [10, 11]]
const TONES = { ok: ['#12261d', '#8fe0b4'], warn: ['#2a2212', '#f0c36a'], bad: ['#2a1412', '#ff8076'], calm: ['#131c26', '#8fb7e0'],
  up: ['#2a1412', '#ff8076'], down: ['#12261d', '#8fe0b4'] }
const BAR_H = [3, 6, 4, 7, 5]
const SKY = { day: ['#a9daf5', '#d8effb'], dawn: ['#f3b98f', '#fbe2c0'], dusk: ['#d9785a', '#f3c28f'], night: ['#141b3a', '#243061'] }
const REGIME_TONE = { 打板日: 'up', 趋势日: 'amber', 混沌日: 'flat', 空仓日: 'down' }
const minutesOf = (hhmm) => (hhmm ? +hhmm.slice(0, 2) * 60 + +hhmm.slice(3, 5) : -999)
const textW = (s, zh, en) => [...s].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? zh : en), 0)

function Box({ x, y, w, d, z0 = 0, z1, top, left, right }) {
  const a = iso(x, y, z1), b = iso(x + w, y, z1), c = iso(x + w, y + d, z1), e = iso(x, y + d, z1)
  const b0 = iso(x + w, y, z0), c0 = iso(x + w, y + d, z0), e0 = iso(x, y + d, z0)
  return (
    <g>
      <polygon points={pts(e, c, c0, e0)} fill={left || shade(top, 0.84)} />
      <polygon points={pts(b, c, c0, b0)} fill={right || shade(top, 0.7)} />
      <polygon points={pts(a, b, c, e)} fill={top} />
    </g>
  )
}

// 大头小人：14 宽，头 10 行、身子 6 行，站着再加 4 行腿。x,y 是左上角。back = 背对我们
function Chibi({ look, x, y, back, stand, nap, talk, typing, cup, write, paper, walk, i = 0 }) {
  const h = look.hairColor
  const s = SKINS[look.skin]
  const c = look.body === 'suit' ? '#2c3550' : look.color
  const k = look.glasses ? '#8a6238' : '#2b2f36'
  const out = []
  const R = (rx, ry, w, hh, f, cls, st) => out.push(<rect key={out.length} x={rx} y={ry} width={w} height={hh} fill={f} className={cls} style={st} />)
  const hair = look.hair
  if (stand) {
    if (walk) {
      out.push(<g key="la" className="px-leg">{[<rect key="1" x="3" y="16" width="2" height="3" fill="#3b3f46" />, <rect key="2" x="9" y="16" width="2" height="3" fill="#3b3f46" />,
        <rect key="3" x="2" y="19" width="3" height="1" fill="#22252b" />, <rect key="4" x="9" y="19" width="3" height="1" fill="#22252b" />]}</g>)
      out.push(<g key="lb" className="px-leg b">{[<rect key="1" x="5" y="16" width="2" height="3" fill="#3b3f46" />, <rect key="2" x="7" y="16" width="2" height="3" fill="#3b3f46" />,
        <rect key="3" x="4" y="19" width="6" height="1" fill="#22252b" />]}</g>)
    } else { R(4, 16, 2, 3, '#3b3f46'); R(8, 16, 2, 3, '#3b3f46'); R(3, 19, 3, 1, '#22252b'); R(8, 19, 3, 1, '#22252b') }
  }
  // 身子
  R(3, 10, 8, 1, c); R(2, 11, 10, 4, c); R(3, 15, 8, 1, c)
  if (look.body === 'vest') { R(2, 11, 2, 4, '#fbf8f2'); R(10, 11, 2, 4, '#fbf8f2') }
  if (back) {
    if (look.body === 'suit') R(5, 10, 4, 1, '#fbf8f2')
    R(1, 11, 1, 3, c, typing ? 'px-hand' : undefined, { animationDelay: `${-(i * 0.07)}s` })
    R(12, 11, 1, 3, c, typing ? 'px-hand' : undefined, { animationDelay: `${-(i * 0.07 + 0.16)}s` })
  } else {
    if (look.body === 'suit') { R(5, 10, 4, 3, '#fbf8f2'); R(6, 11, 2, 3, '#c8322b') } else R(6, 10, 2, 1, '#fbf8f2')
    R(2, 14, 2, 1, s, typing ? 'px-hand' : undefined, { animationDelay: `${-(i * 0.07)}s` })
    R(10, 14, 2, 1, s, typing ? 'px-hand' : undefined, { animationDelay: `${-(i * 0.07 + 0.16)}s` })
  }
  // 头
  if (back) {
    if (hair === 'crew') { R(3, 1, 8, 1, h); R(2, 2, 10, 1, h); R(1, 3, 12, 3, h); R(1, 6, 12, 2, s); R(2, 8, 10, 1, s) }
    else { R(3, 0, 8, 1, h); R(1, 1, 12, 1, h); R(0, 2, 14, 6, h); R(1, 8, 12, 1, h) }
    R(5, 9, 4, 1, s)
    if (hair === 'long') { R(0, 2, 14, 10, h); R(1, 12, 12, 1, h) }
    if (hair === 'bun') { R(5, -2, 4, 2, h); R(6, -1, 2, 1, shade(h, 1.5)) }
    if (hair === 'pony') { R(6, 4, 2, 1, '#e0574a'); R(6, 5, 2, 7, h) }
    if (look.headset) { R(3, -1, 8, 1, k); R(-1, 4, 2, 4, k); R(13, 4, 2, 4, k) }
  } else {
    R(2, 3, 10, 6, s); R(1, 5, 12, 4, s); R(3, 9, 8, 1, s)
    if (hair === 'crew') { R(3, 1, 8, 1, h); R(2, 2, 10, 1, h); R(1, 3, 1, 3, h); R(12, 3, 1, 3, h) }
    else {
      R(3, 0, 8, 1, h); R(1, 1, 12, 1, h); R(0, 2, 14, 2, h); R(0, 4, 1, 4, h); R(13, 4, 1, 4, h); R(1, 4, 1, 1, h); R(12, 4, 1, 1, h)
      if (hair === 'short') { R(4, 4, 1, 1, h); R(8, 4, 2, 1, h) }
      if (hair === 'side') { R(2, 4, 6, 1, h); R(2, 5, 2, 1, h) }
      if (hair === 'long') { R(0, 4, 2, 9, h); R(12, 4, 2, 9, h); R(2, 4, 3, 1, h) }
      if (hair === 'bun') { R(5, -2, 4, 2, h); R(2, 4, 2, 1, h); R(10, 4, 2, 1, h) }
      if (hair === 'pony') { R(13, 2, 2, 3, h); R(14, 5, 1, 4, h); R(2, 4, 4, 1, h) }
    }
    if (nap) { R(3, 7, 2, 1, '#2a2018'); R(9, 7, 2, 1, '#2a2018') }
    else {
      R(3, 6, 2, 2, '#2a2018'); R(9, 6, 2, 2, '#2a2018'); R(3, 6, 1, 1, '#ffffff'); R(9, 6, 1, 1, '#ffffff')
      R(3, 6, 2, 1, s, 'px-blink', { animationDelay: `${-((i * 1.3) % 5)}s` }); R(9, 6, 2, 1, s, 'px-blink', { animationDelay: `${-((i * 1.3) % 5)}s` })
    }
    R(1, 8, 2, 1, '#f4a39c'); R(11, 8, 2, 1, '#f4a39c')
    if (talk) R(6, 8, 2, 2, '#a8453c', 'px-mouth'); else R(6, 8, 2, 1, shade(s, 0.72))
    if (look.glasses) { R(2, 5, 4, 1, k); R(8, 5, 4, 1, k); R(5, 6, 4, 1, k); R(2, 8, 4, 1, k); R(8, 8, 4, 1, k) }
    if (look.headset) { R(3, -1, 8, 1, k); R(1, 0, 2, 1, k); R(11, 0, 2, 1, k); R(-1, 4, 2, 4, k); R(13, 4, 2, 4, k); R(-1, 8, 1, 2, k); R(0, 9, 3, 1, k) }
  }
  if (cup) { R(11, 12, 3, 3, '#ffffff'); R(12, 10, 1, 2, '#ffffff', 'd-steam') }
  if (paper) { R(11, 11, 4, 5, '#ffffff'); R(12, 12, 2, 1, '#c9c0ae'); R(12, 14, 2, 1, '#c9c0ae') }
  if (write) { R(12, 5, 2, 6, c, 'px-hand'); R(12, 3, 2, 2, s, 'px-hand'); R(13, 1, 1, 2, '#2a241d', 'px-hand') }
  return <g transform={`translate(${x},${y})`}>{out}</g>
}

// 一个人放到格子上：坐着的话屁股离地 5，站着脚踩地。talk = 正在说话（跳一跳、嘴巴动）
function Person({ look, at, talk, typing, i = 0, cup, write }) {
  const [sx, sy] = iso(at.gx, at.gy)
  const x = Math.round(sx) - 7
  const y = Math.round(sy) - (at.stand ? 20 : 21)
  const lean = at.nap ? (at.back ? 'translate(2px, 2px)' : 'translate(-2px, 3px)') : undefined
  return (
    <g>
      <ellipse cx={Math.round(sx)} cy={Math.round(sy)} rx="7" ry="2.5" fill="#2a241d" opacity="0.16" />
      <g style={lean ? { transform: lean } : undefined}>
        <g className={`d-char ${talk ? 'is-active' : at.nap ? 'px-breath' : 'look'}`} style={{ animationDelay: `${-((i * 1.7) % 9)}s` }}>
          <Chibi look={look} x={x} y={y} back={at.back} stand={at.stand} nap={at.nap} talk={talk} typing={typing} cup={cup || at.cup} write={write} i={i} />
        </g>
      </g>
      {at.nap && <><text className="d-zz" style={{ animationDelay: `${-i * 0.45}s` }} x={x + 13} y={y + 1}>z</text>
        <text className="d-zz" style={{ animationDelay: `${-i * 0.45 - 1.3}s` }} x={x + 16} y={y - 3}>z</text></>}
    </g>
  )
}

// 转椅。behind = 椅背在人后面（脸朝我们的人），先画；否则椅背挡在人前面（背对我们的人），后画
function ChairBack({ at, behind, big }) {
  const [sx, sy] = iso(at.gx, at.gy)
  const x = Math.round(sx)
  const y = Math.round(sy)
  const col = big ? '#5a3a2a' : '#4a4f57'
  return behind ? (
    <g><rect x={x - 1} y={y - 5} width="2" height="5" fill="#2b2f36" /><rect x={x - 5} y={y - 1} width="10" height="1" fill="#2b2f36" />
      <rect x={x - 6} y={y - 7} width="12" height="3" fill={shade(col, 1.25)} />
      <rect x={x - 3} y={y - (big ? 24 : 20)} width="12" height={big ? 17 : 13} fill={col} /><rect x={x - 3} y={y - (big ? 24 : 20)} width="12" height="1" fill={shade(col, 1.3)} /></g>
  ) : (
    <g><rect x={x - 2} y={y - 2} width="2" height="4" fill="#2b2f36" /><rect x={x - 6} y={y + 1} width="10" height="1" fill="#2b2f36" />
      <rect x={x - 6} y={y - 4} width="12" height="3" fill={shade(col, 1.25)} />
      <rect x={x - 9} y={y - 11} width="12" height="9" fill={col} /><rect x={x - 9} y={y - 11} width="12" height="1" fill={shade(col, 1.3)} /><rect x={x - 9} y={y - 11} width="1" height="9" fill={shade(col, 0.8)} /></g>
  )
}

// 显示器，立在沿 gx 方向的竖直面上。back = 我们看到的是背面（只有一颗状态灯）
function Mon({ gx, gy, z = 9.5, w = 16, h = 11, back, mode, tone, text, textTone, i = 0 }) {
  const X = Math.round(gx * HW - w / 2)
  const t = mode === 'on' ? TONES[tone] || TONES.calm : null
  const bars = Math.floor((w - 3) / 3)
  return (
    <g transform={planeX(gy)}>
      <rect x={X + w / 2 - 1} y={-z - 3} width="2" height="3" fill="#22252b" /><rect x={X + w / 2 - 3} y={-z - 1} width="6" height="1" fill="#22252b" />
      <rect x={X} y={-z - 3 - h} width={w} height={h} fill={back ? '#3d424b' : '#2b2f36'} />
      {back ? (
        <><rect x={X} y={-z - 3 - h} width={w} height="1" fill="#555b65" />
          <rect className={t ? 'd-led' : undefined} x={X + w - 4} y={-z - 6} width="2" height="2" fill={t ? t[1] : mode === 'saver' ? '#4d6a8a' : '#2b2f36'} /></>
      ) : (
        <><rect x={X + 1} y={-z - 2 - h} width={w - 2} height={h - 2} fill={t ? t[0] : mode === 'saver' ? '#161a20' : '#23272d'} />
          {t && !text && Array.from({ length: bars }, (_, k) => (
            <rect key={k} className="d-bar" style={{ animationDelay: `${-(i * 0.37 + k * 0.53)}s` }} x={X + 2 + k * 3} y={-z - 4 - BAR_H[k % 5]} width="2" height={BAR_H[k % 5]} fill={t[1]} />
          ))}
          {t && text && <text x={X + w / 2} y={-z - 6} textAnchor="middle" className="px-mon" fill={(TONES[textTone] || t)[1]}>{text}</text>}
          {mode === 'saver' && <rect className="d-led" style={{ animationDelay: `${-i * 0.7}s` }} x={X + 3 + (i % 3) * 3} y={-z - 9} width="2" height="2" fill="#4d6a8a" />}</>
      )}
    </g>
  )
}

function Plant({ gx, gy }) {
  const [x, y] = iso(gx, gy).map(Math.round)
  return (
    <g>
      <ellipse cx={x} cy={y} rx="6" ry="2.2" fill="#2a241d" opacity="0.16" />
      <rect x={x - 4} y={y - 7} width="8" height="7" fill="#efe8da" /><rect x={x - 5} y={y - 8} width="10" height="2" fill="#fffdf7" /><rect x={x + 2} y={y - 6} width="2" height="6" fill="#d9cfbc" />
      <g className="px-sway">
        <rect x={x - 1} y={y - 14} width="2" height="6" fill="#4f8a4a" />
        <rect x={x - 6} y={y - 17} width="5" height="6" fill="#4f8a4a" /><rect x={x + 1} y={y - 19} width="6" height="7" fill="#5c9c55" />
        <rect x={x - 3} y={y - 23} width="6" height="7" fill="#74b262" /><rect x={x - 8} y={y - 13} width="4" height="3" fill="#5c9c55" /><rect x={x + 5} y={y - 14} width="4" height="3" fill="#4f8a4a" />
        <rect x={x - 1} y={y - 21} width="2" height="2" fill="#9ad07f" />
      </g>
    </g>
  )
}

const CAT = ['o.....o', 'oo...oo', 'ooooooo', 'oeoooeo', 'ooopooo', 'wooooow', '.ooooo.', '.ooooo.', '.w...w.']
const CAT_PAL = { o: '#e8a04a', w: '#fff6e6', e: '#2a2018', p: '#f08a8a' }
function Cat({ gx, gy, asleep }) {
  const [x, y] = iso(gx, gy).map(Math.round)
  const out = []
  CAT.forEach((row, j) => [...row].forEach((ch, i) => { if (ch !== '.') out.push(<rect key={`${j}-${i}`} x={x - 3 + i} y={y - 9 + j} width="1" height="1" fill={asleep && ch === 'e' ? '#b9722a' : CAT_PAL[ch]} />) }))
  return (
    <g>
      <ellipse cx={x} cy={y} rx="5" ry="1.8" fill="#2a241d" opacity="0.16" />
      <g className={asleep ? undefined : 'px-wag'}><rect x={x + 4} y={y - 5} width="1" height="4" fill="#c9822f" /><rect x={x + 5} y={y - 7} width="1" height="3" fill="#c9822f" /></g>
      {out}
      {!asleep && <><rect className="px-blink" x={x - 2} y={y - 6} width="1" height="1" fill="#e8a04a" /><rect className="px-blink" x={x + 2} y={y - 6} width="1" height="1" fill="#e8a04a" /></>}
      {asleep && <text className="d-zz" x={x + 5} y={y - 9}>z</text>}
    </g>
  )
}

// 墙上的窗：外面的天色跟北京时间走
function Win({ x, w, sky, sun, seed = 0 }) {
  const [top, low] = SKY[sky]
  const night = sky === 'night'
  const sx = x + 4 + Math.round((w - 12) * (night ? 0.7 : sun))
  return (
    <g>
      <rect x={x - 2} y="-47" width={w + 4} height="32" fill="#fbf8f2" />
      <rect x={x} y="-45" width={w} height="28" fill={top} /><rect x={x} y="-29" width={w} height="12" fill={low} />
      {night ? [3, 11, 19, 27, 34].filter((k) => k < w - 2).map((k, j) => <rect key={k} className="d-led" style={{ animationDelay: `${-j * 0.5}s` }} x={x + k} y={-43 + ((j * 7 + seed) % 9)} width="1" height="1" fill="#fff8d8" />)
        : <rect className="px-cloud" x={x + 3 + seed} y="-41" width="9" height="3" fill="#ffffff" opacity="0.85" />}
      <rect x={sx} y={night ? -42 : -40 + Math.round(Math.abs(sun - 0.5) * 16)} width="5" height="5" fill={night ? '#f3efd8' : sky === 'day' ? '#ffe27a' : '#ffb35a'} />
      {[[0, 8, 9], [9, 6, 14], [16, 9, 7], [26, 7, 12], [34, 8, 9], [43, 6, 15], [50, 9, 8]].filter(([k, bw]) => k + bw <= w).map(([k, bw, bh], j) => (
        <g key={k}><rect x={x + k} y={-17 - bh} width={bw} height={bh} fill={night ? '#0c1126' : shade(low, 0.74)} />
          {night && (j + seed) % 2 === 0 && <rect x={x + k + 2} y={-15 - bh + 2} width="2" height="2" fill="#f0c36a" />}</g>
      ))}
      <rect x={x + Math.round(w / 2) - 1} y="-45" width="2" height="28" fill="#fbf8f2" /><rect x={x} y="-32" width={w} height="1" fill="#fbf8f2" />
      <rect x={x - 3} y="-16" width={w + 6} height="2" fill="#e3d8c2" />
    </g>
  )
}

function Glass({ plane, x, w }) {
  return (
    <g transform={plane}>
      <rect x={x} y={-GLASS} width={w} height={GLASS} fill="#bfe3f2" opacity="0.2" />
      <polygon points={`${x + w * 0.2},${-GLASS} ${x + w * 0.2 + 5},${-GLASS} ${x + w * 0.2 - 9},0 ${x + w * 0.2 - 14},0`} fill="#ffffff" opacity="0.18" />
      <rect x={x} y={-GLASS} width={w} height="2" fill="#6b7480" /><rect x={x} y="-1" width={w} height="1" fill="#6b7480" opacity="0.6" />
      <rect x={x} y={-GLASS} width="1.5" height={GLASS} fill="#6b7480" /><rect x={x + w - 1.5} y={-GLASS} width="1.5" height={GLASS} fill="#6b7480" />
    </g>
  )
}

function Tag({ x, y, name, color, dot, blink, zz, on, dim }) {
  const w = Math.ceil(textW(name, 5.5, 3.2)) + 10
  return (
    <g transform={`translate(${Math.round(x - w / 2)},${Math.round(y)})`} opacity={dim ? 0.45 : 1}>
      <rect width={w} height="8" fill={on ? '#c8322b' : '#1d1f26'} opacity="0.9" /><rect width="2" height="8" fill={color} />
      <text x="3.5" y="6.1" className="px-tag">{name}</text>
      {zz ? <text x={w - 5} y="6" className="px-tag z">z</text>
        : <rect className={blink ? 'd-led' : undefined} x={w - 4.5} y="2.5" width="3" height="3" fill={dot || '#5a5f68'} />}
    </g>
  )
}

const Dots = ({ x, y }) => (
  <g><rect x={x} y={y} width="13" height="7" fill="#ffffff" /><rect x={x + 2} y={y + 7} width="3" height="1" fill="#ffffff" /><rect x={x + 3} y={y + 8} width="1" height="1" fill="#ffffff" />
    {[0, 1, 2].map((k) => <rect key={k} className="d-led" style={{ animationDelay: `${-k * 0.4}s` }} x={x + 2 + k * 4} y={y + 3} width="2" height="2" fill="#2a241d" />)}</g>
)

function toneOf(role, data) {
  if (role === 'manager') return data?.manager ? 'calm' : null
  if (role.startsWith('strat:')) {
    const t = (data?.strategyDesk?.traders || []).find((x) => `strat:${x.key}` === role)
    return !t ? null : t.error ? 'bad' : t.picks.length ? 'ok' : 'calm'
  }
  if (role.startsWith('trader:')) return (data?.traders || []).some((x) => `trader:${x.sid}` === role) ? 'calm' : null
  const r = (data?.roles || []).find((x) => x.role === role)
  return r && r.level !== 'idle' ? r.level : null
}

function tickerOf(data) {
  const m = data?.manager
  const sign = (v) => `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`
  const parts = [m ? `经理 ${m.slot} ${m.regime}` : '经理 09:26 开会']
  ;(data?.traders || []).forEach((t) => { if (t.account?.today != null) parts.push(`${t.label} 今日 ${sign(t.account.today)}`) })
  if (data?.mgr?.account?.today != null) parts.push(`经理账户 今日 ${sign(data.mgr.account.today)}`)
  if (data?.stance) parts.push(`风险档位 ${data.stance.level}`)
  ;(data?.roles || []).filter((r) => r.level === 'bad' || r.level === 'warn').forEach((r) => parts.push(`${r.name}：${r.line}`))
  const text = parts.join('   ◆   ')
  return { text, width: Math.ceil(textW(text, 5.2, 2.9) / 20) * 20 }
}

function footOf(sc, phase, n) {
  if (sc.stage === 'closed') return '周末休市 · 办公室没人，只有猫在'
  if (sc.stage === 'empty') return sc.m < 525 ? '还没上班 · 08:45 开始到岗' : '下班了 · 灯关了，机柜里程序还在跑'
  if (sc.stage === 'arrive') return `盘前 · 陆续到岗 ${n}/13`
  if (sc.stage === 'lunch') return '午休 · 13:00 开工'
  if (sc.stage === 'review') return '收盘 · 经理在白板前复盘'
  if (sc.stage === 'leave') return sc.meeting ? '周五 · 经理和两位交易员在会议室开周复盘会，其他人陆续下班' : `收盘 · 陆续下班，还剩 ${n} 人`
  return `${phase} · 全员在岗`
}

export function IsoOffice({ data, active, picked, event, phase, onPick }) {
  const sc = sceneOf(data?.now)
  const { sky, sun } = skyOf(data?.now)
  const m = data?.manager
  const sd = data?.strategyDesk
  const roles = Object.fromEntries((data?.roles || []).map((r) => [r.role, r]))
  const traders = Object.fromEntries((data?.traders || []).map((t) => [`trader:${t.sid}`, t]))
  const todo = (sd?.final?.items || []).filter((i) => i.do)
  const hh = data?.now ? +data.now.slice(11, 13) : 12
  const mm = data?.now ? +data.now.slice(14, 16) : 0
  const working = ['work', 'arrive', 'review', 'leave'].includes(sc.stage)
  const lightsOn = working || sc.stage === 'lunch'
  const dark = lightsOn ? (sc.stage === 'lunch' ? 0.14 : 0) : sky === 'night' ? 0.58 : sky === 'day' ? 0.2 : 0.4

  // 真实事件：经理刚判断完的 8 分钟站到白板前；策略席刚汇总完的 12 分钟，跑腿手里拿着单子
  const mgrFresh = m?.ts && data?.now ? (new Date(data.now) - new Date(m.ts)) / 60000 : 999
  const where = (role) => (role === 'manager' && sc.stage === 'work' && mgrFresh >= 0 && mgrFresh < 8 ? 'board' : sc.where(role))
  const sinceDesk = sc.m - minutesOf(sd?.slot)
  const carrying = sinceDesk >= 0 && sinceDesk < 12 && (sd?.traders || []).some((t) => t.picks.length)
  const runner = sc.stage === 'work' || sc.stage === 'arrive' ? 'walk' : sc.stage === 'lunch' || sc.stage === 'review' || (sc.stage === 'leave' && sc.m < 990) ? 'stand' : 'gone'

  const people = ['manager', ...SEATS.map((s) => s.role)]
  const chatters = people.filter((r) => where(r) === 'chat')
  const atOf = (role) => {
    const st = where(role)
    if (st === 'meeting') return MEET[role]
    if (st === 'board') return BOARD
    if (st === 'sofa') return SOFA
    if (st === 'chat') return CHAT[chatters.indexOf(role) % CHAT.length]
    return { ...HOME[role], nap: st === 'nap' }
  }
  const info = Object.fromEntries(people.map((role) => {
    const st = where(role)
    const tone = toneOf(role, data)
    const atDesk = st === 'seat'
    return [role, { st, tone, at: atOf(role), here: st !== 'gone', home: st === 'seat' || st === 'nap',
      mode: atDesk && tone ? 'on' : st === 'gone' ? 'off' : 'saver' }]
  }))
  const idx = (role) => people.indexOf(role)

  const chat = sc.stage === 'work' ? CHAT_PAIRS[Math.floor(sc.m / 3) % (CHAT_PAIRS.length + 2)] : null
  const chatOn = chat && chat.every((k) => info[SEATS[k].role].st === 'seat' && SEATS[k].role !== active)

  const speech = active ? speechOf(active, data) : null
  const away = active && !info[active].here
  const anchor = (() => {
    if (!active) return null
    const at = away ? HOME[active] : info[active].at
    const [sx, sy] = iso(at.gx, at.gy)
    return [sx, sy - (at.back && !at.stand ? 24 : 33)]
  })()
  const who = active ? LOOKS[active].name : ''
  const bubble = !speech ? '' : event && event.role === active ? event.text : away ? `（人不在）走之前说的：${speech.short}` : speech.short

  const nums = (roles.breadth?.line || '').match(/涨 (\d+) \/ 跌 (\d+)/)
  const share = nums ? +nums[1] / (+nums[1] + +nums[2] || 1) : 0.5
  const ticker = tickerOf(data)
  const hand = (deg, len) => ({ x2: 120 + Math.sin((deg * Math.PI) / 180) * len, y2: -38 - Math.cos((deg * Math.PI) / 180) * len })

  const personOf = (role, extra = {}) => {
    const p = info[role]
    return <Person key={role} look={LOOKS[role]} at={p.at} talk={active === role && p.st !== 'nap' && p.st !== 'sofa'} typing={p.mode === 'on'} i={idx(role)} {...extra} />
  }
  const tagOf = (role) => {
    const p = info[role]
    const at = p.here ? p.at : HOME[role]
    const [sx, sy] = iso(at.gx, at.gy)
    const below = at.back && !at.stand
    const t = p.tone && p.here ? TONES[p.tone][1] : null
    return <Tag key={role} x={sx - (below ? 3 : 0)} y={below ? sy + 4 : sy - (at.stand ? 30 : 31)} name={LOOKS[role].name} color={role === 'manager' ? '#c9a13b' : LOOKS[role].color}
      dot={t} blink={!!t && p.st === 'seat'} zz={p.st === 'nap' || p.st === 'sofa'} on={active === role} dim={!p.here} />
  }

  const pod = (key) => {
    const P = PODS[key]
    const seats = Object.entries(PLACE).filter(([, [k]]) => k === key)
    const far = seats.filter(([, [, s]]) => s === 'far')
    const near = seats.filter(([, [, s]]) => s === 'near')
    const spare = SPARE.filter(([k]) => k === key).map(([, c]) => c)
    return (
      <g key={key}>
        <polygon points={pts(iso(P.x - 0.8, P.y - 1.3), iso(P.x + 4.8, P.y - 1.3), iso(P.x + 4.8, P.y + 3.6), iso(P.x - 0.8, P.y + 3.6))} fill={P.tint} opacity="0.13" />
        <polygon points={pts(iso(P.x - 0.8, P.y - 1.3), iso(P.x + 4.8, P.y - 1.3), iso(P.x + 4.8, P.y + 3.6), iso(P.x - 0.8, P.y + 3.6))} fill="none" stroke={P.tint} strokeWidth="0.8" opacity="0.4" />
        <g transform={FLOOR}><text x={(P.x - 0.6) * HW} y={(P.y + 3.42) * HW} className="px-floor" fill={shade(P.tint, 0.7)}>{P.name}</text></g>
        {far.map(([role]) => <ChairBack key={role} at={HOME[role]} behind />)}
        {far.filter(([role]) => info[role].home).map(([role]) => personOf(role))}
        <Box x={P.x + 0.15} y={P.y + 0.15} w={0.3} d={1.7} z1={7.5} top="#cfc5b2" />
        <Box x={P.x + 3.55} y={P.y + 0.15} w={0.3} d={1.7} z1={7.5} top="#cfc5b2" />
        <Box x={P.x} y={P.y} w={4} d={2} z0={7.5} z1={9.5} top="#f8f3e9" left="#d8cdb8" right="#c4b8a0" />
        <polygon points={pts(iso(P.x + 0.1, P.y + 0.98, 9.5), iso(P.x + 3.9, P.y + 0.98, 9.5), iso(P.x + 3.9, P.y + 1.02, 9.5), iso(P.x + 0.1, P.y + 1.02, 9.5))} fill="#d8cdb8" />
        {far.map(([role, [, , c]]) => <Mon key={role} gx={P.x + 0.62 + c * 2} gy={P.y + 0.55} back mode={info[role].mode} tone={info[role].tone} i={idx(role)} />)}
        {key === 'D' && (
          <g transform={planeX(P.y + 0.5)}>
            <rect x={(P.x + 2) * HW - 1} y="-13" width="2" height="3.5" fill="#22252b" />
            <rect x={(P.x + 0.7) * HW} y="-28" width={2.6 * HW} height="15" fill="#2b2f36" /><rect x={(P.x + 0.7) * HW + 1} y="-27" width={2.6 * HW - 2} height="13" fill="#0f1a16" />
            <text x={(P.x + 2) * HW} y="-18.4" textAnchor="middle" className="px-mon" fill="#f0c36a">DeepSeek ×{data?.calls ?? 0}</text>
          </g>
        )}
        {near.map(([role, [, , c]]) => {
          const p = info[role]
          const t = traders[role]
          const today = t?.account?.today
          const pnl = role.startsWith('trader:') && today != null ? `${today > 0 ? '+' : ''}${Number(today).toFixed(1)}` : null
          return (
            <g key={role}>
              <Mon gx={P.x + 1.38 + c * 2} gy={P.y + 1.45} w={pnl ? 19 : 16} mode={p.mode} tone={p.tone} text={pnl} textTone={today > 0 ? 'up' : today < 0 ? 'down' : 'calm'} i={idx(role)} />
              <Box x={P.x + 0.7 + c * 2} y={P.y + 1.62} w={0.6} d={0.24} z0={9.5} z1={10.2} top="#e4e0d6" />
              {idx(role) % 2 === 0 && <g><Box x={P.x + 0.3 + c * 2} y={P.y + 1.5} w={0.16} d={0.16} z0={9.5} z1={12.5} top="#ffffff" left="#e0574a" right="#b8443a" />
                {p.home && <rect className="d-steam" x={iso(P.x + 0.38 + c * 2, P.y + 1.58)[0] - 0.5} y={iso(P.x + 0.38 + c * 2, P.y + 1.58, 15)[1]} width="1" height="2" fill="#ffffff" />}</g>}
            </g>
          )
        })}
        {spare.map((c) => <Mon key={c} gx={P.x + 1.38 + c * 2} gy={P.y + 1.45} mode="off" />)}
        {near.filter(([role]) => info[role].home).map(([role]) => personOf(role))}
        {near.map(([role]) => <ChairBack key={role} at={HOME[role]} />)}
        {spare.map((c) => <ChairBack key={c} at={seatAt(key, 'near', c)} />)}
      </g>
    )
  }
  const chatDots = () => {
    const [a, b] = chat.map((k) => HOME[SEATS[k].role])
    const [x, y] = iso((a.gx + b.gx) / 2, (a.gy + b.gy) / 2)
    return <Dots x={Math.round(x) - 6} y={Math.round(y) - (a.back ? 34 : 44)} />
  }
  const hit = (role) => {
    const at = info[role].here ? info[role].at : HOME[role]
    const [sx, sy] = iso(at.gx, at.gy)
    return (
      <rect key={role} className={`d-hit ${active === role ? 'is-on' : ''}`} x={sx - 13} y={sy - 33} width="26" height="46" onClick={() => onPick(role)}>
        <title>{`${LOOKS[role].name}：点开看完整汇报`}</title>
      </rect>
    )
  }
  const mgr = info.manager
  const meeting = Object.keys(MEET).filter((r) => info[r].st === 'meeting')
  const outline = pts(iso(0, NY), iso(0, NY, WALL), iso(0, 0, WALL), iso(NX, 0, WALL), iso(NX, 0), iso(NX, NY))

  return (
    <div className="iso">
      <div className="iso-stage">
        <svg viewBox={`0 0 ${W} ${H}`} shapeRendering="crispEdges" role="img" aria-label="等距视角的交易台办公室：左边是经理室和会议室，右边大厅四张桌子，按北京时间上下班">
          <defs><clipPath id="px-led-clip"><rect x="136" y="-46" width="114" height="32" /></clipPath></defs>
          {/* 地板：先一整块楼板，再按房间上色 */}
          <Box x={0} y={0} w={NX} d={NY} z0={-7} z1={0} top="#ead9b8" left="#b59f78" right="#9c8762" />
          <polygon points={pts(iso(0, 0), iso(RX, 0), iso(RX, RY), iso(0, RY))} fill="#c9a87c" />
          <polygon points={pts(iso(0, RY), iso(RX, RY), iso(RX, NY), iso(0, NY))} fill="#b4c4cc" />
          {Array.from({ length: NX - 1 }, (_, k) => k + 1).map((g) => <line key={`x${g}`} x1={iso(g, 0)[0]} y1={iso(g, 0)[1]} x2={iso(g, NY)[0]} y2={iso(g, NY)[1]} stroke="#2a241d" strokeWidth="0.5" opacity="0.07" />)}
          {Array.from({ length: NY - 1 }, (_, k) => k + 1).map((g) => <line key={`y${g}`} x1={iso(0, g)[0]} y1={iso(0, g)[1]} x2={iso(NX, g)[0]} y2={iso(NX, g)[1]} stroke="#2a241d" strokeWidth="0.5" opacity="0.07" />)}
          <polygon points={pts(iso(1.6, 1.0), iso(5.4, 1.0), iso(5.4, 4.0), iso(1.6, 4.0))} fill="#2c3550" opacity="0.5" />
          <g transform={FLOOR}>
            <text x={1.3 * HW} y={5.75 * HW} className="px-floor" fill="#6d4a2e">经理室</text>
            <text x={0.5 * HW} y={11.75 * HW} className="px-floor" fill="#4a5f6c">会议室 · 周五复盘</text>
          </g>

          {/* 两面后墙 */}
          <polygon points={pts(iso(0, 0), iso(0, NY), iso(0, NY, WALL), iso(0, 0, WALL))} fill="#e9dfc8" />
          <polygon points={pts(iso(0, 0), iso(NX, 0), iso(NX, 0, WALL), iso(0, 0, WALL))} fill="#f5eedc" />
          <polygon points={pts(iso(0, NY, WALL), iso(0, 0, WALL), iso(NX, 0, WALL), iso(NX, 0, WALL + 3), iso(0, 0, WALL + 3), iso(0, NY, WALL + 3))} fill="#8a7658" />
          <g transform={planeY(0)}>
            <rect x={-NY * HW} y="-4" width={NY * HW} height="4" fill="#c9b994" />
            {/* 经理室的白板 */}
            <rect x="-78" y="-47" width="58" height="30" fill="#8a8f98" /><rect x="-76" y="-45" width="54" height="26" fill="#fdfcf8" /><rect x="-70" y="-17" width="42" height="2" fill="#6b7480" />
            <text x="-74" y="-39" className="px-board">{data?.now ? data.now.slice(5, 10).replace('-', '/') : '—'} {sc.stage === 'review' ? '复盘' : '晨会'}</text>
            <text x="-74" y="-29" className={`px-board big ${REGIME_TONE[m?.regime] || ''}`}>{m ? m.regime : '未开会'}</text>
            <text x="-74" y="-22" className="px-board">{data?.stance ? `档位 ${data.stance.level}` : ''}{m ? ` 可做 ${sd ? todo.length : '—'}` : ' 09:26 开会'}</text>
            {/* 会议室的电视 */}
            <rect x="-190" y="-45" width="56" height="29" fill="#22252b" /><rect x="-188" y="-43" width="52" height="25" fill={meeting.length ? '#14263a' : '#161a20'} />
            <text x="-162" y="-32" textAnchor="middle" className="px-mon" fill={meeting.length ? '#8fd0ff' : '#4d6a8a'}>{meeting.length ? '周复盘 · 进行中' : '周五 15:30'}</text>
            <text x="-162" y="-24" textAnchor="middle" className="px-mon" fill={meeting.length ? '#f0c36a' : '#4d6a8a'}>{data?.stance ? `风险档位 ${data.stance.level}` : '周复盘会'}</text>
            {meeting.length > 0 && [0, 1, 2, 3].map((k) => <rect key={k} className="d-bar" style={{ animationDelay: `${-k * 0.4}s` }} x={-150 + k * 4} y={-22 + (k % 2)} width="3" height={3 - (k % 2)} fill="#8fd0ff" />)}
          </g>
          <g transform={planeX(0)}>
            <rect x="0" y="-4" width={NX * HW} height="4" fill="#d6c7a4" />
            <Win x={24} w={60} sky={sky} sun={sun} />
            <Win x={262} w={34} sky={sky} sun={sun} seed={3} />
            {/* 挂钟 */}
            <circle cx="120" cy="-38" r="7" fill="#2a241d" /><circle cx="120" cy="-38" r="5.6" fill="#fdfcf8" />
            <line x1="120" y1="-38" {...hand(((hh % 12) + mm / 60) * 30, 3)} stroke="#2a241d" strokeWidth="1.2" />
            <line x1="120" y1="-38" {...hand(mm * 6, 4.6)} stroke="#2a241d" strokeWidth="0.7" />
            <rect x="119.5" y="-38.5" width="1" height="1" fill="#c8322b" />
            {/* 行情大屏 */}
            <rect x="132" y="-50" width="122" height="40" fill="#22252b" /><rect x="134" y="-48" width="118" height="36" fill="#08100c" />
            <rect x="132" y="-50" width="122" height="1" fill="#4a4f57" />
            <g clipPath="url(#px-led-clip)">
              {nums ? (
                <text x="138" y="-39" className="px-led big"><tspan className="up">涨 {nums[1]}</tspan><tspan dx="5" className="down">跌 {nums[2]}</tspan></text>
              ) : <text x="138" y="-39.5" className="px-led dim">{roles.breadth?.line || '宽度 · 等开盘'}</text>}
              <rect x="138" y="-36.5" width="108" height="2.5" fill="#2f8a4c" /><rect x="138" y="-36.5" width={Math.round(108 * share)} height="2.5" fill="#d8443b" />
              <text x="138" y="-28" className="px-led">{roles.ladder ? roles.ladder.line.split('｜')[0] : '梯队 · 等开盘'}</text>
              <text x="138" y="-21.5" className="px-led amber">{sd ? (todo.length ? `今日可做 ${todo.length} 只：${todo.map((i) => i.name).join('、')}` : `${sd.slot} 没有应该做的票`) : '今日可做 · 09:26 汇总'}</text>
              <text key={ticker.width} x="136" y="-15.2" className="px-led run" style={{ '--tw': `${ticker.width}px`, animationDuration: `${Math.round((118 + ticker.width) / 9)}s` }}>{ticker.text}</text>
            </g>
          </g>

          {/* 经理室 */}
          <Plant gx={5.3} gy={0.7} />
          <Box x={0.25} y={4.2} w={0.35} d={1.6} z1={13} top="#3b4668" />
          <Box x={0.6} y={4.2} w={0.9} d={1.6} z1={6} top="#4a5680" />
          <Box x={0.25} y={4.05} w={1.25} d={0.2} z1={9} top="#3b4668" /><Box x={0.25} y={5.75} w={1.25} d={0.2} z1={9} top="#3b4668" />
          {mgr.st === 'sofa' && personOf('manager')}
          <ChairBack at={HOME.manager} behind big />
          {mgr.home && personOf('manager')}
          <Box x={2.3} y={2.05} w={0.3} d={1.1} z1={7.5} top="#6d4128" /><Box x={4.5} y={2.05} w={0.3} d={1.1} z1={7.5} top="#6d4128" />
          <Box x={2.2} y={1.95} w={2.7} d={1.3} z0={7.5} z1={9.5} top="#a9744b" left="#7a4a2e" right="#62391f" />
          <Mon gx={3.1} gy={2.5} back mode={mgr.mode} tone="calm" i={5} />
          <Box x={4.2} y={2.75} w={0.5} d={0.14} z0={9.5} z1={12.5} top="#e3c15a" left="#c9a13b" right="#a8842c" />
          {Array.from({ length: Math.min(todo.length, 3) }, (_, k) => <Box key={k} x={2.45} y={2.6} w={0.45} d={0.4} z0={9.5 + k} z1={10.3 + k} top="#ffffff" left="#c9c0ae" right="#b5ab97" />)}
          {mgr.st === 'board' && personOf('manager', { write: true })}

          <Glass plane={planeX(RY)} x={0} w={RX * HW} />

          {/* 会议室 */}
          <Plant gx={5.3} gy={11.2} />
          {[2.3, 3.9].map((g) => <ChairBack key={g} at={{ gx: g, gy: 7.75 }} behind />)}
          {meeting.filter((r) => !MEET[r].back).map((r) => personOf(r))}
          <Box x={1.7} y={8.35} w={0.3} d={1.3} z1={7.5} top="#b7a98e" /><Box x={4.3} y={8.35} w={0.3} d={1.3} z1={7.5} top="#b7a98e" />
          <Box x={1.5} y={8.2} w={3.3} d={1.6} z0={7.5} z1={9.5} top="#e8d3ad" left="#c2a878" right="#a88f62" />
          {meeting.length > 0 && <><Box x={2.6} y={8.7} w={0.5} d={0.4} z0={9.5} z1={10.2} top="#ffffff" /><Box x={3.5} y={9.1} w={0.5} d={0.4} z0={9.5} z1={10.2} top="#ffffff" /></>}
          {meeting.filter((r) => MEET[r].back).map((r) => personOf(r))}
          {[2.3, 3.9].map((g) => <ChairBack key={g} at={{ gx: g, gy: 10.3 }} />)}
          {meeting.length > 0 && <Dots x={Math.round(iso(3.1, 9)[0]) - 6} y={Math.round(iso(3.1, 9)[1]) - 40} />}

          {/* 大厅前面的玻璃隔断，两个门洞 */}
          <Glass plane={planeY(RX)} x={-2.6 * HW} w={2.6 * HW} />
          <Glass plane={planeY(RX)} x={-8.5 * HW} w={4.6 * HW} />
          <Glass plane={planeY(RX)} x={-NY * HW} w={2.3 * HW} />

          {/* 大厅：靠墙的东西 → 后排两桌 → 过道 → 前排两桌 */}
          <Plant gx={6.7} gy={0.6} />
          <Box x={16.75} y={0.15} w={1.1} d={0.85} z1={36} top="#4a4f57" left="#2b2f36" right="#22252b" />
          <g transform={planeX(1.0)}>
            {[0, 1, 2, 3, 4].map((k) => (
              <g key={k}><rect x={16.85 * HW} y={-32 + k * 6} width={0.9 * HW} height="4" fill="#1a1d22" />
                <rect className="d-led" style={{ animationDelay: `${-k * 0.31}s` }} x={16.95 * HW} y={-31 + k * 6} width="2" height="2" fill="#7fd1a5" />
                <rect className="d-led fast" style={{ animationDelay: `${-k * 0.47}s` }} x={16.95 * HW + 4} y={-31 + k * 6} width="2" height="2" fill={k === 2 ? '#f0c36a' : '#7fd1a5'} /></g>
            ))}
          </g>
          {pod('A')}
          {pod('B')}
          {chatOn && chat[0] < 7 && chatDots()}

          <Box x={17.1} y={5.2} w={0.6} d={0.6} z1={14} top="#f4f1ea" left="#d8d3c8" right="#bfb9ab" />
          <Box x={17.2} y={5.3} w={0.4} d={0.4} z0={14} z1={21} top="#bfe3f2" left="#8fc7e0" right="#74b0cc" />
          {runner === 'walk' && (
            <g className="px-run">
              <ellipse cx={iso(7.0, 6.0)[0]} cy={iso(7.0, 6.0)[1]} rx="6" ry="2.2" fill="#2a241d" opacity="0.16" />
              <g className="px-bob">
                <g className="px-fwd"><Chibi look={RUNNER} x={iso(7.0, 6.0)[0] - 7} y={iso(7.0, 6.0)[1] - 20} stand walk paper={carrying} /></g>
                <g className="px-bwd"><Chibi look={RUNNER} x={iso(7.0, 6.0)[0] - 7} y={iso(7.0, 6.0)[1] - 20} stand walk back /></g>
              </g>
            </g>
          )}
          {runner === 'stand' && <Person look={RUNNER} at={{ gx: 16.6, gy: 5.5, stand: true, cup: true }} i={3} />}
          {chatters.map((r) => personOf(r))}
          {chatters.length > 1 && <Dots x={Math.round(iso(16.45, 6.4)[0]) - 6} y={Math.round(iso(16.45, 6.4)[1]) - 36} />}
          <Cat gx={12.3} gy={11.4} asleep={!lightsOn} />

          {pod('C')}
          {pod('D')}
          {chatOn && chat[0] >= 7 && chatDots()}
          <Plant gx={17.3} gy={11.3} />

          {/* 名牌：盖在所有家具上面 */}
          {people.map((r) => tagOf(r))}
          <Tag x={iso(12.3, 11.4)[0]} y={iso(12.3, 11.4)[1] + 3} name="招财" color="#e8a04a" dot="#e8a04a" zz={!lightsOn} />

          {/* 关灯：整间屋子压暗，机柜的灯还在闪（程序确实还在跑） */}
          {dark > 0 && <polygon points={outline} fill={sc.stage === 'lunch' ? '#2a241d' : '#0b1020'} opacity={dark} pointerEvents="none" />}
          {!lightsOn && (
            <g transform={planeX(1.0)}>{[0, 1, 2, 3, 4].map((k) => (
              <g key={k}><rect className="d-led" style={{ animationDelay: `${-k * 0.31}s` }} x={16.95 * HW} y={-31 + k * 6} width="2" height="2" fill="#7fd1a5" />
                <rect className="d-led fast" style={{ animationDelay: `${-k * 0.47}s` }} x={16.95 * HW + 4} y={-31 + k * 6} width="2" height="2" fill={k === 2 ? '#f0c36a' : '#7fd1a5'} /></g>
            ))}</g>
          )}
          {active && !away && <g className="d-char is-active"><polygon points={`${anchor[0] - 3},${anchor[1] - 5} ${anchor[0] + 3},${anchor[1] - 5} ${anchor[0]},${anchor[1] - 1}`} fill="#c8322b" /></g>}
          {people.map((r) => hit(r))}
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
        ) : bubble && (
          <>
            <div className="d-bubble" style={{ left: `${Math.min(84, Math.max(16, (anchor[0] / W) * 100))}%`, top: `${((anchor[1] - 6) / H) * 100}%` }}>{bubble}</div>
            <div className="d-tail" style={{ left: `${(anchor[0] / W) * 100}%`, top: `${((anchor[1] - 6) / H) * 100}%` }} />
          </>
        )}
      </div>
      <div className="d-office-foot">
        <span className={`d-dot ${working ? 'work' : sc.stage === 'lunch' ? 'nap' : ''}`} />
        <span>{footOf(sc, phase, sc.present.length)}</span>
        <span className="d-office-hint">点小人看完整汇报，滚轮往下翻；再点一次收起</span>
      </div>
    </div>
  )
}
