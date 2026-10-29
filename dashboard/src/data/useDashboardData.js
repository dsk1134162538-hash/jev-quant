import { useEffect, useState } from 'react'
import { markets, observations, snapshot, systemItems } from './realSnapshot.js'

const fallback = {
  snapshot,
  markets,
  observations,
  account: { total: 10000, cash: 10000, positionPct: 0, todayPnl: null, positions: 0, fillsToday: 0, source: '静态快照' },
  evidence: { indicesReceived: 0, indicesTotal: 3, sampleDown: 3, sampleTotal: 3, complete: false, researchComplete: false },
  marketBreadth: { available: false, complete: false, loading: false, up: 0, down: 0, total: 0, universe: 0, coverage: 0, amount: 0 },
  sectorBreadth: { available: false, complete: false, loading: false, up: 0, down: 0, total: 0, leaders: [], laggards: [] },
  decisions: [],
  decisionStore: { enabled: false, persistent: true, count: 0, error: '' },
  live: { connected: false, stale: false, error: '', attempted: false, refreshSeconds: 1 },
  systemItems,
}

export function useDashboardData() {
  const [data, setData] = useState(fallback)
  useEffect(() => {
    if (window.location.protocol === 'file:') return
    let stopped = false
    let controller = null
    async function refresh() {
      if (document.hidden) return
      controller?.abort()
      controller = new AbortController()
      try {
        const response = await fetch('/api/dashboard', { signal: controller.signal, cache: 'no-store' })
        if (!response.ok) throw new Error(`只读 API ${response.status}`)
        const payload = await response.json()
        if (!stopped) setData((previous) => ({ ...previous, ...payload, live: { ...payload.live, attempted: true } }))
      } catch (error) {
        if (error.name === 'AbortError' || stopped) return
        setData((previous) => ({
          ...previous,
          live: { ...previous.live, attempted: true, stale: previous.live.connected, error: error.message },
        }))
      }
    }
    refresh()
    const timer = window.setInterval(refresh, 1000)
    const onVisibility = () => { if (!document.hidden) refresh() }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stopped = true
      controller?.abort()
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])
  return data
}

export function money(value) {
  return Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
