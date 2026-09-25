import { siteLocations } from '../data/abuDhabi'
import { statusLabels } from '../domain/incidents'

const types = ['fall', 'heat_stroke', 'manual_sos', 'tremor', 'inactivity', 'unwell', 'impact', 'other']
const statuses = ['suspected', 'no_response', 'worker_ok', 'cancelled', 'acknowledged', 'resolved']
const severities = ['critical', 'warning', 'info']
const sources = ['device', 'voice', 'server', 'api']
const plainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = value => typeof value === 'string' && value.trim().length > 0
const optionalText = value => value == null || typeof value === 'string'
const validDate = value => typeof value === 'string' && Number.isFinite(Date.parse(value))
const fallbackSite = siteLocations[0]

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (!plainObject(value)) return value
  return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]))
}

function extractReports(payload) {
  if (Array.isArray(payload)) return payload
  if (plainObject(payload) && Array.isArray(payload.incidents)) {
    if (payload.hasMore || payload.has_more || payload.next_cursor || payload.next) throw new Error('The incident feed is paginated. Its pagination contract must be configured before all incidents can be displayed.')
    return payload.incidents
  }
  if (plainObject(payload) && payload.schema === 'heatguard.incident.v1') return [payload]
  throw new Error('Expected heatguard.incident.v1 reports as an array, an incidents envelope, or a single incident.')
}

function validateReport(report) {
  if (!plainObject(report) || report.schema !== 'heatguard.incident.v1' || !text(report.incident_id) || !text(report.device_id)) throw new Error('Invalid incident identity or schema in the API response.')
  if (!types.includes(report.type) || !statuses.includes(report.status) || !severities.includes(report.severity) || !sources.includes(report.source) || !validDate(report.occurred_at)) throw new Error(`Invalid type, status, severity, source, or timestamp for incident ${report.incident_id}.`)
  if (report.person != null && (!plainObject(report.person) || !['name', 'trade', 'crew', 'worker_id', 'zone'].every(key => optionalText(report.person[key])))) throw new Error('Invalid person details in the incident response.')
  if (report.location != null) {
    const location = report.location
    const missingCoordinates = location.lat == null && location.lon == null
    if (!plainObject(location) || !['label', 'source', 'maps_url'].every(key => optionalText(location[key])) || (!missingCoordinates && (!Number.isFinite(location.lat) || !Number.isFinite(location.lon) || Math.abs(location.lat) > 85 || Math.abs(location.lon) > 180))) throw new Error('Invalid incident location. A reported location will not be replaced with invented coordinates.')
  }
  if (report.details != null && (!plainObject(report.details) || !optionalText(report.details.message))) throw new Error('Invalid incident details in the API response.')
  for (const key of ['impact_g', 'freefall_ms']) if (report.details?.[key] != null && (!Number.isFinite(report.details[key]) || report.details[key] < 0)) throw new Error(`Invalid ${key} measurement in the incident response.`)
  for (const key of ['received_at', 'updated_at']) if (report[key] != null && !validDate(report[key])) throw new Error(`Invalid ${key} in the incident response.`)
  if (!optionalText(report.alert_id) || !optionalText(report.boot_id) || (report.read_time_us != null && (!Number.isFinite(report.read_time_us) || report.read_time_us < 0)) || (report.escalated != null && (!Array.isArray(report.escalated) || !report.escalated.every(text)))) throw new Error('Invalid incident metadata in the API response.')
}

function detailItems(details) {
  const labels = { impact_g: ['Impact force', 'g'], freefall_ms: ['Free-fall duration', 'ms'], message: ['Message', ''], die_c: ['IMU die temperature', '°C'], air_c: ['Reported air temperature', '°C'] }
  return Object.entries(details).map(([key, value]) => {
    const [label, unit] = Object.hasOwn(labels, key) ? labels[key] : [key.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase()), '']
    const rendered = value == null ? 'Not provided' : typeof value === 'object' ? JSON.stringify(value) : String(value)
    return { label, value: `${rendered}${unit && value != null ? ` ${unit}` : ''}` }
  })
}

