import { useEffect, useRef, useState } from 'react'
import { SEATS, sceneOf, skyOf, speechOf } from './Office.jsx'

// 等距视角的像素办公室，第三版。
// dj 2026-11-12：①「让你做一张这样的布局，没让你做一模一样的，用你的像素风做成动画，人物做可爱点」
//               ②「让这个东西可以旋转移动放大」「小人手会敲键盘，不是单纯坐在上面，每个人坐姿也不一样。再增加多一些的动画细节」
// 布局借参考图：左边一竖条是经理室（上）和会议室（下），玻璃隔开；右边大厅四张桌子，每桌两边对坐；墙上挂行情屏、窗、挂钟。
//
// 坐标：地板是 NX×NY 的格子，世界坐标 (gx, gy, z)。ROT 是当前朝向（0–3，每档转 90°）：rot() 把世界坐标转成「视图坐标」，
// scr() 再把视图坐标投到屏幕（视图 x 往右下、视图 y 往左下、z 往上）。所有东西都用世界坐标写，转视角时自动跟着变。
// 遮挡：地板和后墙先画；其余每样东西带一个占地矩形，sortItems() 按「谁完全在谁后面」排出先后；名牌和气泡最后盖上去。
// 竖直的面（墙、屏幕、玻璃）用 <Plane a b>：从正面看 a 在左、b 在右；转到背面时只画 back 那份。
const W = 600
const H = 344
const HW = 18
const HH = 9
const OY = 64
const NX = 18
const NY = 12
const RX = 6            // gx < RX 是左边那一竖条房间
const RY = 6            // 房间里 gy < RY 是经理室，其余是会议室
const WALL = 54
const GLASS = 34

let ROT = 0             // 只有一个办公室实例，渲染开头设一次，下面的投影函数都读它
const rot = (x, y) => (ROT === 0 ? [x, y] : ROT === 1 ? [NY - y, x] : ROT === 2 ? [NX - x, NY - y] : [y, NX - x])
const rotDir = (dx, dy) => (ROT === 0 ? [dx, dy] : ROT === 1 ? [-dy, dx] : ROT === 2 ? [-dx, -dy] : [dy, -dx])
const dims = () => (ROT % 2 ? [NY, NX] : [NX, NY])
const scr = (vx, vy, z = 0) => [30 + dims()[1] * HW + (vx - vy) * HW, OY + (vx + vy) * HH - z]
const iso = (gx, gy, z = 0) => scr(...rot(gx, gy), z)
const fpOf = (x, y, w, d) => { const [ax, ay] = rot(x, y); const [bx, by] = rot(x + w, y + d); return [Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay)] }
const pts = (...p) => p.map(([x, y]) => `${x},${y}`).join(' ')
const shade = (hex, f) => `#${[1, 3, 5].map((i) => Math.max(0, Math.min(255, Math.round(parseInt(hex.slice(i, i + 2), 16) * f))).toString(16).padStart(2, '0')).join('')}`
const R0 = (v) => Math.round(v)

const SKINS = ['#f6d2ad', '#ecc096', '#fbe0c3']
const MANAGER = { role: 'manager', name: '经理', color: '#2c3550', hair: 'side', hairColor: '#3a3632', skin: 1, glasses: true, body: 'suit' }
const RUNNER = { hair: 'short', hairColor: '#3a2a1e', skin: 0, color: '#5f86ad' }
const LOOKS = { manager: MANAGER, ...Object.fromEntries(SEATS.map((s) => [s.role, s])) }

// 四张桌子：x,y 是桌子的一个角，桌面 4×2 格，两条长边各坐两个人，面对面
const PODS = {
  A: { x: 7.6, y: 2.0, name: '汇报一组', tint: '#c8322b' }, B: { x: 13, y: 2.0, name: '汇报二组', tint: '#3b78b5' },
  C: { x: 7.6, y: 8.1, name: '策略组', tint: '#6aa84f' }, D: { x: 13, y: 8.1, name: '交易组', tint: '#d9a21b' },
}
const PLACE = {
  breadth: ['A', 'far', 0], ladder: ['A', 'far', 1], high: ['A', 'near', 0], movers: ['A', 'near', 1],
  scan: ['B', 'far', 0], book: ['B', 'far', 1], risk: ['B', 'near', 0], backtest: ['B', 'near', 1],
  'strat:relay': ['C', 'far', 0], 'strat:wts': ['C', 'far', 1], 'strat:bz': ['C', 'near', 0],
  'trader:aiflex': ['D', 'near', 0], 'trader:aiflex2': ['D', 'near', 1],
}
const SPARE = [['C', 1]]                                   // near 一侧没人常坐的位子（汇报二组那个空位 11-12 给了回测员）
// f 是这个人脸朝的方向（世界坐标）：far 一侧朝 +gy，near 一侧朝 -gy，都是冲着桌子
const seatAt = (pod, side, col) => ({ gx: PODS[pod].x + 1 + col * 2, gy: side === 'far' ? PODS[pod].y - 0.2 : PODS[pod].y + 2.2, f: [0, side === 'far' ? 1 : -1] })
const HOME = { manager: { gx: 3.5, gy: 1.72, f: [0, 1] }, ...Object.fromEntries(Object.entries(PLACE).map(([r, [p, s, c]]) => [r, seatAt(p, s, c)])) }
const MEET_SEATS = [{ gx: 2.3, gy: 7.95, f: [0, 1] }, { gx: 3.9, gy: 7.95, f: [0, 1] }, { gx: 2.3, gy: 10.05, f: [0, -1] }, { gx: 3.9, gy: 10.05, f: [0, -1] }]
const MEET = { manager: MEET_SEATS[0], 'trader:aiflex': MEET_SEATS[2], 'trader:aiflex2': MEET_SEATS[3] }
const BOARD = { gx: 1.0, gy: 2.7, f: [-1, 0], stand: true }
const SOFA = { gx: 0.95, gy: 5.0, f: [1, 0], nap: true, sofa: true }
const CHAT = [{ gx: 16.0, gy: 6.1, f: [0, 1], stand: true, cup: true }, { gx: 16.9, gy: 6.75, f: [0, -1], stand: true, cup: true }]
const CHAT_PAIRS = [[0, 1], [2, 3], [4, 5], [7, 8], [10, 11]]
const CAT_SPOTS = [[12.3, 11.4], [6.8, 6.4], [17.3, 3.7], [5.0, 5.3]]
// 每个人的坐姿不一样。k：type 双手打字｜mouse 一手键盘一手鼠标｜chin 托腮｜sip 时不时喝一口｜write 低头写字｜phone 打电话｜cross 抱着胳膊
// hx 歪头（像素），lean 身子往桌子探（正）或往后靠（负）
const POSE = {
  manager: { k: 'chin' }, breadth: { k: 'type' }, ladder: { k: 'phone', hx: 1 }, high: { k: 'chin', hx: -1 }, movers: { k: 'type', lean: 1 },
  scan: { k: 'sip' }, book: { k: 'write', hx: -1, lean: 1 }, risk: { k: 'cross', lean: -1 }, backtest: { k: 'mouse', lean: 1 },
  'strat:relay': { k: 'mouse' }, 'strat:wts': { k: 'type', hx: 1 }, 'strat:bz': { k: 'sip', lean: -1 },
  'trader:aiflex': { k: 'type', lean: 1 }, 'trader:aiflex2': { k: 'mouse', hx: -1 },
}
const TONES = { ok: ['#12261d', '#8fe0b4'], warn: ['#2a2212', '#f0c36a'], bad: ['#2a1412', '#ff8076'], calm: ['#131c26', '#8fb7e0'],
  up: ['#2a1412', '#ff8076'], down: ['#12261d', '#8fe0b4'] }
