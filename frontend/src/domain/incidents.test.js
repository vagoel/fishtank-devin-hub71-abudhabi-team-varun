import { describe, expect, it } from 'vitest'
import { isActive, mergeIncidents, newAlerts, siteStatus, timeAgo, transitionIncident } from './incidents'

const incident = { id: 'i1', revision: 1, siteId: 's1', severity: 'critical', status: 'new', timeline: [] }

describe('incident lifecycle', () => {
  it('keeps acknowledged and dispatched incidents active', () => {
    expect(isActive({ ...incident, status: 'reviewing' })).toBe(true)
    expect(isActive({ ...incident, status: 'dispatched' })).toBe(true)
    expect(isActive({ ...incident, status: 'dismissed' })).toBe(false)
  })
  it('requires a reason for a false alarm and prevents invalid transitions', () => {
    expect(() => transitionIncident(incident, 'dismissed')).toThrow('reason')
    expect(() => transitionIncident(incident, 'dispatched')).toThrow('transition')
    const dismissed = transitionIncident(incident, 'dismissed', { reason: 'Band removed during break' })
    expect(dismissed.revision).toBe(2)
    expect(dismissed.timeline[0].label).toContain('Band removed')
    expect(() => transitionIncident(dismissed, 'reviewing')).toThrow('transition')
  })
  it('requires explicit selected services to confirm dispatch', () => {
    expect(() => transitionIncident(incident, 'confirmed')).toThrow('service')
    expect(transitionIncident(incident, 'confirmed', { services: ['medical'] }).status).toBe('confirmed')
  })
})

describe('site health', () => {
  const site = { id: 's1', lastSeen: new Date().toISOString(), connectivity: 'online' }
  it('uses highest unresolved severity, not the selected incident', () => {
    expect(siteStatus(site, [incident, { ...incident, id: 'i2', severity: 'warning' }])).toBe('critical')
    expect(siteStatus(site, [{ ...incident, status: 'resolved' }])).toBe('healthy')
  })
  it('does not claim stale or offline data is healthy', () => {
    expect(siteStatus({ ...site, connectivity: 'offline' }, [])).toBe('offline')
    expect(siteStatus({ ...site, lastSeen: '2000-01-01T00:00:00Z' }, [])).toBe('offline')
    expect(siteStatus(site, [], true)).toBe('offline')
    expect(siteStatus(site, [incident], true)).toBe('critical')
  })
})

describe('timestamp formatting', () => {
  it('accepts numeric sync timestamps and ISO sensor timestamps', () => {
    const now = Date.now()
    expect(timeAgo(now - 2000, now)).toBe('2s ago')
    expect(timeAgo(new Date(now - 120000).toISOString(), now)).toBe('2m ago')
  })
})

describe('polling reconciliation', () => {
  it('ignores repeated and older revisions', () => {
    const previous = [{ ...incident, revision: 3, status: 'reviewing' }]
    expect(mergeIncidents(previous, [incident])[0]).toEqual(previous[0])
    expect(mergeIncidents(previous, [{ ...incident, revision: 4 }])[0].revision).toBe(4)
  })
  it('notifies only new active incidents or severity increases', () => {
    expect(newAlerts([incident], [incident])).toEqual([])
    expect(newAlerts([incident], [{ ...incident, revision: 2, analysis: {} }])).toEqual([])
    expect(newAlerts([{ ...incident, severity: 'warning' }], [{ ...incident, revision: 2 }])).toHaveLength(1)
    expect(newAlerts([], [{ ...incident, status: 'resolved' }])).toEqual([])
    expect(newAlerts([], [incident])).toEqual([incident])
  })
})
