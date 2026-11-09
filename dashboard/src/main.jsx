import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import Terminal from './Terminal.jsx'
import DeskPage from './Desk.jsx'
import './styles.css'
import './terminal.css'
import './desk.css'

// 默认打开交易台（dj 2026-11-09：经理 + 汇报员 + 交易员，仿「横行」面板）；#term 是原来的深色盘面，#detail 是研究面板
const viewOf = () => (window.location.hash === '#detail' ? 'detail' : window.location.hash === '#term' ? 'terminal' : 'desk')
const THEME = { desk: '#f2ede3', terminal: '#0b0e13', detail: '#f4f6f3' }

function Root() {
  const [view, setView] = useState(viewOf)
  useEffect(() => {
    const onHash = () => setView(viewOf())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    document.body.classList.toggle('is-term', view === 'terminal')
    document.body.classList.toggle('is-desk', view === 'desk')
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME[view])
  }, [view])
  const go = (hash) => () => { window.location.hash = hash }
  if (view === 'desk') return <DeskPage onTerm={go('#term')} onDetail={go('#detail')} />
  if (view === 'terminal') return <Terminal onDetail={go('#detail')} />
  return (
    <>
      <button className="back-to-term" onClick={go('')}>← 返回交易台</button>
      <App />
    </>
  )
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