const BAR_H = [3, 6, 4, 7, 5]
const SKY = { day: ['#a9daf5', '#d8effb'], dawn: ['#f3b98f', '#fbe2c0'], dusk: ['#d9785a', '#f3c28f'], night: ['#141b3a', '#243061'] }
const REGIME_TONE = { 打板日: 'up', 趋势日: 'amber', 混沌日: 'flat', 空仓日: 'down' }
const minutesOf = (hhmm) => (hhmm ? +hhmm.slice(0, 2) * 60 + +hhmm.slice(3, 5) : -999)
const textW = (s, zh, en) => [...s].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? zh : en), 0)
const facing = (f) => { const [a, b] = rotDir(...f); return { front: a + b > 0, side: Math.sign(a - b) || 1 } }   // front 脸朝我们；side 脸偏屏幕左(-1)还是右(1)

// 谁先画：A 在视图 x 或视图 y 上完全位于 B 的后面，A 就先画。排不出先后的按中心深度
function sortItems(items) {
  const v = items.filter(Boolean).map((it) => { const [x, y, w, d] = fpOf(...it.fp); return { it, x0: x, x1: x + w, y0: y, y1: y + d, c: x + w / 2 + y + d / 2, out: [], n: 0 } })
  const E = 0.02
  const behind = (a, b) => a.x1 <= b.x0 + E || a.y1 <= b.y0 + E
  for (let i = 0; i < v.length; i += 1) for (let j = i + 1; j < v.length; j += 1) {
    const ab = behind(v[i], v[j])
    const ba = behind(v[j], v[i])
    if (ab && !ba) { v[i].out.push(v[j]); v[j].n += 1 } else if (ba && !ab) { v[j].out.push(v[i]); v[i].n += 1 }
  }
  const left = new Set(v)
  const res = []
  while (left.size) {
    let pick = null
    let free = false
    left.forEach((q) => { const ok = q.n <= 0; if (!pick || (ok && !free) || (ok === free && q.c < pick.c)) { pick = q; free = ok } })
    left.delete(pick)
    pick.out.forEach((b) => { b.n -= 1 })
    res.push(pick.it)
  }
  return res
}
const byDepth = (subs) => subs.filter(Boolean).map((s, i) => ({ s, i, d: rot(...s.p).reduce((a, b) => a + b, 0) })).sort((a, b) => a.d - b.d || a.i - b.i).map((x) => x.s.node)

function Box({ x, y, w, d, z0 = 0, z1, top, left, right }) {
  const [vx, vy, vw, vd] = fpOf(x, y, w, d)
  const a = scr(vx, vy, z1), b = scr(vx + vw, vy, z1), c = scr(vx + vw, vy + vd, z1), e = scr(vx, vy + vd, z1)
  const b0 = scr(vx + vw, vy, z0), c0 = scr(vx + vw, vy + vd, z0), e0 = scr(vx, vy + vd, z0)
  return (
    <g>
      <polygon points={pts(e, c, c0, e0)} fill={left || shade(top, 0.84)} />
      <polygon points={pts(b, c, c0, b0)} fill={right || shade(top, 0.7)} />
      <polygon points={pts(a, b, c, e)} fill={top} />
    </g>
  )
}

// 竖直的面。本地坐标：X 从左端往右（单位是像素，一格 = HW），Y = -z。children 是正面，back 是转到背面时看到的
function Plane({ a, b, back, children }) {
  const va = rot(...a)
  const vb = rot(...b)
  const alongX = Math.abs(va[1] - vb[1]) < 1e-6
  const front = alongX ? va[0] < vb[0] : va[1] > vb[1]
  const content = front ? children : back
  if (!content) return null
  const [e, f] = scr(...(front ? va : vb))
  return <g transform={`matrix(1 ${alongX ? 0.5 : -0.5} 0 1 ${e} ${f})`}>{content}</g>
}
// 贴在地上的字：放在一块占地矩形靠近我们的左下边
function FloorText({ fp, className, fill, children }) {
  const [vx, vy, , vd] = fpOf(...fp)
  const [e, f] = scr(0, 0)
  return <g transform={`matrix(1 0.5 -1 0.5 ${e} ${f})`}><text x={(vx + 0.25) * HW} y={(vy + vd - 0.18) * HW} className={className} fill={fill}>{children}</text></g>
}

// 大头小人：14 宽，头 10 行、身子 6 行，站着再加 4 行腿。x,y 是左上角。back = 背对我们。desk = 坐在桌前（手臂另外画在桌面上）
function Chibi({ look, x, y, back, stand, nap, talk, cup, write, paper, walk, pose, hx = 0, stretch, desk, i = 0 }) {
  const h = look.hairColor
  const s = SKINS[look.skin]
  const c = look.body === 'suit' ? '#2c3550' : look.color
  const k = look.glasses ? '#8a6238' : '#2b2f36'
  const out = []
  let ox = 0
  const R = (rx, ry, w, hh, f, cls, st) => out.push(<rect key={out.length} x={rx + ox} y={ry} width={w} height={hh} fill={f} className={cls} style={st} />)
  const hair = look.hair
  if (stand) {
    if (walk) {
      out.push(<g key="la" className="px-leg"><rect x="3" y="16" width="2" height="3" fill="#3b3f46" /><rect x="9" y="16" width="2" height="3" fill="#3b3f46" />
        <rect x="2" y="19" width="3" height="1" fill="#22252b" /><rect x="9" y="19" width="3" height="1" fill="#22252b" /></g>)
      out.push(<g key="lb" className="px-leg b"><rect x="5" y="16" width="2" height="3" fill="#3b3f46" /><rect x="7" y="16" width="2" height="3" fill="#3b3f46" />
        <rect x="4" y="19" width="6" height="1" fill="#22252b" /></g>)
    } else { R(4, 16, 2, 3, '#3b3f46'); R(8, 16, 2, 3, '#3b3f46'); R(3, 19, 3, 1, '#22252b'); R(8, 19, 3, 1, '#22252b') }
  }
  // 身子
  R(3, 10, 8, 1, c); R(2, 11, 10, 4, c); R(3, 15, 8, 1, c)
  if (look.body === 'vest') { R(2, 11, 2, 4, '#fbf8f2'); R(10, 11, 2, 4, '#fbf8f2') }
  if (back) {
    if (look.body === 'suit') R(5, 10, 4, 1, '#fbf8f2')
    if (!desk) { R(1, 11, 1, 3, c); R(12, 11, 1, 3, c) }
  } else {
    if (look.body === 'suit') { R(5, 10, 4, 3, '#fbf8f2'); R(6, 11, 2, 3, '#c8322b') } else R(6, 10, 2, 1, '#fbf8f2')
    if (!desk) { R(2, 14, 2, 1, s); R(10, 14, 2, 1, s) }
    if (pose === 'cross' && !nap) { R(2, 12, 10, 2, shade(c, 0.8)); R(2, 12, 2, 1, s); R(10, 13, 2, 1, s) }
  }
  if (stretch) { R(0, 2, 2, 9, c, 'd-arms'); R(12, 2, 2, 9, c, 'd-arms'); R(0, 0, 2, 2, s, 'd-arms'); R(12, 0, 2, 2, s, 'd-arms') }
  // 头（歪头就是整颗头横移一个像素）
  ox = hx
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
    if (!nap && pose === 'chin') { R(9, 10, 2, 4, shade(c, 0.82)); R(8, 9, 3, 2, s) }
    if (!nap && pose === 'phone') { R(10, 9, 2, 5, shade(c, 0.82)); R(12, 4, 2, 6, '#2b2f36'); R(11, 8, 2, 2, s); R(12, 5, 1, 1, '#7fd1a5', 'd-led') }
    if (!nap && pose === 'sip') {
      out.push(<g key="sip" className="px-sip" style={{ animationDelay: `${-(i * 2.3) % 11}s` }}>
        <rect x={9 + ox} y="10" width="2" height="4" fill={shade(c, 0.82)} /><rect x={5 + ox} y="7" width="4" height="4" fill="#ffffff" /><rect x={9 + ox} y="8" width="1" height="2" fill="#ffffff" /><rect x={8 + ox} y="10" width="2" height="1" fill={s} /></g>)
    }
  }
  ox = 0
  if (cup) { R(11, 12, 3, 3, '#ffffff'); R(12, 10, 1, 2, '#ffffff', 'd-steam') }
  if (paper) { R(11, 11, 4, 5, '#ffffff'); R(12, 12, 2, 1, '#c9c0ae'); R(12, 14, 2, 1, '#c9c0ae') }
  if (write) { R(12, 5, 2, 6, c, 'px-hand'); R(12, 3, 2, 2, s, 'px-hand'); R(13, 1, 1, 2, '#2a241d', 'px-hand') }
  return <g transform={`translate(${x},${y})`}>{out}</g>
}

