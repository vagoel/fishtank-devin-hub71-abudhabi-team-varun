import { describe, expect, it } from 'vitest'
import { normalizeIncidentFeed } from './incidentFeed'
import { isActive, newAlerts, siteStatus } from '../domain/incidents'

const report = {
  schema: 'heatguard.incident.v1', incident_id: 'sticks3-7ce8b1-9f3a01c2-17', device_id: 'sticks3-7ce8b1',
  person: { name: 'Ravi Kumar', trade: 'Steel fixer', crew: 'B-2' }, type: 'fall', status: 'suspected', severity: 'critical',
  occurred_at: '2026-09-25T12:41:07Z', location: { lat: 24.8974, lon: 55.161, label: 'Tower B · Level 4' },
  details: { impact_g: 7.5, freefall_ms: 361, message: 'Worker has not responded yet.' }, source: 'device',
}
const now = Date.parse('2026-09-25T12:42:00Z')

describe('heatguard.incident.v1 mapping', () => {
  it.each([report, [report], { incidents: [report] }])('accepts a single report, array, or incidents envelope', payload => {
    const data = normalizeIncidentFeed(payload, undefined, now)
    expect(data.incidents[0]).toMatchObject({ id: report.incident_id, type: 'fall', status: 'suspected', severity: 'critical', source: 'device' })
    expect(data.workers[0]).toMatchObject({ name: 'Ravi Kumar', role: 'Steel fixer', crew: 'B-2', bandId: report.device_id, connectivity: 'unknown', temperature: null, readings: [] })
    expect(data.sites[0]).toMatchObject({ name: 'Tower B · Level 4', coordinates: [55.161, 24.8974], incidentOnly: true, locationIsDemo: false })
    expect(data.incidents[0].evidence.value).toBe('7.5 g')
    expect(data.incidents[0].detailItems).toContainEqual({ label: 'Free-fall duration', value: '361 ms' })
    expect(data.incidents[0].analysis.status).toBe('unavailable')
    expect(data.capabilities.review).toBe(false)
  })
  it('keeps repeated responses stable but observes remote status/severity changes', () => {
    const first = normalizeIncidentFeed([report], undefined, now)
    const same = normalizeIncidentFeed([{ ...report, details: { message: report.details.message, freefall_ms: 361, impact_g: 7.5 } }], first, now + 2000)
    expect(same.incidents[0].revision).toBe(1)
    expect(same.incidents[0].timeline).toEqual(first.incidents[0].timeline)
    expect(newAlerts(first.incidents, same.incidents)).toEqual([])
    const resolved = normalizeIncidentFeed([{ ...report, status: 'resolved' }], same, now + 4000)
    expect(resolved.incidents[0].revision).toBe(2)
    expect(isActive(resolved.incidents[0])).toBe(false)
    expect(resolved.incidents[0].timeline.at(-1).label).toContain('Observed source status')
    expect(newAlerts(same.incidents, resolved.incidents)).toEqual([])
  })
  it.each(['suspected', 'no_response', 'acknowledged'])('keeps %s active', status => {
    expect(isActive(normalizeIncidentFeed([{ ...report, status }], undefined, now).incidents[0])).toBe(true)
  })
  it.each(['worker_ok', 'cancelled', 'resolved'])('preserves the %s terminal outcome instead of calling it a false alarm', status => {
    const incident = normalizeIncidentFeed([{ ...report, status }], undefined, now).incidents[0]
    expect(incident.status).toBe(status)
    expect(isActive(incident)).toBe(false)
  })
  it.each(['fall', 'heat_stroke', 'manual_sos', 'tremor', 'inactivity', 'unwell', 'impact', 'other'])('preserves the %s type', type => {
    expect(normalizeIncidentFeed([{ ...report, type }], undefined, now).incidents[0].type).toBe(type)
  })
  it('supports informational severity and does not invent online-device telemetry', () => {
    const data = normalizeIncidentFeed([{ ...report, severity: 'info' }], undefined, now)
    expect(data.incidents[0].severity).toBe('info')
    expect(siteStatus(data.sites[0], data.incidents, false, now)).toBe('info')
    expect(data.workers[0].lastSeen).toBeNull()
  })
  it('uses clearly marked demo identity/location only when absent', () => {
    const data = normalizeIncidentFeed([{ ...report, person: null, location: null }], undefined, now)
    expect(data.incidents[0]).toMatchObject({ identityIsDemo: true, locationIsDemo: true })
    expect(data.workers[0].name).toContain('Demo')
    expect(data.sites[0].name).toContain('demo assignment')
    expect(data.incidents[0].id).toBe(report.incident_id)
  })
  it('groups a device and coordinate location, retaining incident-specific person/location details', () => {
    const data = normalizeIncidentFeed([report, { ...report, incident_id: 'second', person: { ...report.person, name: 'Another worker' }, location: { ...report.location, label: 'Tower B · Level 5' }, occurred_at: '2026-09-25T12:42:00Z' }], undefined, now)
    expect(data.workers).toHaveLength(1)
    expect(data.sites).toHaveLength(1)
    expect(data.incidents.find(item => item.id === report.incident_id).person.name).toBe('Ravi Kumar')
    expect(data.incidents.find(item => item.id === 'second').location.label).toContain('Level 5')
  })
  it('does not clear a previously reported incident merely because it is absent from a poll', () => {
    const previous = normalizeIncidentFeed([report], undefined, now)
    const next = normalizeIncidentFeed([], previous, now + 2000)
    expect(next.incidents).toHaveLength(1)
    expect(next.incidents[0].status).toBe('suspected')
    expect(normalizeIncidentFeed([], undefined, now).workers).toEqual([])
  })
  it.each([
    { ...report, schema: 'other' }, { ...report, status: 'invalid' }, { ...report, severity: 'high' },
    { ...report, location: { lat: 1000, lon: 55, label: 'bad' } }, { ...report, occurred_at: 'bad' },
    { ...report, details: [] }, { ...report, person: { name: {} } },
  ])('rejects malformed data rather than presenting a fabricated location or status', invalid => {
    expect(() => normalizeIncidentFeed([invalid], undefined, now)).toThrow()
  })
  it('accepts the deployed api source and preserves server escalation and worker metadata as reported', () => {
    const deployed = { ...report, source: 'api', person: { ...report.person, worker_id: 'LIVE-7ce8b1', zone: 'Tower B' }, alert_id: 'A-000005', escalated: ['call', 'whatsapp'], received_at: report.occurred_at, updated_at: report.occurred_at, boot_id: 'boot-1', read_time_us: 1000 }
    const data = normalizeIncidentFeed([deployed], undefined, now)
    expect(data.incidents[0]).toMatchObject({ source: 'api', alertId: 'A-000005', escalated: ['call', 'whatsapp'], sourceUpdatedAt: report.occurred_at, sourceReceivedAt: report.occurred_at, bootId: 'boot-1', deviceReadTimeUs: 1000 })
    expect(data.incidents[0].person.worker_id).toBe('LIVE-7ce8b1')
    expect(data.dispatches).toEqual([])
  })
  it('ignores older server versions rather than reopening a resolved incident', () => {
    const previous = normalizeIncidentFeed([{ ...report, status: 'resolved', updated_at: '2026-09-25T12:43:00Z' }], undefined, now)
    const next = normalizeIncidentFeed([{ ...report, updated_at: '2026-09-25T12:42:00Z' }], previous, now + 2000)
    expect(next.incidents[0].status).toBe('resolved')
    expect(next.incidents[0].revision).toBe(previous.incidents[0].revision)
  })
  it('rejects an unknown envelope, duplicate IDs, and partial/paginated payloads instead of silently dropping incidents', () => {
    expect(() => normalizeIncidentFeed({ data: [report] }, undefined, now)).toThrow()
    expect(() => normalizeIncidentFeed([report, report], undefined, now)).toThrow()
    expect(() => normalizeIncidentFeed({ incidents: [report], next_cursor: 'more' }, undefined, now)).toThrow()
  })
})