export function normalizeIncidentFeed(payload, previous, now = Date.now()) {
  const reports = extractReports(payload)
  const ids = new Set()
  reports.forEach(report => {
    validateReport(report)
    if (ids.has(report.incident_id)) throw new Error('Duplicate incident IDs in the API response.')
    ids.add(report.incident_id)
  })
  const receivedAt = new Date(now).toISOString()
  const records = new Map((previous?.incidents || []).map(incident => [incident.id, incident]))
  for (const report of reports) {
    const old = records.get(report.incident_id)
    if (old?.sourceUpdatedAt && report.updated_at && Date.parse(report.updated_at) < Date.parse(old.sourceUpdatedAt)) continue
    const identityIsDemo = !text(report.person?.name)
    const locationIsDemo = report.location?.lat == null && report.location?.lon == null
    const person = { ...report.person, name: identityIsDemo ? 'Demo worker (unassigned device)' : report.person.name, trade: report.person?.trade || 'Trade not provided', crew: report.person?.crew || 'Crew not provided' }
    const location = locationIsDemo
      ? { lat: fallbackSite.coordinates[1], lon: fallbackSite.coordinates[0], label: `${report.location?.label || fallbackSite.name} (demo assignment)`, source: 'demo assignment' }
      : { ...report.location, label: report.location.label || `${report.location.lat.toFixed(4)}, ${report.location.lon.toFixed(4)}` }
    const siteId = locationIsDemo ? 'demo-assignment:AD-01' : `location:${location.lat},${location.lon}`
    const details = { ...report.details }
    const escalated = [...(report.escalated || [])]
    const alertId = report.alert_id || null
    const signature = JSON.stringify(canonical({ type: report.type, status: report.status, severity: report.severity, occurredAt: report.occurred_at, deviceId: report.device_id, person, location, identityIsDemo, locationIsDemo, details, source: report.source, escalated, alertId }))
    const changed = !old || old.feedSignature !== signature
    const timeline = old ? [...old.timeline] : [{ at: report.occurred_at, label: `Incident occurred · reported by ${report.source}` }]
    if (!old) timeline.push({ at: receivedAt, label: `First observed in dashboard · ${statusLabels[report.status]}` })
    else if (old.status !== report.status) timeline.push({ at: receivedAt, label: `Observed source status: ${statusLabels[report.status]}` })
    else if (changed) timeline.push({ at: receivedAt, label: 'Updated incident details observed in the feed' })
    const evidence = Number.isFinite(details.impact_g)
      ? { label: 'Reported impact force', value: `${details.impact_g} g`, source: `Incident report · ${report.source}` }
      : { label: 'Reported event', value: report.type === 'manual_sos' ? 'SOS' : 'Source report', source: `Incident report · ${report.source}` }
    records.set(report.incident_id, {
      id: report.incident_id, workerId: report.device_id, deviceId: report.device_id, siteId,
      type: report.type, severity: report.severity, status: report.status, source: report.source,
      createdAt: report.occurred_at, updatedAt: changed ? receivedAt : old.updatedAt, lastObservedAt: receivedAt,
      sourceUpdatedAt: report.updated_at || null, sourceReceivedAt: report.received_at || null,
      alertId, escalated, bootId: report.boot_id || null, deviceReadTimeUs: report.read_time_us ?? null,
      revision: old ? old.revision + (changed ? 1 : 0) : 1, feedSignature: signature,
      person, location, identityIsDemo, locationIsDemo, incidentOnly: true,
      details, detailItems: detailItems(details), evidence,
      analysis: { status: 'unavailable', summary: '', recommendation: '', source: 'The incident endpoint does not include a Devin assessment.' },
      timeline: timeline.slice(-100),
    })
  }
  const incidents = [...records.values()].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
  const workersById = new Map()
  const sitesById = new Map()
  for (const incident of incidents) {
    if (!workersById.has(incident.workerId)) workersById.set(incident.workerId, {
      id: incident.workerId, bandId: incident.deviceId, officialWorkerId: incident.person.worker_id || null, zone: incident.person.zone || null,
      name: incident.person.name, role: incident.person.trade, crew: incident.person.crew,
      siteId: incident.siteId, connectivity: 'unknown', lastSeen: null, latestIncidentAt: incident.createdAt,
      temperature: null, readings: [], incidentOnly: true, identityIsDemo: incident.identityIsDemo, locationIsDemo: incident.locationIsDemo,
    })
    const site = sitesById.get(incident.siteId)
    if (!site) sitesById.set(incident.siteId, {
      id: incident.siteId, name: incident.location.label, district: incident.locationIsDemo ? 'Demo location assignment' : 'Reported incident location',
      code: `L${String(sitesById.size + 1).padStart(2, '0')}`, coordinates: [incident.location.lon, incident.location.lat],
      connectivity: 'unknown', lastSeen: incident.lastObservedAt, incidentOnly: true, locationIsDemo: incident.locationIsDemo,
    })
    else if (Date.parse(incident.lastObservedAt) > Date.parse(site.lastSeen)) site.lastSeen = incident.lastObservedAt
  }
  return {
    sites: [...sitesById.values()], workers: [...workersById.values()], incidents, dispatches: [], cursor: null,
    serverTime: null, receivedAt, feedEntries: reports.length, capabilities: { review: false, dispatch: false, telemetry: false },
  }
}
