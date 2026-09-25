import { siteLocations } from './abuDhabi'

export const incidentTypes = {
  fall: { label: 'Possible fall detected', short: 'Possible fall', severity: 'critical', measurement: 'Abrupt orientation change', value: '84°', services: ['medical', 'safety', 'rescue'] },
  temperature: { label: 'Elevated band temperature', short: 'Temperature alert', severity: 'warning', measurement: 'Band surface temperature', value: '39.1 °C', services: ['medical', 'safety'] },
  distress: { label: 'Worker assistance requested', short: 'Assistance request', severity: 'warning', measurement: 'Manual distress event', value: 'SOS', services: ['safety', 'medical'] },
  heat_stroke: { label: 'Reported heat-stroke alert', short: 'Heat-stroke alert', services: ['medical', 'safety'] },
  manual_sos: { label: 'Manual SOS received', short: 'Manual SOS', services: ['safety', 'medical'] },
  tremor: { label: 'Tremor reported', short: 'Tremor', services: ['medical', 'safety'] },
  inactivity: { label: 'Inactivity reported', short: 'Inactivity', services: ['safety'] },
  unwell: { label: 'Worker reported feeling unwell', short: 'Worker unwell', services: ['medical', 'safety'] },
  impact: { label: 'Impact reported', short: 'Impact', services: ['safety', 'medical'] },
  other: { label: 'Safety incident reported', short: 'Safety incident', services: ['safety'] },
}

export const responderServices = [
  { id: 'medical', name: 'Medical response', unit: 'Ambulance unit', description: 'Assessment and medical assistance', color: '#6daaff' },
  { id: 'safety', name: 'Site safety team', unit: 'On-site response', description: 'Local verification and site access', color: '#65d9ad' },
  { id: 'rescue', name: 'Civil Defence / rescue', unit: 'Rescue unit', description: 'Rescue support if the operator determines it is needed', color: '#ffb45b' },
]

const firstNames = ['Arjun', 'Mohammed', 'Ravi', 'Faisal', 'Ramesh', 'Ahmed', 'Bilal', 'Kumar', 'Imran', 'Suresh', 'Omar', 'Hassan']
const lastNames = ['Sharma', 'Khan', 'Kumar', 'Ali', 'Patel', 'Hussain']
const roles = ['Steel fixer', 'Site electrician', 'Scaffolder', 'Mason', 'Crane operator', 'General operative']

export function makeIncident(type, siteId, workerId, sequence, now = Date.now()) {
  const details = incidentTypes[type]
  return {
    id: `INC-${String(sequence).padStart(4, '0')}`, revision: 1, siteId, workerId, type,
    severity: details.severity, status: 'new', createdAt: new Date(now).toISOString(), updatedAt: new Date(now).toISOString(),
    evidence: { label: details.measurement, value: details.value, source: type === 'distress' ? 'Simulated manual report' : 'S3 Stick · simulated sensor data' },
    analysis: { status: 'pending', summary: '', recommendation: '' },
    timeline: [{ at: new Date(now).toISOString(), label: `${details.short} received from demo device` }],
  }
}

export function demoAnalysis(type) {
  const text = {
    fall: ['A sharp orientation change followed by limited motion was reported. This may indicate a fall, but the sensor reading alone cannot confirm an injury.', 'Ask the site supervisor to check on the worker. Review the event and confirm the appropriate response.'],
    temperature: ['The band reports a rising surface temperature. Sun exposure or direct contact can affect this reading; it is not a core body-temperature measurement.', 'Contact the worker and verify their condition. A human assessment is required before escalation.'],
    distress: ['A manual assistance event was received. No additional symptoms or cause have been verified.', 'Contact the worker or site safety team to establish what happened before selecting responders.'],
  }
  return { status: 'ready', summary: text[type][0], recommendation: text[type][1], source: 'Demo analysis · not a live Devin result' }
}

export function createDemoData(now = Date.now()) {
  const sites = siteLocations.map((site, i) => ({ ...site, connectivity: i === 5 ? 'offline' : 'online', lastSeen: new Date(now - (i === 5 ? 360000 : 0)).toISOString() }))
  const workers = sites.flatMap((site, s) => Array.from({ length: 12 }, (_, i) => ({
    id: `WK-${String(s * 12 + i + 1).padStart(3, '0')}`, bandId: `S3-${String(s * 12 + i + 101).padStart(4, '0')}`,
    name: `${firstNames[i]} ${lastNames[(s + i) % lastNames.length]}`, role: roles[(s + i) % roles.length], siteId: site.id,
    lastSeen: site.lastSeen, connectivity: site.connectivity, temperature: Number((35.4 + (i % 7) * 0.15).toFixed(1)),
    readings: Array.from({ length: 16 }, (_, t) => Number((35.5 + Math.sin((i + t) * 0.8) * 0.35).toFixed(1))),
    shift: '07:00–16:00',
  })))
  workers[0].temperature = 39.1
  workers[0].readings = [35.8, 35.9, 36.1, 36, 36.3, 36.5, 36.8, 36.7, 37, 37.2, 37.5, 37.9, 38.1, 38.5, 38.9, 39.1]
  const incidents = [
    makeIncident('fall', 'AD-02', 'WK-014', 1042, now - 85000),
    makeIncident('temperature', 'AD-01', 'WK-001', 1041, now - 248000),
    makeIncident('distress', 'AD-03', 'WK-027', 1040, now - 432000),
  ].map(incident => ({ ...incident, analysis: demoAnalysis(incident.type), timeline: [...incident.timeline, { at: new Date(Date.parse(incident.createdAt) + 4000).toISOString(), label: 'Demo analysis available for human review' }] }))
  return { sites, workers, incidents, dispatches: [], cursor: '1', serverTime: new Date(now).toISOString() }
}
