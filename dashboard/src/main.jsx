import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import Terminal from './Terminal.jsx'
import './styles.css'
import './terminal.css'

// 默认打开行情终端（dj 2026-11-01：深色、实时数据、少写字）；原来的研究面板放在「详细记录」里，#detail 直达
function Root() {
  const [view, setView] = useState(() => (window.location.hash === '#detail' ? 'detail' : 'terminal'))
  useEffect(() => {
    const onHash = () => setView(window.location.hash === '#detail' ? 'detail' : 'terminal')
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    document.body.classList.toggle('is-term', view === 'terminal')
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', view === 'terminal' ? '#0b0e13' : '#f4f6f3')
  }, [view])
  if (view === 'terminal') return <Terminal onDetail={() => { window.location.hash = '#detail' }} />
  return (
    <>
      <button className="back-to-term" onClick={() => { window.location.hash = '' }}>← 返回盘面</button>
      <App />
    </>
  )
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
