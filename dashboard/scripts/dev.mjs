import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repo = resolve(root, '..')
const python = resolve(repo, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
const command = existsSync(python) ? python : (process.platform === 'win32' ? 'python.exe' : 'python3')
const api = spawn(command, [resolve(root, 'server.py'), '--api-only', '--port', '8767'], {
  cwd: repo,
  stdio: 'inherit',
  windowsHide: true,
})
const vite = spawn(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), ...process.argv.slice(2)], {
  cwd: root,
  stdio: 'inherit',
})
let closing = false
function close(code = 0) {
  if (closing) return
  closing = true
  if (!api.killed) api.kill()
  if (!vite.killed) vite.kill()
  setTimeout(() => process.exit(code), 100).unref()
}
api.on('error', (error) => { console.error('只读 API 启动失败：', error.message); close(1) })
api.on('exit', (code) => { if (!closing && code) close(code) })
vite.on('error', (error) => { console.error('Vite 启动失败：', error.message); close(1) })
vite.on('exit', (code) => close(code || 0))
process.on('SIGINT', () => close(0))
process.on('SIGTERM', () => close(0))
