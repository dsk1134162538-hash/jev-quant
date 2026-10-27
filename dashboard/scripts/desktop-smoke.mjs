import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const artifacts = resolve(root, 'artifacts')
const profile = resolve(artifacts, 'edge-app-profile')
await mkdir(profile, { recursive: true })
const appUrl = pathToFileURL(resolve(root, 'dist', 'desktop.html')).href

let context
try {
  context = await chromium.launchPersistentContext(profile, {
    channel: 'msedge',
    headless: false,
    viewport: { width: 1440, height: 960 },
    args: [`--app=${appUrl}`],
  })
  let page
  for (let attempt = 0; attempt < 100 && !page; attempt += 1) {
    page = context.pages().find((candidate) => candidate.url().startsWith('file:'))
    if (!page) await new Promise((resolveWait) => setTimeout(resolveWait, 100))
  }
  if (!page) throw new Error(`未找到应用窗口：${context.pages().map((candidate) => candidate.url()).join(', ')}`)
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => message.type() === 'error' && errors.push(message.text()))

  await page.getByRole('heading', { name: '早上好，Jamo' }).waitFor()
  await page.getByRole('button', { name: '复盘时间线', exact: true }).click()
  await page.getByRole('heading', { name: '复盘决策质量，而非只看盈亏' }).waitFor()
  await page.getByRole('button', { name: '决策记录' }).click()
  await page.getByRole('heading', { name: '每一次判断，都留下依据' }).waitFor()
  await page.getByRole('button', { name: '今日总览', exact: true }).click()
  await page.getByRole('heading', { name: '早上好，Jamo' }).waitFor()
  await page.screenshot({ path: resolve(artifacts, 'desktop-app.png') })

  if (!page.url().startsWith('file:')) throw new Error(`桌面窗口未直接加载本地文件：${page.url()}`)
  if (errors.length) throw new Error(`桌面窗口错误：${errors.join(' | ')}`)
  const result = { desktopLaunch: 'passed', localFile: 'passed', navigation: 'passed', browserErrors: 0 }
  await writeFile(resolve(artifacts, 'desktop-smoke-results.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  if (context) await context.close()
}
