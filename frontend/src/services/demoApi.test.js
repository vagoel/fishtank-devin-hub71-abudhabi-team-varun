import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDemoApi } from './demoApi'
import { siteStatus } from '../domain/incidents'

afterEach(() => vi.useRealTimers())

describe('demo adapter', () => {
  it('builds a deterministic fictional roster and isolates instances', async () => {
    const api = createDemoApi()
    const snapshot = await api.loadSnapshot()
    expect(snapshot.workers).toHaveLength(72)
    expect(snapshot.sites).toHaveLength(6)
    snapshot.workers[0].name = 'changed'
    expect((await api.loadSnapshot()).workers[0].name).not.toBe('changed')
    await api.trigger('fall')
    expect((await createDemoApi().loadSnapshot()).incidents).toHaveLength(3)
  })
  it('does not let a stale approval dispatch and never clears other active incidents', async () => {
    const api = createDemoApi()
    const original = (await api.loadSnapshot()).incidents[0]
    await api.reviewIncident(original.id, original.revision, 'reviewing')
    await expect(api.simulateDispatch(original.id, original.revision, ['medical'])).rejects.toThrow('changed')
    const reviewed = (await api.loadSnapshot()).incidents[0]
    const dispatch = await api.simulateDispatch(reviewed.id, reviewed.revision, ['medical', 'rescue'])
    expect(dispatch.dispatches[0].simulated).toBe(true)
    expect(dispatch.incidents[0].status).toBe('dispatching')
    expect(siteStatus(dispatch.sites[1], dispatch.incidents)).toBe('critical')
  })
  it('advances only a simulated response and supports reset', async () => {
    vi.useFakeTimers()
    const api = createDemoApi()
    const incident = (await api.loadSnapshot()).incidents[0]
    await api.simulateDispatch(incident.id, incident.revision, ['medical'])
    await vi.advanceTimersByTimeAsync(7000)
    expect((await api.pollChanges()).incidents[0].status).toBe('dispatched')
    const reset = await api.reset()
    expect(reset.dispatches).toHaveLength(0)
    expect(reset.incidents[0].status).toBe('new')
  })
  it('does not reuse new-event IDs after reset, so repeated demos can announce them again', async () => {
    const api = createDemoApi()
    const first = (await api.trigger('fall')).incidents[0].id
    await api.reset()
    expect((await api.trigger('fall')).incidents[0].id).not.toBe(first)
  })
  it('returns pending analysis for a new event before its demo analysis arrives', async () => {
    vi.useFakeTimers()
    const api = createDemoApi()
    const data = await api.trigger('temperature', 'AD-01')
    expect(data.incidents[0].analysis.status).toBe('pending')
    await vi.advanceTimersByTimeAsync(4000)
    expect((await api.pollChanges()).incidents[0].analysis.status).toBe('ready')
  })
})
