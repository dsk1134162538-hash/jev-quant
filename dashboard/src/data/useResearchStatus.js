import { useEffect, useState } from 'react'

export function useResearchStatus() {
  const [state, setState] = useState({ data: null, kronos: null, loading: true, error: '' })

  useEffect(() => {
    if (window.location.protocol === 'file:') {
      setState({ data: null, kronos: null, loading: false, error: '静态预览未连接研究只读接口' })
      return
    }
    const controller = new AbortController()
    let stopped = false
    async function load() {
      try {
        const options = { signal: controller.signal, cache: 'no-store' }
        const [baselineResponse, kronosResponse] = await Promise.all([
          fetch('/api/research/cross-sectional', options),
          fetch('/api/research/kronos', options),
        ])
        if (!baselineResponse.ok) throw new Error(`研究只读接口 ${baselineResponse.status}`)
        if (!kronosResponse.ok) throw new Error(`Kronos 只读接口 ${kronosResponse.status}`)
        const [data, kronos] = await Promise.all([baselineResponse.json(), kronosResponse.json()])
        if (!stopped) setState({ data, kronos, loading: false, error: '' })
      } catch (error) {
        if (error.name === 'AbortError' || stopped) return
        setState({ data: null, kronos: null, loading: false, error: error.message })
      }
    }
    load()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [])

  return state
}
