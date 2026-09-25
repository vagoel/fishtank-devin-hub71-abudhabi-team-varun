import { useCallback, useEffect, useRef, useState } from 'react'
import { createDemoApi } from '../services/demoApi'
import { createHttpApi, dataMode, pollInterval } from '../services/dashboardApi'
import { startPolling } from '../services/polling'
import { isActive, mergeIncidents, newAlerts } from '../domain/incidents'

export function useDashboard() {
  const [api] = useState(() => dataMode === 'demo' ? createDemoApi() : createHttpApi())
  const [data, setData] = useState({ sites: [], workers: [], incidents: [], dispatches: [], cursor: null })
  const [connection, setConnection] = useState('connecting')
  const [lastUpdated, setLastUpdated] = useState(null)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)
  const [paused, setPaused] = useState(false)
  const [notices, setNotices] = useState([])
  const [now, setNow] = useState(() => Date.now())
  const current = useRef(data)
  const hydrated = useRef(false)
  const mutationEpoch = useRef(0)
  const mutating = useRef(false)
  const polling = useRef(null)

  const ingest = useCallback((incoming, notify = true, recovery = false) => {
    const alerts = hydrated.current && notify && !recovery ? newAlerts(current.current.incidents, incoming.incidents) : []
    const merged = { ...incoming, incidents: mergeIncidents(current.current.incidents, incoming.incidents) }
    current.current = merged
    hydrated.current = true
    setData(merged)
    setLastUpdated(Date.now())
    setConnection('connected')
    setError('')
    if (alerts.length) setNotices(previous => [...alerts.map(incident => ({ ...incident, noticeId: `${incident.id}:${incident.revision}` })), ...previous].slice(0, 20))
    if (recovery && merged.incidents.some(isActive)) setNotices(previous => [{ noticeId: `recovery:${Date.now()}`, summary: `${merged.incidents.filter(isActive).length} active incidents after reconnection. Review the current incident list.` }, ...previous].slice(0, 20))
  }, [])

  useEffect(() => {
    if (paused) return
    polling.current = startPolling({
      interval: pollInterval,
      async task(signal) {
        const epoch = mutationEpoch.current
        const snapshot = hydrated.current ? await api.pollChanges(current.current.cursor, signal) : await api.loadSnapshot(signal)
        return { snapshot, epoch }
      },
      onSuccess({ snapshot, epoch }, recovery) { if (epoch === mutationEpoch.current) ingest(snapshot, true, recovery) },
      onError(failure) { setConnection('disconnected'); setError(failure.message || 'Connection interrupted') },
    })
    const onVisibility = () => { if (document.visibilityState === 'visible') polling.current?.refresh() }
    window.addEventListener('online', onVisibility)
    document.addEventListener('visibilitychange', onVisibility)
    return () => { polling.current?.stop(); window.removeEventListener('online', onVisibility); document.removeEventListener('visibilitychange', onVisibility) }
  }, [api, ingest, paused])

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  async function mutate(operation, reset = false) {
    if (mutating.current) return false
    mutating.current = true
    mutationEpoch.current++
    setBusy(true)
    setActionError('')
    try {
      const snapshot = await operation()
      if (reset) { current.current = { incidents: [] }; hydrated.current = false; setNotices([]) }
      ingest(snapshot, !reset)
      return true
    } catch (failure) { setActionError(failure.message); return false }
    finally { mutationEpoch.current++; mutating.current = false; setBusy(false) }
  }

  return {
    ...data, mode: api.mode, connection, lastUpdated, error: actionError || error, busy, paused, setPaused, notices, now,
    stale: paused || connection !== 'connected' || (lastUpdated !== null && now - lastUpdated > 12000),
    dismissNotice: id => setNotices(previous => previous.filter(notice => notice.noticeId !== id)),
    clearError: () => { setError(''); setActionError('') },
    refresh: () => polling.current?.refresh(),
    review: (incident, status, options) => mutate(() => api.reviewIncident(incident.id, incident.revision, status, options)),
    dispatch: (incident, services) => mutate(() => api.simulateDispatch(incident.id, incident.revision, services)),
    trigger: (type, siteId) => api.mode === 'demo' ? mutate(() => api.trigger(type, siteId)) : Promise.resolve(false),
    reset: () => api.mode === 'demo' ? mutate(() => api.reset(), true) : Promise.resolve(false),
  }
}
