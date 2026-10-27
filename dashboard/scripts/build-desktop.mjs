import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = resolve(root, 'dist')
let html = await readFile(resolve(dist, 'index.html'), 'utf8')

const scriptMatch = html.match(/<script type="module" crossorigin src="(.+?)"><\/script>/)
const styleMatch = html.match(/<link rel="stylesheet" crossorigin href="(.+?)">/)
if (!scriptMatch || !styleMatch) throw new Error('无法识别 Vite 构建入口，桌面单文件打包已停止')

const javascript = await readFile(resolve(dist, scriptMatch[1]), 'utf8')
const css = await readFile(resolve(dist, styleMatch[1]), 'utf8')
html = html
  .replace(scriptMatch[0], () => `<script type="module">${javascript.replace(/<\/script/gi, '<\\/script')}</script>`)
  .replace(styleMatch[0], () => `<style>${css}</style>`)

const output = resolve(dist, 'desktop.html')
await writeFile(output, html)
console.log(output)
