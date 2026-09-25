export const severityRank = { healthy: 0, offline: 0, warning: 1, critical: 2 }
export const statusLabels = { new: 'Awaiting review', reviewing: 'Under review', confirmed: 'Response approved', dispatching: 'Dispatching · demo', dispatched: 'Response active · demo', resolved: 'Resolved', dismissed: 'False alarm' }
export const colors = { healthy: '#69dab0', warning: '#efb35e', critical: '#f87878', offline: '#8393a1' }
export const isActive = incident => !['resolved', 'dismissed'].includes(incident.status)

export function siteStatus(site, incidents, stale = false, now = Date.now()) {
  const active = incidents.filter(incident => incident.siteId === site.id && isActive(incident))
  if (active.some(incident => incident.severity === 'critical')) return 'critical'
  if (active.some(incident => incident.severity === 'warning')) return 'warning'
  return stale || site.connectivity !== 'online' || !Number.isFinite(Date.parse(site.lastSeen)) || now - Date.parse(site.lastSeen) > 30000 ? 'offline' : 'healthy'
}

export function mergeIncidents(previous, incoming) {
  const records = new Map(previous.map(incident => [incident.id, incident]))
  for (const incident of incoming) {
    if (!records.has(incident.id) || incident.revision > records.get(incident.id).revision) records.set(incident.id, incident)
  }
  return [...records.values()].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
}

export function newAlerts(previous, incoming) {
  const records = new Map(previous.map(incident => [incident.id, incident]))
  return incoming.filter(incident => {
    const old = records.get(incident.id)
    return isActive(incident) && (!old || (incident.revision > old.revision && severityRank[incident.severity] > severityRank[old.severity]))
  }).sort((a, b) => severityRank[b.severity] - severityRank[a.severity])
}

const transitions = { new: ['reviewing', 'dismissed', 'confirmed'], reviewing: ['dismissed', 'confirmed'], confirmed: ['dispatching'], dispatching: ['dispatched'], dispatched: ['resolved'], resolved: [], dismissed: [] }

export function transitionIncident(incident, status, { reason = '', services = [] } = {}) {
  if (!transitions[incident.status]?.includes(status)) throw new Error('Invalid incident transition. Refresh and review the current state.')
  if (status === 'dismissed' && reason.trim().length < 5) throw new Error('Please provide a reason of at least 5 characters.')
  if (status === 'confirmed' && (!services.length || services.some(service => !['medical', 'safety', 'rescue'].includes(service)))) throw new Error('Select at least one valid response service.')
  const at = new Date().toISOString()
  return { ...incident, status, revision: incident.revision + 1, updatedAt: at, timeline: [...incident.timeline, { at, label: `${statusLabels[status]}${reason.trim() ? `: ${reason.trim()}` : ''}` }] }
}

export function timeAgo(timestamp, now = Date.now()) {
  const seconds = Math.max(0, Math.floor((now - (typeof timestamp === 'number' ? timestamp : Date.parse(timestamp))) / 1000))
  if (!Number.isFinite(seconds)) return 'Unknown'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

export function localTime(timestamp = Date.now()) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Dubai', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(timestamp))
}
