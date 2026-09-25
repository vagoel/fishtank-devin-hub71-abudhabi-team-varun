import { resolveDataMode } from '../config'
import { normalizeIncidentFeed } from './incidentFeed'

export const dataMode = resolveDataMode(import.meta.env)
export const pollInterval = Math.max(1000, Number(import.meta.env.VITE_POLL_INTERVAL_MS) || 2000)
const defaultBase = import.meta.env.VITE_API_BASE_URL || '/api'

export function validateSnapshot(data) {
  if (!data || !['sites', 'workers', 'incidents', 'dispatches'].every(key => Array.isArray(data[key]))) throw new Error('Invalid dashboard response: expected sites, workers, incidents and dispatches.')
  const text = value => typeof value === 'string' && value.length > 0
  const date = value => typeof value === 'string' && Number.isFinite(Date.parse(value))
  const optionalText = value => value == null || typeof value === 'string'
  const validAnalysis = value => !value || (['pending', 'ready', 'failed', 'unavailable'].includes(value.status) && (value.status !== 'ready' || (typeof value.summary === 'string' && typeof value.recommendation === 'string')))
  const validEvidence = value => !value || (optionalText(value.label) && optionalText(value.source) && (optionalText(value.value) || Number.isFinite(value.value)))
  const coordinates = value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite) && Math.abs(value[0]) <= 180 && Math.abs(value[1]) <= 85
  for (const key of ['sites', 'workers', 'incidents', 'dispatches']) {
    if (data[key].some(record => !record || !text(record.id)) || new Set(data[key].map(record => record.id)).size !== data[key].length) throw new Error(`Invalid or duplicate ${key} identifiers in API response.`)
  }
  if (data.sites.some(site => !text(site.name) || !optionalText(site.code) || !optionalText(site.district) || (site.height != null && (!Number.isFinite(site.height) || site.height < 0 || site.height > 1000)) || !coordinates(site.coordinates) || !date(site.lastSeen) || !['online', 'offline', 'unknown'].includes(site.connectivity) || (site.footprint && (!Array.isArray(site.footprint) || site.footprint.length < 4 || !site.footprint.every(coordinates))))) throw new Error('Invalid site coordinates or metadata in API response.')
  if (data.workers.some(worker => !text(worker.name) || !optionalText(worker.role) || !text(worker.siteId) || !text(worker.bandId) || !(date(worker.lastSeen) || (worker.incidentOnly && worker.lastSeen === null)) || !['online', 'offline', 'unknown'].includes(worker.connectivity) || (worker.temperature != null && !Number.isFinite(worker.temperature)) || (worker.readings && (!Array.isArray(worker.readings) || !worker.readings.every(Number.isFinite))))) throw new Error('Invalid worker in API response.')
  if (data.incidents.some(incident => !Number.isInteger(incident.revision) || incident.revision < 1 || !text(incident.siteId) || !text(incident.workerId) || !text(incident.type) || !date(incident.createdAt) || !['critical', 'warning', 'info'].includes(incident.severity) || !['new', 'reviewing', 'confirmed', 'dispatching', 'dispatched', 'resolved', 'dismissed', 'suspected', 'no_response', 'worker_ok', 'cancelled', 'acknowledged'].includes(incident.status) || !Array.isArray(incident.timeline) || incident.timeline.some(entry => !date(entry.at) || !text(entry.label)) || !validAnalysis(incident.analysis) || !validEvidence(incident.evidence))) throw new Error('Invalid incident in API response.')
  if (data.dispatches.some(dispatch => !text(dispatch.siteId) || !text(dispatch.incidentId) || !date(dispatch.createdAt) || !Array.isArray(dispatch.services) || dispatch.services.some(service => !['medical', 'safety', 'rescue'].includes(service)))) throw new Error('Invalid dispatch record in API response.')
  return data
}

async function requestIncidents(baseUrl, externalSignal) {
  const controller = new AbortController()
  const signal = externalSignal ? AbortSignal.any([externalSignal, controller.signal]) : controller.signal
  const timeout = setTimeout(() => controller.abort(), 10000)
  try {
    const response = await fetch(`${baseUrl.replace(/\/$/, '')}/v1/incidents`, { method: 'GET', credentials: 'same-origin', cache: 'no-store', signal })
    if (!response.ok) {
      if (response.status === 404) throw new Error('GET /v1/incidents is not available at the configured host yet. Use VITE_DATA_MODE=demo and restart, or run dev:demo.')
      if ([401, 403].includes(response.status)) throw new Error('The incident API denied access. Confirm authentication requirements with the API team.')
      throw new Error(`Incident API unavailable (${response.status}). Last valid reports are retained; demo mode is available separately.`)
    }
    try { return await response.json() } catch { throw new Error('The incident API did not return JSON. Check the API URL and proxy configuration.') }
  } catch (error) {
    if (externalSignal?.aborted) throw error
    if (controller.signal.aborted) throw new Error('The incident API timed out. Last valid reports are retained.')
    if (error instanceof TypeError) throw new Error('Could not reach the incident API. Check the proxy/network connection, or switch to demo mode.')
    throw error
  } finally { clearTimeout(timeout) }
}

export function createHttpApi({ baseUrl = defaultBase } = {}) {
  let snapshot
  async function refresh(signal) {
    const payload = await requestIncidents(baseUrl, signal)
    if (signal?.aborted) throw new DOMException('Incident polling cancelled', 'AbortError')
    const next = validateSnapshot(normalizeIncidentFeed(payload, snapshot))
    snapshot = next
    return next
  }
  return {
    mode: 'api',
    loadSnapshot: refresh,
    pollChanges: (_cursor, signal) => refresh(signal),
    async reviewIncident() { throw new Error('The incident API contract is read-only. No review/status-update endpoint has been provided.') },
    async simulateDispatch() { throw new Error('Dispatch simulation is only available in demo mode. No live dispatch endpoint is connected.') },
  }
}
