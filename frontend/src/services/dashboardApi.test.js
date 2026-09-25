import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDemoData } from '../data/demoData'
import { createHttpApi, validateSnapshot } from './dashboardApi'

const report = { schema: 'heatguard.incident.v1', incident_id: 'incident-1', device_id: 'band-1', person: { name: 'Ravi Kumar', trade: 'Steel fixer', crew: 'B-2' }, type: 'fall', status: 'suspected', severity: 'warning', occurred_at: '2026-09-25T12:41:07Z', location: { lat: 24.8974, lon: 55.161, label: 'Tower B · Level 4' }, details: { impact_g: 7.5, freefall_ms: 361 }, source: 'device' }
const response = body => ({ ok: true, json: async () => structuredClone(body) })
afterEach(() => vi.unstubAllGlobals())

describe('incident HTTP API boundary', () => {
  it('retains validation for malformed dashboard records', () => {
    const data = createDemoData()
    expect(validateSnapshot(data)).toEqual(data)
    expect(() => validateSnapshot({ ...data, workers: [{ id: 'broken' }] })).toThrow()
    expect(() => validateSnapshot({ ...data, sites: [{ ...data.sites[0], coordinates: [999, 24] }] })).toThrow()
    expect(() => validateSnapshot({ ...data, incidents: [{ ...data.incidents[0], status: 'invalid' }] })).toThrow()
  })
  it('polls only GET /v1/incidents and maps changed source status without inventing a cursor API', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(response([report])).mockResolvedValueOnce(response([{ ...report, status: 'no_response', severity: 'critical' }]))
    vi.stubGlobal('fetch', fetcher)
    const api = createHttpApi()
    const initial = await api.loadSnapshot()
    expect(initial.incidents[0].revision).toBe(1)
    const next = await api.pollChanges(initial.cursor)
    expect(next.incidents[0]).toMatchObject({ status: 'no_response', severity: 'critical', revision: 2 })
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(['/api/v1/incidents', '/api/v1/incidents'])
    expect(fetcher.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
  })
  it('keeps the last valid incident set after a malformed response or outage and never switches to demo data', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(response([report]))
      .mockResolvedValueOnce(response([{ ...report, status: 'resolved' }, { ...report, incident_id: 'bad', severity: 'invalid' }]))
      .mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(response([]))
    vi.stubGlobal('fetch', fetcher)
    const api = createHttpApi()
    await api.loadSnapshot()
    await expect(api.pollChanges()).rejects.toThrow('Invalid')
    await expect(api.pollChanges()).rejects.toThrow('incident API')
    const recovered = await api.pollChanges()
    expect(recovered.incidents).toHaveLength(1)
    expect(recovered.incidents[0].status).toBe('suspected')
    expect(recovered.workers).toHaveLength(1)
  })
  it('explains the not-yet-deployed endpoint and never calls unsupported review/dispatch routes', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 404 })
    vi.stubGlobal('fetch', fetcher)
    const api = createHttpApi()
    await expect(api.loadSnapshot()).rejects.toThrow('/v1/incidents')
    await expect(api.reviewIncident('id', 1, 'reviewing')).rejects.toThrow('read-only')
    await expect(api.simulateDispatch()).rejects.toThrow('demo mode')
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('honors request cancellation and a configurable API base URL', async () => {
    const fetcher = vi.fn().mockResolvedValue(response([]))
    vi.stubGlobal('fetch', fetcher)
    const controller = new AbortController()
    await createHttpApi({ baseUrl: '/incidents-proxy/' }).loadSnapshot(controller.signal)
    expect(fetcher.mock.calls[0][0]).toBe('/incidents-proxy/v1/incidents')
    controller.abort()
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true)
  })
})
