import { mergeIncidents } from '../domain/incidents'

export const dataMode = import.meta.env.VITE_DATA_MODE === 'api' ? 'api' : 'demo'
export const pollInterval = Math.max(1000, Number(import.meta.env.VITE_POLL_INTERVAL_MS) || 2000)
const base = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '')

export function validateSnapshot(data) {
  if (!data || !['sites', 'workers', 'incidents', 'dispatches'].every(key => Array.isArray(data[key]))) throw new Error('Invalid dashboard response: expected sites, workers, incidents and dispatches.')
  const text = value => typeof value === 'string' && value.length > 0
  const date = value => typeof value === 'string' && Number.isFinite(Date.parse(value))
  const optionalText = value => value == null || typeof value === 'string'
  const validAnalysis = value => !value || (['pending', 'ready', 'failed'].includes(value.status) && (value.status !== 'ready' || (typeof value.summary === 'string' && typeof value.recommendation === 'string')))
  const validEvidence = value => !value || (optionalText(value.label) && optionalText(value.source) && (optionalText(value.value) || Number.isFinite(value.value)))
  const coordinates = value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite) && Math.abs(value[0]) <= 180 && Math.abs(value[1]) <= 85
  for (const key of ['sites', 'workers', 'incidents', 'dispatches']) {
    if (data[key].some(record => !record || !text(record.id)) || new Set(data[key].map(record => record.id)).size !== data[key].length) throw new Error(`Invalid or duplicate ${key} identifiers in API response.`)
  }
  if (data.sites.some(site => !text(site.name) || !optionalText(site.code) || !optionalText(site.district) || (site.height != null && (!Number.isFinite(site.height) || site.height < 0 || site.height > 1000)) || !coordinates(site.coordinates) || !date(site.lastSeen) || !['online', 'offline'].includes(site.connectivity) || (site.footprint && (!Array.isArray(site.footprint) || site.footprint.length < 4 || !site.footprint.every(coordinates))))) throw new Error('Invalid site coordinates or metadata in API response.')
  if (data.workers.some(worker => !text(worker.name) || !optionalText(worker.role) || !text(worker.siteId) || !text(worker.bandId) || !date(worker.lastSeen) || !['online', 'offline'].includes(worker.connectivity) || (worker.temperature != null && !Number.isFinite(worker.temperature)) || (worker.readings && (!Array.isArray(worker.readings) || !worker.readings.every(Number.isFinite))))) throw new Error('Invalid worker in API response.')
  if (data.incidents.some(incident => !Number.isInteger(incident.revision) || incident.revision < 1 || !text(incident.siteId) || !text(incident.workerId) || !text(incident.type) || !date(incident.createdAt) || !['critical', 'warning'].includes(incident.severity) || !['new', 'reviewing', 'confirmed', 'dispatching', 'dispatched', 'resolved', 'dismissed'].includes(incident.status) || !Array.isArray(incident.timeline) || incident.timeline.some(entry => !date(entry.at) || !text(entry.label)) || !validAnalysis(incident.analysis) || !validEvidence(incident.evidence))) throw new Error('Invalid incident in API response.')
  if (data.dispatches.some(dispatch => !text(dispatch.siteId) || !text(dispatch.incidentId) || !date(dispatch.createdAt) || !Array.isArray(dispatch.services) || dispatch.services.some(service => !['medical', 'safety', 'rescue'].includes(service)))) throw new Error('Invalid dispatch record in API response.')
  return data
}

async function request(path, options = {}) {
  const controller = new AbortController()
  const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal
  const timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const response = await fetch(`${base}${path}`, { credentials: 'same-origin', ...options, signal, headers: { 'Content-Type': 'application/json', ...options.headers } })
    if (!response.ok) throw new Error(response.status === 409 ? 'This incident has changed. Refresh and review before retrying.' : `API unavailable (${response.status}). Your last valid data is retained.`)
    return await response.json()
  } finally { clearTimeout(timeout) }
}

export function createHttpApi() {
  let snapshot = null
  return {
    mode: 'api',
    async loadSnapshot(signal) { snapshot = validateSnapshot(await request('/dashboard', { signal })); return snapshot },
    async pollChanges(cursor, signal) {
      let next = cursor
      let candidate = structuredClone(snapshot)
      for (let page = 0; page < 100; page++) {
        const update = await request(`/events?cursor=${encodeURIComponent(next || '')}`, { signal })
        if (!update || !Array.isArray(update.incidents) || typeof update.cursor !== 'string') throw new Error('Invalid event response from API.')
        candidate = { ...candidate, ...update, incidents: mergeIncidents(candidate.incidents, update.incidents) }
        if (!update.hasMore) { snapshot = validateSnapshot(candidate); return snapshot }
        if (update.cursor === next) throw new Error('API event cursor did not advance.')
        next = update.cursor
      }
      throw new Error('Event backlog is too large. Refresh the dashboard snapshot.')
    },
    async reviewIncident(id, revision, status, options) {
      snapshot = validateSnapshot(await request(`/incidents/${encodeURIComponent(id)}/review`, { method: 'POST', body: JSON.stringify({ revision, status, ...options }) }))
      return snapshot
    },
    async simulateDispatch() { throw new Error('Dispatch simulation is only available in demo mode. No live dispatch endpoint is connected.') },
  }
}
