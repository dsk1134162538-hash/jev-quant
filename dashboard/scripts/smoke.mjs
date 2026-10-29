import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifacts = resolve(root, 'artifacts')
await mkdir(artifacts, { recursive: true })

const contentTypes = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
const livePayload = {
  snapshot: { displayTime: '2026-10-27 12:02:06', phase: '盘中', regime: '防守观察 · 09:53 历史决策' },
  markets: [
    { name: '上证指数', value: '3,988.01', change: '+0.20%', breadth: '昨收 3,980.00' },
    { name: '深证成指', value: '11,496.44', change: '-0.33%', breadth: '昨收 11,534.38' },
    { name: '创业板指', value: '2,626.31', change: '-0.45%', breadth: '昨收 2,638.23' },
  ],
  observations: [{ name: '万向钱潮', code: '000559.SZ', price: '12.23', change: -3.78, minute: '—', volume: '1,461,675', verdict: '行情已更新，等待 AI 判断', tone: 'muted' }],
  account: { total: 10000, cash: 10000, positionPct: 0, todayPnl: null, positions: 0, fillsToday: 0, source: 'SQLite 只读 · test.sqlite' },
  evidence: { indicesReceived: 3, indicesTotal: 3, sampleDown: 1, sampleTotal: 1, complete: true, researchComplete: true },
  marketBreadth: { available: true, complete: true, up: 2850, down: 2450, flat: 200, total: 5500, universe: 5577, coverage: 98.6, upRatio: 51.8, limitUp: 45, limitDown: 12, amount: 865000000000 },
  sectorBreadth: { available: true, complete: true, up: 28, down: 21, total: 49, leaders: [{ name: '机器人', change: 2.35, companies: 60, leaderName: '某某科技', leaderChange: 8.2 }], laggards: [{ name: '白酒', change: -1.25, companies: 20, leaderName: '某某酒业', leaderChange: 1.1 }] },
  decisions: [{ id: 'AI-20261027-120000', time: '12:00', type: '盘中 AI', title: '防守观察 · 空仓等待', action: '空仓等待', regime: '防守观察', risk: '仅研究 · 不下单', status: '已持久化', persisted: true, market: '全市场上涨 2850 家。', sector: '行业上涨 28 个。', stock: '未生成 quote.json。', trigger: '使用新鲜数据。', invalidation: '数据失效时放弃。' }],
  decisionStore: { enabled: true, persistent: true, count: 1, error: '' },
  live: { connected: true, stale: false, error: '', quoteAsOf: '2026-10-27 12:02:06' },
  systemItems: [],
}
const researchPayload = {
  available: true,
  status: 'completed',
  model: 'five-day-cross-sectional-lightgbm',
  generatedAtUtc: '2026-10-27T17:16:08.312541+00:00',
  dataAsOf: '2026-10-19',
  universeMode: 'pit_full_market',
  pointInTimeFullMarket: true,
  sampleRows: 6728796,
  codes: 5777,
  signalDays: 1304,
  signalStart: '2021-06-01',
  signalEnd: '2026-10-19',
  featureCompleteRatio: 0.9466255181461884,
  rankIc: 0.08428618050843688,
  rankIcTStat: 11.854400097083316,
  rankIcDays: 630,
  coverage: 1,
  quantileMonotonicity: 0.39999999999999997,
  quantileGrossReturns: [0.0018125382128899637, 0.0059500385462835335, 0.00610864508804131, 0.006133105037682979, 0.00456940889025227],
  netReturn: 0.3624702833338169,
  annualizedNetReturn: 0.127398777676053,
  maxDrawdown: -0.30629483859781925,
  periodMeanTStat: 1.0303396429289893,
  averageTurnover: 0.3845622400983529,
  averagePositions: 1080.3153846153846,
  walkForward: { train_days: 504, validation_days: 126, test_days: 126, step_days: 126, embargo_days: 6 },
  limitations: [],
  folds: [
    { fold: 1, rankIc: 0.0746064833195704, netReturn: -0.14615716969794046, testSignalStart: '2024-01-18T00:00:00', testSignalEnd: '2024-07-29T00:00:00', bestIteration: 182 },
    { fold: 2, rankIc: 0.08929206056747338, netReturn: 0.419241257044195, testSignalStart: '2024-07-30T00:00:00', testSignalEnd: '2025-02-10T00:00:00', bestIteration: 266 },
    { fold: 3, rankIc: 0.12256917318987023, netReturn: 0.26917058870858757, testSignalStart: '2025-02-11T00:00:00', testSignalEnd: '2025-08-12T00:00:00', bestIteration: 42 },
    { fold: 4, rankIc: 0.08678288904676165, netReturn: 0.08002990689227363, testSignalStart: '2025-08-13T00:00:00', testSignalEnd: '2026-02-24T00:00:00', bestIteration: 153 },
    { fold: 5, rankIc: 0.04818029641850873, netReturn: -0.17976727167278206, testSignalStart: '2026-02-25T00:00:00', testSignalEnd: '2026-08-26T00:00:00', bestIteration: 89 },
  ],
  deploymentEligible: false,
  tradingConnected: false,
}
const kronosPayload = {
  available: true,
  status: 'smoke_completed',
  scope: 'smoke_only',
  comparisonEligible: false,
  deploymentEligible: false,
  tradingConnected: false,
  generatedAtUtc: '2026-10-27T23:40:13.216683+00:00',
  modelRevision: '901c26c1332695a2a8f243eb2f37243a37bea320',
  coverage: 0.0029993438935232916,
  rankIcDays: 2,
  smoke: { days: 2, names_per_day: 16, rows: 32 },
}
async function useBuiltSite(context, payload = null) {
  await context.route('http://dashboard.local/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/dashboard' && payload) {
      return route.fulfill({ status: 200, body: JSON.stringify(payload), contentType: 'application/json' })
    }
    if (pathname === '/api/research/cross-sectional') {
      return route.fulfill({ status: 200, body: JSON.stringify(researchPayload), contentType: 'application/json' })
    }
    if (pathname === '/api/research/kronos') {
      return route.fulfill({ status: 200, body: JSON.stringify(kronosPayload), contentType: 'application/json' })
    }
    const relative = pathname === '/' ? 'index.html' : pathname.slice(1)
    if (relative.includes('..')) return route.fulfill({ status: 403 })
    const extension = relative.slice(relative.lastIndexOf('.'))
    try {
      const body = await readFile(resolve(root, 'dist', relative))
      return route.fulfill({ status: 200, body, contentType: contentTypes[extension] || 'application/octet-stream' })
    } catch {
      return route.fulfill({ status: 404, body: 'Not found' })
    }
  })
}

