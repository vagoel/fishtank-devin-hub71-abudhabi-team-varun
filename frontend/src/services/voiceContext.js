import { incidentTypes } from '../data/demoData'
import { isActive, statusLabels } from '../domain/incidents'

function safe(value, limit = 110) { return String(value || 'Not available').replace(/[\r\n<>]/g, ' ').slice(0, limit) }

export function incidentBriefing(incident, dashboard) {
  if (incident.summary) return safe(incident.summary, 350)
  const site = dashboard.sites.find(item => item.id === incident.siteId)
  return `${dashboard.mode === 'demo' ? 'Demo incident. ' : ''}${safe(incident.severity)}: ${safe(incidentTypes[incident.type]?.label || incident.type)}. Worker ${safe(incident.workerId, 20)} at ${safe(site?.name)}. Reported evidence: ${safe(incident.evidence?.value, 40)}. Human review required. No emergency services have been contacted.`
}

export function currentVoiceContext(dashboard, selectedIncidentId) {
  const active = dashboard.incidents.filter(isActive)
  const incident = dashboard.incidents.find(item => item.id === selectedIncidentId) || active[0]
  const header = `${dashboard.mode === 'demo' ? 'Demo dashboard with fictional data.' : 'API dashboard.'} ${active.length} active incidents. Data ${dashboard.stale ? 'is stale; do not imply current readings' : 'is current'}. `
  if (!incident) return `${header}No active incident is selected. No real dispatch integration is connected.`
  return `${header}${incidentBriefing(incident, dashboard)} Review status: ${safe(statusLabels[incident.status])}. Analysis: ${incident.analysis?.status === 'ready' ? safe(incident.analysis.summary, 240) : 'Not ready'}`.slice(0, 950)
}

export function delegationContext(event, dashboard, selectedIncidentId) {
  if (event.delegation?.target !== 'client' || !event.delegation.id) return null
  return { delegationId: event.delegation.id, content: `Read-only context: ${currentVoiceContext(dashboard, selectedIncidentId)} Requests beyond these facts need the operator or an API capability that is not connected. No action was taken.`.slice(0, 1100) }
}
