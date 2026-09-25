import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDemoData } from '../data/demoData'
import { createHttpApi, validateSnapshot } from './dashboardApi'

const response = body => ({ ok: true, json: async () => structuredClone(body) })
afterEach(() => vi.unstubAllGlobals())

describe('HTTP API boundary', () => {
  it('rejects malformed worker, incident, site and dispatch records before rendering', () => {
    const data = createDemoData()
    expect(validateSnapshot(data)).toEqual(data)
    expect(() => validateSnapshot({ ...data, workers: [{ id: 'broken' }] })).toThrow()
    expect(() => validateSnapshot({ ...data, sites: [{ ...data.sites[0], coordinates: [999, 24] }] })).toThrow()
    expect(() => validateSnapshot({ ...data, incidents: [{ ...data.incidents[0], status: 'unknown' }] })).toThrow()
    expect(() => validateSnapshot({ ...data, incidents: [{ ...data.incidents[0], createdAt: 'invalid' }] })).toThrow()
    expect(() => validateSnapshot({ ...data, dispatches: [{ id: 'bad' }] })).toThrow()
  })
  it('drains event pages and ignores out-of-order incident revisions', async () => {
    const data = createDemoData()
    const changed = { ...data.incidents[0], revision: 3, status: 'reviewing' }
    const fetcher = vi.fn().mockResolvedValueOnce(response(data))
      .mockResolvedValueOnce(response({ incidents: [changed], cursor: '2', hasMore: true }))
      .mockResolvedValueOnce(response({ incidents: [data.incidents[0]], cursor: '3', hasMore: false }))
    vi.stubGlobal('fetch', fetcher)
    const api = createHttpApi()
    await api.loadSnapshot()
    const latest = await api.pollChanges('1')
    expect(latest.cursor).toBe('3')
    expect(latest.incidents[0].revision).toBe(3)
    expect(fetcher.mock.calls[2][0]).toContain('cursor=2')
  })
  it('does not advance its snapshot when a later event page fails', async () => {
    const data = createDemoData()
    const fetcher = vi.fn().mockResolvedValueOnce(response(data))
      .mockResolvedValueOnce(response({ incidents: [{ ...data.incidents[0], revision: 3 }], cursor: '2', hasMore: true }))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(response({ incidents: [], cursor: '1', hasMore: false }))
    vi.stubGlobal('fetch', fetcher)
    const api = createHttpApi()
    await api.loadSnapshot()
    await expect(api.pollChanges('1')).rejects.toThrow('offline')
    expect((await api.pollChanges('1')).incidents[0].revision).toBe(1)
  })
  it('surfaces conflicts and never contacts a real dispatch endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 409 })
    vi.stubGlobal('fetch', fetcher)
    const api = createHttpApi()
    await expect(api.reviewIncident('id', 1, 'reviewing')).rejects.toThrow('changed')
    await expect(api.simulateDispatch()).rejects.toThrow('only available in demo mode')
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