// 一个人放到格子上。坐着屁股离地 5，沙发上 6；站着脚踩地。emote：! 报警｜heart 赚钱｜sweat 亏钱
function Person({ look, at, talk, i = 0, write, pose, stretch, emote, desk, turn = 0 }) {
  const { front, side } = facing(at.f)
  const [sx, sy] = iso(at.gx, at.gy).map(R0)
  const x = sx - 7
  const y = sy - (at.stand ? 20 : at.sofa ? 22 : 21)
  const P = pose || {}
  const lean = at.nap ? 2 : P.lean || 0
  const shift = at.sofa ? undefined : lean ? `translate(${side * lean}px, ${at.nap ? (front ? 3 : 1) : 0}px)` : undefined
  return (
    <g>
      {!at.sofa && <ellipse cx={sx} cy={sy} rx="7" ry="2.5" fill="#2a241d" opacity="0.16" />}
      <g style={shift ? { transform: shift } : undefined}>
        <g className={`d-char ${talk ? 'is-active' : at.nap ? 'px-breath' : 'look'}`} style={{ animationDelay: `${-((i * 1.7) % 9)}s` }}>
          <Chibi look={look} x={x} y={y} back={!front} stand={at.stand} nap={at.nap} talk={talk} cup={at.cup} write={write} pose={desk ? P.k : null}
            hx={at.nap ? 0 : (P.hx || 0) + turn} stretch={stretch} desk={desk} i={i} />
        </g>
      </g>
      {at.nap && <><text className="d-zz" style={{ animationDelay: `${-i * 0.45}s` }} x={x + 13} y={y + 1}>z</text>
        <text className="d-zz" style={{ animationDelay: `${-i * 0.45 - 1.3}s` }} x={x + 16} y={y - 3}>z</text></>}
      {emote === 'alert' && <g className="d-char is-active"><rect x={sx + 9} y={y - 2} width="2" height="5" fill="#e5463c" /><rect x={sx + 9} y={y + 4} width="2" height="2" fill="#e5463c" /></g>}
      {emote === 'heart' && <g className="px-float" style={{ animationDelay: `${-i * 0.6}s` }}><rect x={sx + 8} y={y} width="2" height="2" fill="#ff6b7a" /><rect x={sx + 11} y={y} width="2" height="2" fill="#ff6b7a" />
        <rect x={sx + 8} y={y + 1} width="5" height="2" fill="#ff6b7a" /><rect x={sx + 9} y={y + 3} width="3" height="1" fill="#ff6b7a" /><rect x={sx + 10} y={y + 4} width="1" height="1" fill="#ff6b7a" /></g>}
      {emote === 'sweat' && <g className="px-drip"><rect x={sx + 8} y={y + 3} width="1" height="1" fill="#7fc4ee" /><rect x={sx + 7} y={y + 4} width="3" height="2" fill="#7fc4ee" /></g>}
    </g>
  )
}

// 转椅：椅背总在人背后。人脸朝我们时椅背先画（在人后面），背对我们时椅背后画（挡在人前面，做矮一点好露出肩膀）
function Chair({ at, big, part }) {
  const { front, side } = facing(at.f)
  const [x, y] = iso(at.gx, at.gy).map(R0)
  const col = big ? '#5a3a2a' : '#4a4f57'
  const cx = x - side * 3
  if (front !== (part === 'behind')) return null
  return front ? (
    <g><rect x={x - 1} y={y - 5} width="2" height="5" fill="#2b2f36" /><rect x={x - 5} y={y - 1} width="10" height="1" fill="#2b2f36" />
      <rect x={x - 6} y={y - 7} width="12" height="3" fill={shade(col, 1.25)} />
      <rect x={cx - 6} y={y - (big ? 24 : 20)} width="12" height={big ? 17 : 13} fill={col} /><rect x={cx - 6} y={y - (big ? 24 : 20)} width="12" height="1" fill={shade(col, 1.3)} /></g>
  ) : (
    <g><rect x={x - 1} y={y - 2} width="2" height="4" fill="#2b2f36" /><rect x={x - 5} y={y + 1} width="10" height="1" fill="#2b2f36" />
      <rect x={x - 6} y={y - 4} width="12" height="3" fill={shade(col, 1.25)} />
      <rect x={cx - 6} y={y - 9} width="12" height="6" fill={col} /><rect x={cx - 6} y={y - 9} width="12" height="1" fill={shade(col, 1.3)} /></g>
  )
}
const Seat = ({ at, big, children }) => <g><Chair at={at} big={big} part="behind" />{children}<Chair at={at} big={big} part="front" /></g>

// 显示器。face = 屏幕朝世界 +gy(1) 还是 -gy(-1)；转到背面只看到外壳和一颗状态灯。kind：bars 柱子｜candle K 线｜lines 一行行字
function Mon({ gx, gy, face = 1, z = 9.5, w = 16, h = 11, mode, tone, text, textTone, kind = 'bars', i = 0 }) {
  const hw = w / 2 / HW
  const t = mode === 'on' ? TONES[tone] || TONES.calm : null
  const stand = <><rect x={w / 2 - 1} y={-z - 3} width="2" height="3" fill="#22252b" /><rect x={w / 2 - 3} y={-z - 1} width="6" height="1" fill="#22252b" /></>
  const a = [gx - face * hw, gy]
  const b = [gx + face * hw, gy]
  return (
    <Plane a={a} b={b} back={(
      <>{stand}<rect x="0" y={-z - 3 - h} width={w} height={h} fill="#3d424b" /><rect x="0" y={-z - 3 - h} width={w} height="1" fill="#555b65" />
        <rect className={t ? 'd-led' : undefined} x={w - 4} y={-z - 6} width="2" height="2" fill={t ? t[1] : mode === 'saver' ? '#4d6a8a' : '#2b2f36'} /></>
    )}>
      {stand}
      <rect x="0" y={-z - 3 - h} width={w} height={h} fill="#2b2f36" />
      <rect x="1" y={-z - 2 - h} width={w - 2} height={h - 2} fill={t ? t[0] : mode === 'saver' ? '#161a20' : '#23272d'} />
      {t && !text && kind === 'bars' && Array.from({ length: Math.floor((w - 3) / 3) }, (_, k) => (
        <rect key={k} className="d-bar" style={{ animationDelay: `${-(i * 0.37 + k * 0.53)}s` }} x={2 + k * 3} y={-z - 4 - BAR_H[k % 5]} width="2" height={BAR_H[k % 5]} fill={t[1]} />
      ))}
      {t && !text && kind === 'candle' && [0, 1, 2, 3].map((k) => (
        <g key={k} className="px-tick" style={{ animationDelay: `${-(i * 0.5 + k * 0.9)}s` }}>
          <rect x={3.5 + k * 3} y={-z - 11 + ((k * 3 + i) % 4)} width="1" height="6" fill={(k + i) % 3 ? '#ff8076' : '#8fe0b4'} />
          <rect x={3 + k * 3} y={-z - 10 + ((k * 3 + i) % 4)} width="2" height="3" fill={(k + i) % 3 ? '#ff8076' : '#8fe0b4'} /></g>
      ))}
      {t && !text && kind === 'lines' && [0, 1, 2, 3].map((k) => (
        <rect key={k} className="px-line" style={{ animationDelay: `${-(i * 0.3) + k * 0.7 - 2.8}s` }} x="2.5" y={-z - 11.5 + k * 2} width={[9, 6, 10, 4][(k + i) % 4]} height="1" fill={t[1]} />
      ))}
      {t && text && <text x={w / 2} y={-z - 6} textAnchor="middle" className="px-mon" fill={(TONES[textTone] || t)[1]}>{text}</text>}
      {mode === 'saver' && <rect className="d-led" style={{ animationDelay: `${-i * 0.7}s` }} x={3 + (i % 3) * 3} y={-z - 9} width="2" height="2" fill="#4d6a8a" />}
    </Plane>
  )
}

