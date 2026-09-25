import { incidentTypes } from '../data/demoData'
import { isActive, statusLabels } from '../domain/incidents'

function safe(value, limit = 110) { return String(value ?? 'Not available').replace(/[\r\n<>]/g, ' ').slice(0, limit) }

export function incidentBriefing(incident, dashboard) {
  if (incident.summary) return safe(incident.summary, 350)
  const site = dashboard.sites.find(item => item.id === incident.siteId)
  const assignment = `${incident.identityIsDemo ? 'The worker identity is a demo assignment. ' : ''}${incident.locationIsDemo ? 'The location is a demo assignment, not a reported position. ' : ''}`
  const person = incident.person?.name || incident.workerId
  const message = incident.details?.message ? `Source message: ${safe(incident.details.message, 150)}. ` : ''
  const escalations = incident.escalated?.length ? `Backend reports escalation to ${safe(incident.escalated.join(', '), 80)}; delivery is not verified here. ` : ''
  return `${dashboard.mode === 'demo' ? 'Demo incident. ' : 'Source-reported incident. '}${safe(incident.severity)}: ${safe(incidentTypes[incident.type]?.label || incident.type)}. Worker ${safe(person, 80)} at ${safe(incident.location?.label || site?.name)}. ${assignment}Reported evidence: ${safe(incident.evidence?.value, 40)}. ${message}${escalations}Human review required. No emergency services have been contacted by this dashboard.`
}

export function currentVoiceContext(dashboard, selectedIncidentId) {
  const active = dashboard.incidents.filter(isActive)
  const incident = dashboard.incidents.find(item => item.id === selectedIncidentId) || active[0]
  const mode = dashboard.mode === 'demo' ? 'Demo dashboard with fictional data.' : 'Read-only incident API. Current vitals, device connectivity, and Devin assessments are not supplied.'
  const header = `${mode} ${active.length} active incidents. Feed data ${dashboard.stale ? 'is stale; do not imply current readings' : 'was fetched successfully; event age is separate'}. `
  if (!incident) return `${header}No active incident is selected. No real dispatch integration is connected.`
  const analysis = incident.analysis?.status === 'unavailable' ? 'Not provided by this endpoint' : incident.analysis?.status === 'ready' ? safe(incident.analysis.summary, 240) : 'Not ready'
  return `${header}${incidentBriefing(incident, dashboard)} Source status: ${safe(statusLabels[incident.status])}. Analysis: ${analysis}`.slice(0, 1050)
}

export function delegationContext(event, dashboard, selectedIncidentId) {
  if (event.delegation?.target !== 'client' || !event.delegation.id) return null
  return { delegationId: event.delegation.id, content: `Read-only context: ${currentVoiceContext(dashboard, selectedIncidentId)} Requests beyond these facts need the operator or an API capability that is not connected. No action was taken.`.slice(0, 1100) }
}
