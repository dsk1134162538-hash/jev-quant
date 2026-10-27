import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifacts = resolve(root, 'artifacts')
await mkdir(artifacts, { recursive: true })

const contentTypes = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
async function useBuiltSite(context) {
  await context.route('http://dashboard.local/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
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
  await useBuiltSite(context)
  const page = await context.newPage()
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => message.type() === 'error' && errors.push(message.text()))

  await page.goto('http://dashboard.local/', { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '早上好，Jamo' }).waitFor()
  await page.screenshot({ path: resolve(artifacts, 'dashboard-desktop.png'), fullPage: true })

  await page.getByRole('button', { name: '深证成指', exact: true }).click()
  await page.getByRole('img', { name: '深证成指演示走势' }).waitFor()
  await page.getByRole('button', { name: '查看完整决策依据' }).click()
  await page.getByRole('dialog').waitFor()
  await page.getByRole('button', { name: '关闭决策详情' }).click()
  await page.getByRole('dialog').waitFor({ state: 'detached' })

  await page.getByRole('button', { name: '决策记录' }).click()
  await page.getByPlaceholder('搜索决策、动作或类型').fill('不存在的决策')
  await page.getByRole('heading', { name: '没有匹配的决策' }).waitFor()
  await page.getByPlaceholder('搜索决策、动作或类型').fill('')
  await page.getByRole('button', { name: '复盘', exact: true }).click()
  await page.locator('.decision-row').waitFor()
  if (await page.locator('.decision-row').count() !== 1) throw new Error('决策动作筛选结果不正确')

  await page.getByRole('button', { name: '策略验证', exact: true }).click()
  await page.getByRole('heading', { name: '先证伪，再考虑上线' }).waitFor()
  await page.getByRole('button', { name: '系统状态', exact: true }).click()
  await page.getByRole('heading', { name: '先确认系统知道自己在做什么' }).waitFor()

  await page.getByRole('button', { name: '复盘时间线', exact: true }).click()
  await page.getByRole('heading', { name: '复盘决策质量，而非只看盈亏' }).waitFor()
  await page.getByRole('button', { name: /盘中复盘/ }).click()
  await page.getByRole('button', { name: '导出盘中复盘' }).waitFor()
  await page.getByRole('button', { name: /盘前复盘/ }).click()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出盘前复盘' }).click()
  const download = await downloadPromise
  if (!download.suggestedFilename().endsWith('.md')) throw new Error('复盘导出不是 Markdown 文件')
  await download.saveAs(resolve(artifacts, download.suggestedFilename()))

  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' })
  await useBuiltSite(mobile)
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
  await mobile.close()

  if (errors.length) throw new Error(`浏览器错误：${errors.join(' | ')}`)
  const result = {
    desktop: 'passed', decisionModal: 'passed', reviewExport: 'passed',
    mobileNavigation: 'passed', browserErrors: 0,
    screenshots: ['dashboard-desktop.png', 'dashboard-mobile-nav.png', 'dashboard-mobile-review.png'],
  }
  await writeFile(resolve(artifacts, 'smoke-results.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
  await context.close()
} finally {
  if (browser) await browser.close()
}