function Plant({ gx, gy }) {
  const [x, y] = iso(gx, gy).map(R0)
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
  const [x, y] = iso(gx, gy).map(R0)
  const out = []
  CAT.forEach((row, j) => [...row].forEach((ch, i) => { if (ch !== '.') out.push(<rect key={`${j}-${i}`} x={x - 3 + i} y={y - 9 + j} width="1" height="1" fill={asleep && ch === 'e' ? '#b9722a' : CAT_PAL[ch]} />) }))
  return (
    <g>
      <ellipse cx={x} cy={y} rx="5" ry="1.8" fill="#2a241d" opacity="0.16" />
      <g className={asleep ? undefined : 'px-wag'}><rect x={x + 4} y={y - 5} width="1" height="4" fill="#c9822f" /><rect x={x + 5} y={y - 7} width="1" height="3" fill="#c9822f" /></g>
      <g className={asleep ? 'px-breath' : 'px-earflick'}>{out}</g>
      {!asleep && <><rect className="px-blink" x={x - 2} y={y - 6} width="1" height="1" fill="#e8a04a" /><rect className="px-blink" x={x + 2} y={y - 6} width="1" height="1" fill="#e8a04a" /></>}
      {asleep && <text className="d-zz" x={x + 5} y={y - 9}>z</text>}
    </g>
  )
}