let browser
try {
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = []
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, acceptDownloads: true })
  await useBuiltSite(context, livePayload)
  const page = await context.newPage()
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => message.type() === 'error' && errors.push(message.text()))

  await page.goto('http://dashboard.local/', { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '今日策略：防守观察' }).waitFor()
  await page.getByText('万向钱潮', { exact: true }).waitFor()
  await page.getByText('3,988.01', { exact: true }).waitFor()
  await page.locator('.topbar').getByText('只读实时', { exact: true }).waitFor()
  await page.getByRole('heading', { name: '行业领涨' }).waitFor()
  await page.getByText('防守观察 · 空仓等待', { exact: true }).first().waitFor()
  await page.getByText('2850 涨 / 2450 跌', { exact: true }).first().waitFor()
  if ((await page.locator('body').innerText()).includes('演示数据')) throw new Error('真实快照页面仍包含演示数据标记')
  await page.screenshot({ path: resolve(artifacts, 'dashboard-desktop.png'), fullPage: true })
  await page.getByRole('button', { name: '查看完整决策依据' }).click()
  await page.getByRole('dialog').waitFor()
  await page.getByRole('button', { name: '关闭决策详情' }).click()
  await page.getByRole('dialog').waitFor({ state: 'detached' })

  await page.getByRole('button', { name: '决策记录' }).click()
  await page.getByPlaceholder('搜索决策、动作或类型').fill('不存在的决策')
  await page.getByRole('heading', { name: '没有匹配的决策' }).waitFor()
  await page.getByPlaceholder('搜索决策、动作或类型').fill('')
  await page.getByRole('button', { name: '空仓', exact: true }).click()
  await page.locator('.decision-row').waitFor()
  if (await page.locator('.decision-row').count() !== 1) throw new Error('决策动作筛选结果不正确')

  await page.getByRole('button', { name: '策略验证', exact: true }).click()
  await page.getByRole('heading', { name: '先证伪，再考虑上线' }).waitFor()
  await page.getByRole('heading', { name: '全 A PIT 基线已完成，尚未通过上线评估' }).waitFor()
  await page.getByText('0.0843', { exact: true }).waitFor()
  await page.getByText('真实权重冒烟通过 · 全量未运行', { exact: true }).waitFor()
  await page.getByText('2 日 × 16 只 · 32 条', { exact: true }).waitFor()
  await page.getByText('6,728,796 行 · 5,777 只', { exact: true }).waitFor()
  await page.getByText('Q5 不是最优组。', { exact: true }).waitFor()
  await page.getByRole('cell', { name: '2026-02-25 — 2026-08-26', exact: true }).waitFor()
  await page.getByText('deploymentEligible=false', { exact: true }).waitFor()
  await page.screenshot({ path: resolve(artifacts, 'dashboard-research-desktop.png'), fullPage: true })
  await page.getByRole('button', { name: '系统状态', exact: true }).click()
  await page.getByRole('heading', { name: '先确认系统知道自己在做什么' }).waitFor()

  await page.getByRole('button', { name: '复盘时间线', exact: true }).click()
  await page.getByRole('heading', { name: '复盘决策质量，而非只看盈亏' }).waitFor()
  await page.locator('.review-nav').getByRole('button', { name: /盘中复盘/ }).click()
  await page.getByRole('button', { name: '导出盘中复盘' }).waitFor()
  await page.locator('.review-nav').getByRole('button', { name: /盘前复盘/ }).click()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出盘前复盘' }).click()
  const download = await downloadPromise
  if (!download.suggestedFilename().endsWith('.md')) throw new Error('复盘导出不是 Markdown 文件')
  await download.saveAs(resolve(artifacts, download.suggestedFilename()))

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' })
  await useBuiltSite(mobile, livePayload)
  const mobilePage = await mobile.newPage()
  mobilePage.on('pageerror', (error) => errors.push(error.message))
  mobilePage.on('console', (message) => message.type() === 'error' && errors.push(message.text()))
  await mobilePage.goto('http://dashboard.local/', { waitUntil: 'networkidle' })
  const overflow = await mobilePage.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    elements: [...document.querySelectorAll('body *')].flatMap((element) => {
      const rect = element.getBoundingClientRect()
      return rect.right > window.innerWidth + 1
        ? [{ tag: element.tagName, class: element.getAttribute('class'), left: rect.left, right: rect.right, width: rect.width }]
        : []
    }),
  }))
  if (overflow.document > overflow.viewport) {
    throw new Error(`移动端横向溢出：${JSON.stringify(overflow)}`)
  }
  await mobilePage.getByRole('button', { name: '打开导航' }).click()
  await mobilePage.locator('.sidebar.open').waitFor()
  await mobilePage.screenshot({ path: resolve(artifacts, 'dashboard-mobile-nav.png'), fullPage: true })
  await mobilePage.getByRole('button', { name: '复盘时间线', exact: true }).click()
  await mobilePage.locator('.sidebar.open').waitFor({ state: 'detached' })
  await mobilePage.screenshot({ path: resolve(artifacts, 'dashboard-mobile-review.png'), fullPage: true })
  await mobilePage.getByRole('button', { name: '打开导航' }).click()
  await mobilePage.getByRole('button', { name: '策略验证', exact: true }).click()
  await mobilePage.getByRole('heading', { name: '全 A PIT 基线已完成，尚未通过上线评估' }).waitFor()
  const researchOverflow = await mobilePage.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    elements: [...document.querySelectorAll('body *')].flatMap((element) => {
      const rect = element.getBoundingClientRect()
      return rect.right > window.innerWidth + 1 && !element.closest('.table-scroll')
        ? [{ tag: element.tagName, class: element.getAttribute('class'), left: rect.left, right: rect.right, width: rect.width }]
        : []
    }),
  }))
  if (researchOverflow.document > researchOverflow.viewport) {
    throw new Error(`研究页移动端横向溢出：${JSON.stringify(researchOverflow)}`)
  }
  await mobilePage.screenshot({ path: resolve(artifacts, 'dashboard-research-mobile.png'), fullPage: true })
  await mobile.close()

  if (errors.length) throw new Error(`浏览器错误：${errors.join(' | ')}`)
  const result = {
    desktop: 'passed', decisionModal: 'passed', reviewExport: 'passed',
    researchDesktop: 'passed', researchMobile: 'passed', mobileNavigation: 'passed', browserErrors: 0,
    screenshots: ['dashboard-desktop.png', 'dashboard-research-desktop.png', 'dashboard-mobile-nav.png', 'dashboard-mobile-review.png', 'dashboard-research-mobile.png'],
  }
  await writeFile(resolve(artifacts, 'smoke-results.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
  await context.close()
} finally {
  if (browser) await browser.close()
}