// 墙上的窗：外面的天色跟北京时间走。x 是本地坐标里窗的左边
function Win({ x, w, sky, sun, seed = 0 }) {
  const [top, low] = SKY[sky]
  const night = sky === 'night'
  const sx = x + 4 + Math.round((w - 12) * (night ? 0.7 : sun))
  return (
    <g>
      <rect x={x - 2} y="-47" width={w + 4} height="32" fill="#fbf8f2" />
      <rect x={x} y="-45" width={w} height="28" fill={top} /><rect x={x} y="-29" width={w} height="12" fill={low} />
      {night ? [3, 11, 19, 27, 34].filter((k) => k < w - 2).map((k, j) => <rect key={k} className="d-led" style={{ animationDelay: `${-j * 0.5}s` }} x={x + k} y={-43 + ((j * 7 + seed) % 9)} width="1" height="1" fill="#fff8d8" />)
        : <><rect className="px-cloud" x={x + 3 + seed} y="-41" width="9" height="3" fill="#ffffff" opacity="0.85" /><rect className="px-cloud" x={x + 5 + seed} y="-43" width="5" height="2" fill="#ffffff" opacity="0.85" /></>}
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

// 一面墙：只有当它是「后墙」（里面朝我们）时才画
function Wall({ a, b, fill, children }) {
  const L = Math.hypot(b[0] - a[0], b[1] - a[1]) * HW
  return (
    <Plane a={a} b={b}>
      <rect x="0" y={-WALL} width={L} height={WALL} fill={fill} />
      <rect x="0" y={-WALL - 3} width={L} height="3" fill="#8a7658" /><rect x="0" y="-4" width={L} height="4" fill="#cdbd98" />
      {children}
    </Plane>
  )
}

function Glass({ a, b }) {
  const w = Math.hypot(b[0] - a[0], b[1] - a[1]) * HW
  const pane = (
    <>
      <rect x="0" y={-GLASS} width={w} height={GLASS} fill="#bfe3f2" opacity="0.2" />
      <polygon points={`${w * 0.25},${-GLASS} ${w * 0.25 + 5},${-GLASS} ${w * 0.25 - 9},0 ${w * 0.25 - 14},0`} fill="#ffffff" opacity="0.18" />
      <rect className="px-glint" x={w * 0.6} y={-GLASS + 2} width="2" height={GLASS - 3} fill="#ffffff" />
      <rect x="0" y={-GLASS} width={w} height="2" fill="#6b7480" /><rect x="0" y="-1" width={w} height="1" fill="#6b7480" opacity="0.6" />
      <rect x="0" y={-GLASS} width="1.5" height={GLASS} fill="#6b7480" /><rect x={w - 1.5} y={-GLASS} width="1.5" height={GLASS} fill="#6b7480" />
    </>
  )
  return <Plane a={a} b={b} back={pane}>{pane}</Plane>
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

const RackLeds = () => (
  <>{[[[16.75, 1.0], [17.85, 1.0]], [[16.75, 0.15], [16.75, 1.0]]].map(([a, b], n) => (
    <Plane key={n} a={a} b={b}>
      {[0, 1, 2, 3, 4].map((k) => (
        <g key={k}><rect x="2" y={-32 + k * 6} width={Math.hypot(b[0] - a[0], b[1] - a[1]) * HW - 4} height="4" fill="#1a1d22" />
          <rect className="d-led" style={{ animationDelay: `${-k * 0.31 - n}s` }} x="4" y={-31 + k * 6} width="2" height="2" fill="#7fd1a5" />
          <rect className="d-led fast" style={{ animationDelay: `${-k * 0.47 - n}s` }} x="8" y={-31 + k * 6} width="2" height="2" fill={k === 2 ? '#f0c36a' : '#7fd1a5'} /></g>
      ))}
    </Plane>
  ))}</>
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
  if (sc.stage === 'arrive') return `盘前 · 陆续到岗 ${n}/${SEATS.length + 1}`
  if (sc.stage === 'lunch') return '午休 · 13:00 开工'
  if (sc.stage === 'review') return '收盘 · 经理在白板前复盘'
  if (sc.stage === 'leave') return sc.meeting ? '周五 · 经理和两位交易员在会议室开周复盘会，其他人陆续下班' : `收盘 · 陆续下班，还剩 ${n} 人`
  return `${phase} · 全员在岗`
}

const loadRot = () => { try { return (+localStorage.getItem('desk-office-rot') || 0) % 4 } catch { return 0 } }
const clampView = (v) => { const k = Math.min(5, Math.max(1, v.k)); return { k, x: Math.min(W - W / k, Math.max(0, v.x)), y: Math.min(H - H / k, Math.max(0, v.y)) } }

export function IsoOffice({ data, active, picked, event, phase, onPick }) {
  const [rotation, setRotation] = useState(loadRot)
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })         // viewBox = [x, y, W/k, H/k]
  const svgRef = useRef(null)
  const viewRef = useRef(view)
  const drag = useRef(null)
  viewRef.current = view
  ROT = rotation

  // 滚轮缩放（以鼠标位置为中心）。已经缩到最小还往外滚，就放行给页面自己滚动
  useEffect(() => {
    const el = svgRef.current
    if (!el) return undefined
    const onWheel = (e) => {
      const v = viewRef.current
      const k = Math.min(5, Math.max(1, v.k * (e.deltaY < 0 ? 1.18 : 1 / 1.18)))
      if (k === v.k) return
      e.preventDefault()
      const r = el.getBoundingClientRect()
      const px = v.x + ((e.clientX - r.left) / r.width) * (W / v.k)
      const py = v.y + ((e.clientY - r.top) / r.height) * (H / v.k)
      setView(clampView({ k, x: px - (px - v.x) * (v.k / k), y: py - (py - v.y) * (v.k / k) }))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])
  // 按住拖动。拖过的那一下不算点击
  useEffect(() => {
    const move = (e) => {
      const d = drag.current
      if (!d) return
      const dx = e.clientX - d.cx
      const dy = e.clientY - d.cy
      if (!d.moved && Math.hypot(dx, dy) < 4) return
      d.moved = true
      const r = svgRef.current.getBoundingClientRect()
      setView(clampView({ k: d.v.k, x: d.v.x - (dx / r.width) * (W / d.v.k), y: d.v.y - (dy / r.height) * (H / d.v.k) }))
    }
    const up = () => { const d = drag.current; if (d) setTimeout(() => { if (drag.current === d) drag.current = null }, 0) }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
  }, [])
  const turnBy = (n) => setRotation((r) => { const v = (r + n + 4) % 4; try { localStorage.setItem('desk-office-rot', String(v)) } catch { /* 存不了就算了 */ } return v })
  const zoomBy = (f) => setView((v) => { const k = Math.min(5, Math.max(1, v.k * f)); const cx = v.x + W / v.k / 2; const cy = v.y + H / v.k / 2; return clampView({ k, x: cx - W / k / 2, y: cy - H / k / 2 }) })

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
    return [role, { st, tone, at: atOf(role), here: st !== 'gone', home: st === 'seat' || st === 'nap', mode: st === 'seat' && tone ? 'on' : st === 'gone' ? 'off' : 'saver' }]
  }))
  const idx = (role) => people.indexOf(role)

  // 日常小动作（纯装饰，按分钟换人）：邻座歪过头聊两句、伸个懒腰
  const chat = sc.stage === 'work' ? CHAT_PAIRS[Math.floor(sc.m / 3) % (CHAT_PAIRS.length + 2)] : null
  const chatOn = chat && chat.every((k) => info[SEATS[k].role].st === 'seat' && SEATS[k].role !== active)
  const stretcher = sc.stage === 'work' || sc.stage === 'lunch' ? SEATS[(Math.floor(sc.m / 2) * 5 + 3) % SEATS.length].role : null
  const turnOf = (role) => {
    if (!chatOn) return 0
    const k = chat.map((n) => SEATS[n].role).indexOf(role)
    if (k < 0) return 0
    const other = HOME[SEATS[chat[1 - k]].role]
    return Math.sign(iso(other.gx, other.gy)[0] - iso(HOME[role].gx, HOME[role].gy)[0])
  }
  const emoteOf = (role) => {
    const p = info[role]
    if (p.st !== 'seat') return null
    if (p.tone === 'bad') return 'alert'
    const today = traders[role]?.account?.today
    return today > 0 ? 'heart' : today < 0 ? 'sweat' : null
  }

  const speech = active ? speechOf(active, data) : null
  const away = active && !info[active].here
  const anchor = (() => {
    if (!active) return null
    const at = away ? HOME[active] : info[active].at
    const [sx, sy] = iso(at.gx, at.gy)
    return [sx, sy - (!facing(at.f).front && !at.stand ? 24 : 33)]
  })()
  const who = active ? LOOKS[active].name : ''
  const bubble = !speech ? '' : event && event.role === active ? event.text : away ? `（人不在）走之前说的：${speech.short}` : speech.short
  const vw = W / view.k
  const vh = H / view.k
  const pct = anchor ? [((anchor[0] - view.x) / vw) * 100, ((anchor[1] - 6 - view.y) / vh) * 100] : null
  const bubbleOn = pct && pct[0] > 2 && pct[0] < 98 && pct[1] > 8 && pct[1] < 100

  const nums = (roles.breadth?.line || '').match(/涨 (\d+) \/ 跌 (\d+)/)
  const share = nums ? +nums[1] / (+nums[1] + +nums[2] || 1) : 0.5
  const ticker = tickerOf(data)

  const personOf = (role, extra = {}) => {
    const p = info[role]
    return <Person key={role} look={LOOKS[role]} at={p.at} talk={active === role && p.st !== 'nap' && p.st !== 'sofa'} i={idx(role)} pose={POSE[role]}
      desk={p.home} stretch={stretcher === role && p.st === 'seat' && active !== role} emote={emoteOf(role)} turn={turnOf(role)} {...extra} />
  }
  const seatFp = (at) => [at.gx - 0.18, at.gy - 0.18, 0.36, 0.36]
  // 桌面上属于某个人的东西：键盘 / 纸、鼠标，还有伸到桌面上的两条胳膊（手在动）
  const deskSet = (role) => {
    const p = info[role]
    const at = HOME[role]
    const look = LOOKS[role]
    const pose = p.st === 'nap' ? 'rest' : POSE[role].k
    const ky = at.gy + at.f[1] * 0.4
    const subs = []
    if (POSE[role].k === 'write') subs.push({ p: [at.gx, ky], node: <Box key={`${role}-kb`} x={at.gx - 0.28} y={ky - 0.2} w={0.5} d={0.4} z0={9.5} z1={9.9} top="#ffffff" left="#c9c0ae" right="#b5ab97" /> })
    else subs.push({ p: [at.gx, ky], node: <g key={`${role}-kb`}><Box x={at.gx - 0.32} y={ky - 0.12} w={0.64} d={0.24} z0={9.5} z1={10.2} top="#e4e0d6" />
      {p.mode === 'on' && <polygon className="d-led fast" points={pts(iso(at.gx - 0.1, ky - 0.05, 10.3), iso(at.gx + 0.05, ky - 0.05, 10.3), iso(at.gx + 0.05, ky + 0.05, 10.3), iso(at.gx - 0.1, ky + 0.05, 10.3))} fill="#bdb7a8" />}</g> })
    if (POSE[role].k === 'mouse') subs.push({ p: [at.gx + 0.36, ky], node: <Box key={`${role}-ms`} x={at.gx + 0.3} y={ky - 0.1} w={0.13} d={0.2} z0={9.5} z1={10.4} top="#3d424b" /> })
    if (!p.home) return subs
    const col = look.body === 'suit' ? '#2c3550' : look.body === 'vest' ? '#fbf8f2' : look.color
    const typing = p.mode === 'on'
    const hands = pose === 'type' || pose === 'rest' ? [[-0.12, 'px-hand'], [0.12, 'px-hand b']] : pose === 'mouse' ? [[-0.1, 'px-hand slow'], [0.36, 'px-mouse']]
      : pose === 'write' ? [[0.08, 'px-pen', true]] : pose === 'cross' ? [] : [[-0.1, 'px-hand slow']]
    const [sx, sy] = iso(at.gx, at.gy).map(R0)
    const { side } = facing(at.f)
    const lean = pose === 'rest' ? 2 : POSE[role].lean || 0
    hands.forEach(([dx, cls, pen], k) => {
      const [hx, hy] = iso(at.gx + dx, ky, 11).map(R0)
      const lefty = hands.length === 2 ? (iso(at.gx + hands[0][0], ky)[0] < iso(at.gx + hands[1][0], ky)[0]) === (k === 0) : hx < sx
      subs.push({ p: [at.gx + dx, ky + at.f[1] * 0.01], node: (
        <g key={`${role}-h${k}`}>
          <line x1={sx + (lefty ? -5 : 5) + side * lean} y1={sy - 7} x2={hx} y2={hy} stroke={col} strokeWidth="2" />
          <g className={typing ? cls : undefined} style={{ animationDelay: `${-(idx(role) * 0.07)}s` }}>
            <rect x={hx - 1} y={hy - 1} width="2" height="2" fill={SKINS[look.skin]} />
            {pen && <rect x={hx} y={hy - 4} width="1" height="3" fill="#2a241d" />}
          </g>
        </g>
      ) })
    })
    return subs
  }

  const tagOf = (role) => {
    const p = info[role]
    const at = p.here ? p.at : HOME[role]
    const [sx, sy] = iso(at.gx, at.gy)
    const { front, side } = facing(at.f)
    const below = !front && !at.stand
    const t = p.tone && p.here ? TONES[p.tone][1] : null
    return <Tag key={role} x={sx - (below ? side * 3 : 0)} y={below ? sy + 4 : sy - (at.stand ? 30 : 31)} name={LOOKS[role].name} color={role === 'manager' ? '#c9a13b' : LOOKS[role].color}
      dot={t} blink={!!t && p.st === 'seat'} zz={p.st === 'nap' || p.st === 'sofa'} on={active === role} dim={!p.here} />
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
  const items = []
  const put = (key, fp, node) => items.push({ fp, node: <g key={key}>{node}</g> })

  // 四张桌子：桌子（连同桌面上的东西）是一件，每个座位是一件
  Object.entries(PODS).forEach(([key, P]) => {
    const seats = Object.entries(PLACE).filter(([, [k]]) => k === key)
    const subs = []
    seats.forEach(([role, [, side, c]]) => {
      const p = info[role]
      const t = traders[role]
      const today = t?.account?.today
      const pnl = role.startsWith('trader:') && today != null ? `${today > 0 ? '+' : ''}${Number(today).toFixed(1)}` : null
      const far = side === 'far'
      const mx = P.x + 1 + c * 2 + (far ? -0.38 : 0.38)
      const my = P.y + (far ? 0.55 : 1.45)
      subs.push({ p: [mx, my], node: <Mon key={`${role}-mon`} gx={mx} gy={my} face={far ? -1 : 1} w={pnl ? 19 : 16} mode={p.mode} tone={p.tone} text={pnl}
        textTone={today > 0 ? 'up' : today < 0 ? 'down' : 'calm'} kind={['bars', 'candle', 'lines'][idx(role) % 3]} i={idx(role)} /> })
      subs.push(...deskSet(role))
      if (idx(role) % 2 === 0 && POSE[role].k !== 'sip') {
        const cx = P.x + 0.35 + c * 2
        const cy = P.y + (far ? 0.42 : 1.58)
        subs.push({ p: [cx, cy], node: <g key={`${role}-mug`}><Box x={cx - 0.08} y={cy - 0.08} w={0.16} d={0.16} z0={9.5} z1={12.5} top="#ffffff" left="#e0574a" right="#b8443a" />
          {p.home && <rect className="d-steam" x={iso(cx, cy)[0] - 0.5} y={iso(cx, cy, 15)[1]} width="1" height="2" fill="#ffffff" />}</g> })
      }
    })
    SPARE.filter(([k]) => k === key).forEach(([, c]) => subs.push({ p: [P.x + 1.38 + c * 2, P.y + 1.45], node: <Mon key={`sp${c}`} gx={P.x + 1.38 + c * 2} gy={P.y + 1.45} mode="off" /> }))
    if (key === 'D') subs.push({ p: [P.x + 2, P.y + 0.5], node: (
      <Plane key="big" a={[P.x + 0.7, P.y + 0.5]} b={[P.x + 3.3, P.y + 0.5]} back={<><rect x={1.3 * HW - 1} y="-13" width="2" height="3.5" fill="#22252b" /><rect x="0" y="-28" width={2.6 * HW} height="15" fill="#3d424b" /></>}>
        <rect x={1.3 * HW - 1} y="-13" width="2" height="3.5" fill="#22252b" />
        <rect x="0" y="-28" width={2.6 * HW} height="15" fill="#2b2f36" /><rect x="1" y="-27" width={2.6 * HW - 2} height="13" fill="#0f1a16" />
        <text x={1.3 * HW} y="-18.4" textAnchor="middle" className="px-mon" fill="#f0c36a">DeepSeek ×{data?.calls ?? 0}</text>
      </Plane>
    ) })
    put(`pod${key}`, [P.x, P.y, 4, 2], (
      <>
        <Box x={P.x + 0.15} y={P.y + 0.15} w={0.3} d={1.7} z1={7.5} top="#cfc5b2" />
        <Box x={P.x + 3.55} y={P.y + 0.15} w={0.3} d={1.7} z1={7.5} top="#cfc5b2" />
        <Box x={P.x} y={P.y} w={4} d={2} z0={7.5} z1={9.5} top="#f8f3e9" left="#d8cdb8" right="#c4b8a0" />
        <polygon points={pts(iso(P.x + 0.1, P.y + 0.98, 9.5), iso(P.x + 3.9, P.y + 0.98, 9.5), iso(P.x + 3.9, P.y + 1.02, 9.5), iso(P.x + 0.1, P.y + 1.02, 9.5))} fill="#d8cdb8" />
        {byDepth(subs)}
      </>
    ))
    seats.forEach(([role]) => put(`seat-${role}`, seatFp(HOME[role]), <Seat at={HOME[role]}>{info[role].home && personOf(role)}</Seat>))
    SPARE.filter(([k]) => k === key).forEach(([, c]) => { const at = seatAt(key, 'near', c); put(`spare-${key}${c}`, seatFp(at), <Seat at={at} />) })
  })

  // 经理室
  put('mgr-desk', [2.2, 1.95, 2.7, 1.3], (
    <>
      <Box x={2.3} y={2.05} w={0.3} d={1.1} z1={7.5} top="#6d4128" /><Box x={4.5} y={2.05} w={0.3} d={1.1} z1={7.5} top="#6d4128" />
      <Box x={2.2} y={1.95} w={2.7} d={1.3} z0={7.5} z1={9.5} top="#a9744b" left="#7a4a2e" right="#62391f" />
      {byDepth([
        { p: [3.12, 2.75], node: <Mon key="mon" gx={3.12} gy={2.75} face={-1} mode={mgr.mode} tone="calm" kind="candle" i={5} /> },
        { p: [4.45, 2.9], node: <Box key="plate" x={4.2} y={2.85} w={0.5} d={0.14} z0={9.5} z1={12.5} top="#e3c15a" left="#c9a13b" right="#a8842c" /> },
        ...Array.from({ length: Math.min(todo.length, 3) }, (_, k) => ({ p: [2.6, 2.6], node: <Box key={`pp${k}`} x={2.4} y={2.4} w={0.45} d={0.4} z0={9.5 + k} z1={10.3 + k} top="#ffffff" left="#c9c0ae" right="#b5ab97" /> })),
        ...deskSet('manager'),
      ])}
    </>
  ))
  put('mgr-seat', seatFp(HOME.manager), <Seat at={HOME.manager} big>{mgr.home && personOf('manager')}</Seat>)
  put('sofa', [0.25, 4.05, 1.25, 1.9], (
    <>
      <Box x={0.25} y={4.2} w={0.35} d={1.6} z1={13} top="#3b4668" /><Box x={0.6} y={4.2} w={0.9} d={1.6} z1={6} top="#4a5680" />
      <Box x={0.25} y={4.05} w={1.25} d={0.2} z1={9} top="#3b4668" />
      {mgr.st === 'sofa' && personOf('manager')}
      <Box x={0.25} y={5.75} w={1.25} d={0.2} z1={9} top="#3b4668" />
    </>
  ))
  if (mgr.st === 'board') put('mgr-board', seatFp(BOARD), personOf('manager', { write: true }))
  put('plant1', [5.1, 0.5, 0.4, 0.4], <Plant gx={5.3} gy={0.7} />)
  put('plant2', [5.1, 11.0, 0.4, 0.4], <Plant gx={5.3} gy={11.2} />)
  put('plant3', [6.5, 0.4, 0.4, 0.4], <Plant gx={6.7} gy={0.6} />)
  put('plant4', [17.1, 11.1, 0.4, 0.4], <Plant gx={17.3} gy={11.3} />)
  put('glass1', [0, RY - 0.03, RX, 0.06], <Glass a={[0, RY]} b={[RX, RY]} />)
  ;[[0, 2.6], [3.9, 8.5], [9.7, NY]].forEach(([y0, y1], k) => put(`glass2${k}`, [RX - 0.03, y0, 0.06, y1 - y0], <Glass a={[RX, y1]} b={[RX, y0]} />))

  // 会议室
  put('meet-table', [1.5, 8.2, 3.3, 1.6], (
    <>
      <Box x={1.7} y={8.35} w={0.3} d={1.3} z1={7.5} top="#b7a98e" /><Box x={4.3} y={8.35} w={0.3} d={1.3} z1={7.5} top="#b7a98e" />
      <Box x={1.5} y={8.2} w={3.3} d={1.6} z0={7.5} z1={9.5} top="#e8d3ad" left="#c2a878" right="#a88f62" />
      {meeting.length > 0 && <><Box x={2.6} y={8.7} w={0.5} d={0.4} z0={9.5} z1={10.2} top="#ffffff" /><Box x={3.5} y={9.1} w={0.5} d={0.4} z0={9.5} z1={10.2} top="#ffffff" /></>}
    </>
  ))
  MEET_SEATS.forEach((at, k) => {
    const role = meeting.find((r) => MEET[r] === at)
    put(`meet-seat${k}`, seatFp(at), <Seat at={at}>{role && personOf(role)}</Seat>)
  })

  // 大厅里的零碎
  put('rack', [16.75, 0.15, 1.1, 0.85], <><Box x={16.75} y={0.15} w={1.1} d={0.85} z1={36} top="#4a4f57" left="#2b2f36" right="#22252b" /><RackLeds /></>)
  put('cooler', [17.1, 5.2, 0.6, 0.6], (
    <>
      <Box x={17.1} y={5.2} w={0.6} d={0.6} z1={14} top="#f4f1ea" left="#d8d3c8" right="#bfb9ab" />
      <Box x={17.2} y={5.3} w={0.4} d={0.4} z0={14} z1={21} top="#bfe3f2" left="#8fc7e0" right="#74b0cc" />
      <rect className="d-steam late" x={iso(17.4, 5.5)[0] - 1} y={iso(17.4, 5.5, 18)[1]} width="1" height="1" fill="#ffffff" />
    </>
  ))
  if (runner === 'walk') {
    const [x0, y0] = iso(7.0, 6.0).map(R0)
    const [x1, y1] = iso(16.0, 6.0).map(R0)
    const fwdFront = facing([1, 0]).front
    put('runner', [7.0, 5.85, 9.0, 0.3], (
      <g className="px-run" style={{ '--rx': `${x1 - x0}px`, '--ry': `${y1 - y0}px` }}>
        <ellipse cx={x0} cy={y0} rx="6" ry="2.2" fill="#2a241d" opacity="0.16" />
        <g className="px-bob">
          <g className="px-fwd"><Chibi look={RUNNER} x={x0 - 7} y={y0 - 20} stand walk back={!fwdFront} paper={carrying && fwdFront} /></g>
          <g className="px-bwd"><Chibi look={RUNNER} x={x0 - 7} y={y0 - 20} stand walk back={fwdFront} /></g>
        </g>
      </g>
    ))
  }
  if (runner === 'stand') put('runner', [16.3, 5.2, 0.6, 0.6], <Person look={RUNNER} at={{ gx: 16.6, gy: 5.5, f: [0, 1], stand: true, cup: true }} i={3} />)
  chatters.forEach((r) => put(`chat-${r}`, seatFp(info[r].at), personOf(r)))
  const catAt = lightsOn ? CAT_SPOTS[Math.floor(sc.m / 7) % CAT_SPOTS.length] : CAT_SPOTS[0]
  put('cat', [catAt[0] - 0.2, catAt[1] - 0.2, 0.4, 0.4], <Cat gx={catAt[0]} gy={catAt[1]} asleep={!lightsOn} />)

  const dotsAt = (gx, gy, up) => { const [x, y] = iso(gx, gy).map(R0); return <Dots x={x - 6} y={y - up} /> }
  const [VX, VY] = dims()
  const outline = pts(scr(0, VY), scr(0, VY, WALL + 3), scr(0, 0, WALL + 3), scr(VX, 0, WALL + 3), scr(VX, 0), scr(VX, VY))
  const hand = (deg, len) => ({ x2: 120 + Math.sin((deg * Math.PI) / 180) * len, y2: -38 - Math.cos((deg * Math.PI) / 180) * len })

  return (
    <div className="iso">
      <div className="iso-stage">
        <svg ref={svgRef} viewBox={`${view.x} ${view.y} ${vw} ${vh}`} shapeRendering="crispEdges" role="img" className={view.k > 1 ? 'zoomed' : undefined}
          aria-label="等距视角的交易台办公室：经理室、会议室和大厅四张桌子，按北京时间上下班；可以旋转、拖动、缩放"
          onPointerDown={(e) => { if (e.button === 0) drag.current = { cx: e.clientX, cy: e.clientY, v: viewRef.current, moved: false } }}
          onClickCapture={(e) => { if (drag.current?.moved) { e.stopPropagation(); drag.current = null } }}
          onDoubleClick={() => setView({ k: 1, x: 0, y: 0 })}>
          <defs><clipPath id="px-led-clip"><rect x="136" y="-46" width="114" height="32" /></clipPath></defs>
          {/* 地板：先一整块楼板，再按房间上色 */}
          <Box x={0} y={0} w={NX} d={NY} z0={-7} z1={0} top="#ead9b8" left="#b59f78" right="#9c8762" />
          <polygon points={pts(iso(0, 0), iso(RX, 0), iso(RX, RY), iso(0, RY))} fill="#c9a87c" />
          <polygon points={pts(iso(0, RY), iso(RX, RY), iso(RX, NY), iso(0, NY))} fill="#b4c4cc" />
          {Array.from({ length: NX - 1 }, (_, k) => k + 1).map((g) => <line key={`x${g}`} x1={iso(g, 0)[0]} y1={iso(g, 0)[1]} x2={iso(g, NY)[0]} y2={iso(g, NY)[1]} stroke="#2a241d" strokeWidth="0.5" opacity="0.07" />)}
          {Array.from({ length: NY - 1 }, (_, k) => k + 1).map((g) => <line key={`y${g}`} x1={iso(0, g)[0]} y1={iso(0, g)[1]} x2={iso(NX, g)[0]} y2={iso(NX, g)[1]} stroke="#2a241d" strokeWidth="0.5" opacity="0.07" />)}
          <polygon points={pts(iso(1.6, 1.0), iso(5.4, 1.0), iso(5.4, 4.0), iso(1.6, 4.0))} fill="#2c3550" opacity="0.5" />
          {Object.entries(PODS).map(([key, P]) => {
            const rug = pts(iso(P.x - 0.8, P.y - 1.3), iso(P.x + 4.8, P.y - 1.3), iso(P.x + 4.8, P.y + 3.6), iso(P.x - 0.8, P.y + 3.6))
            return (
              <g key={key}>
                <polygon points={rug} fill={P.tint} opacity="0.13" /><polygon points={rug} fill="none" stroke={P.tint} strokeWidth="0.8" opacity="0.4" />
                <FloorText fp={[P.x - 0.8, P.y - 1.3, 5.6, 4.9]} className="px-floor" fill={shade(P.tint, 0.7)}>{P.name}</FloorText>
              </g>
            )
          })}
          <FloorText fp={[0, 0, RX, RY]} className="px-floor" fill="#6d4a2e">经理室</FloorText>
          <FloorText fp={[0, RY, RX, NY - RY]} className="px-floor" fill="#4a5f6c">会议室 · 周五复盘</FloorText>

          {/* 四面墙，只画当前朝向下在后面的两面 */}
          <Wall a={[0, NY]} b={[0, 0]} fill="#e9dfc8">
            {/* 会议室的电视 */}
            <rect x="26" y="-45" width="56" height="29" fill="#22252b" /><rect x="28" y="-43" width="52" height="25" fill={meeting.length ? '#14263a' : '#161a20'} />
            <text x="54" y="-32" textAnchor="middle" className="px-mon" fill={meeting.length ? '#8fd0ff' : '#4d6a8a'}>{meeting.length ? '周复盘 · 进行中' : '周五 15:30'}</text>
            <text x="54" y="-24" textAnchor="middle" className="px-mon" fill={meeting.length ? '#f0c36a' : '#4d6a8a'}>{data?.stance ? `风险档位 ${data.stance.level}` : '周复盘会'}</text>
            {/* 经理室的白板 */}
            <rect x="138" y="-47" width="58" height="30" fill="#8a8f98" /><rect x="140" y="-45" width="54" height="26" fill="#fdfcf8" /><rect x="146" y="-17" width="42" height="2" fill="#6b7480" />
            <text x="142" y="-39" className="px-board">{data?.now ? data.now.slice(5, 10).replace('-', '/') : '—'} {sc.stage === 'review' ? '复盘' : '晨会'}</text>
            <text x="142" y="-29" className={`px-board big ${REGIME_TONE[m?.regime] || ''}`}>{m ? m.regime : '未开会'}</text>
            <text x="142" y="-22" className="px-board">{data?.stance ? `档位 ${data.stance.level}` : ''}{m ? ` 可做 ${sd ? todo.length : '—'}` : ' 09:26 开会'}</text>
          </Wall>
          <Wall a={[0, 0]} b={[NX, 0]} fill="#f5eedc">
            <Win x={24} w={60} sky={sky} sun={sun} />
            <Win x={262} w={34} sky={sky} sun={sun} seed={3} />
            {/* 挂钟：时针分针是真的北京时间，秒针自己走 */}
            <circle cx="120" cy="-38" r="7" fill="#2a241d" /><circle cx="120" cy="-38" r="5.6" fill="#fdfcf8" />
            <line x1="120" y1="-38" {...hand(((hh % 12) + mm / 60) * 30, 3)} stroke="#2a241d" strokeWidth="1.2" />
            <line x1="120" y1="-38" {...hand(mm * 6, 4.6)} stroke="#2a241d" strokeWidth="0.7" />
            <rect className="px-sec" x="119.75" y="-43" width="0.5" height="5" fill="#c8322b" />
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
          </Wall>
          <Wall a={[NX, NY]} b={[0, NY]} fill="#f5eedc">
            <Win x={28} w={60} sky={sky} sun={sun} seed={1} />
            <Win x={124} w={60} sky={sky} sun={sun} seed={4} />
            <Win x={244} w={52} sky={sky} sun={sun} seed={2} />
          </Wall>
          <Wall a={[NX, 0]} b={[NX, NY]} fill="#e9dfc8">
            {/* 大门和公司的牌子 */}
            <rect x="92" y="-42" width="32" height="42" fill="#6d4a2e" /><rect x="94" y="-40" width="13" height="40" fill="#8a5f3c" /><rect x="109" y="-40" width="13" height="40" fill="#8a5f3c" />
            <rect x="105" y="-20" width="1" height="3" fill="#e3c15a" /><rect x="110" y="-20" width="1" height="3" fill="#e3c15a" />
            <rect x="98" y="-50" width="20" height="6" fill="#2f8a4c" /><text x="108" y="-45.4" textAnchor="middle" className="px-exit">出口</text>
            <rect x="22" y="-44" width="52" height="22" fill="#2a241d" /><text x="48" y="-33" textAnchor="middle" className="px-logo">衡策</text>
            <text x="48" y="-26" textAnchor="middle" className="px-logo s">TRADING DESK</text>
            <Win x={150} w={52} sky={sky} sun={sun} seed={5} />
          </Wall>

          {sortItems(items).map((it) => it.node)}

          {/* 聊天的小气泡、名牌：盖在所有家具上面 */}
          {chatOn && (() => { const [a, b] = chat.map((k) => HOME[SEATS[k].role]); return dotsAt((a.gx + b.gx) / 2, (a.gy + b.gy) / 2, 44) })()}
          {chatters.length > 1 && dotsAt(16.45, 6.4, 36)}
          {meeting.length > 0 && dotsAt(3.1, 9, 40)}
          {people.map((r) => tagOf(r))}
          <Tag x={iso(...catAt)[0]} y={iso(...catAt)[1] + 3} name="招财" color="#e8a04a" dot="#e8a04a" zz={!lightsOn} />

          {/* 关灯：整间屋子压暗，机柜的灯还在闪（程序确实还在跑） */}
          {dark > 0 && <polygon points={outline} fill={sc.stage === 'lunch' ? '#2a241d' : '#0b1020'} opacity={dark} pointerEvents="none" />}
          {!lightsOn && <RackLeds />}
          {active && !away && <g className="d-char is-active"><polygon points={`${anchor[0] - 3},${anchor[1] - 5} ${anchor[0] + 3},${anchor[1] - 5} ${anchor[0]},${anchor[1] - 1}`} fill="#c8322b" /></g>}
          {people.map((r) => hit(r))}
        </svg>
        <div className="iso-ctl">
          <button onClick={() => turnBy(-1)} title="向左转 90°">↺</button>
          <button onClick={() => turnBy(1)} title="向右转 90°">↻</button>
          <button onClick={() => zoomBy(1.4)} title="放大（也可以滚轮）">＋</button>
          <button onClick={() => zoomBy(1 / 1.4)} title="缩小">－</button>
          <button onClick={() => { setView({ k: 1, x: 0, y: 0 }); if (rotation) turnBy(-rotation) }} title="回到默认视角">复位</button>
        </div>
        {picked && speech ? (
          <div className={`d-sheet ${!pct || pct[0] < 50 ? 'right' : 'left'}`} role="dialog" aria-label={`${who}的汇报`}>
            <header><b>{who}</b><span>{away ? '人不在 · 这是走之前说的' : '当面汇报'}</span><button onClick={() => onPick(active)} aria-label="关闭">×</button></header>
            <div className="d-sheet-body">
              <p className="lead">{event && event.role === active ? event.text : speech.short}</p>
              {speech.full.map((line, k) => <p key={k}>{line}</p>)}
              {!speech.full.length && <p className="dim">就这些，没有更多要报的。</p>}
            </div>
          </div>
        ) : bubble && bubbleOn && (
          <>
            <div className="d-bubble" style={{ left: `${Math.min(84, Math.max(16, pct[0]))}%`, top: `${pct[1]}%` }}>{bubble}</div>
            <div className="d-tail" style={{ left: `${pct[0]}%`, top: `${pct[1]}%` }} />
          </>
        )}
      </div>
      <div className="d-office-foot">
        <span className={`d-dot ${working ? 'work' : sc.stage === 'lunch' ? 'nap' : ''}`} />
        <span>{footOf(sc, phase, sc.present.length)}</span>
        <span className="d-office-hint">点小人看汇报 · 滚轮缩放 · 按住拖动 · 右上角转视角 · 双击复原</span>
      </div>
    </div>
  )
}
