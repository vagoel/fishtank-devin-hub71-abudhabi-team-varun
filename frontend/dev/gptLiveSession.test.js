import { Readable } from 'node:stream'
import { describe, expect, it, vi } from 'vitest'
import { createSessionHandler } from './gptLiveSession'

async function call(handler, { method = 'POST', origin = 'http://127.0.0.1:5175', address = '127.0.0.1', body = { sdp: 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111' }, type = 'application/json' } = {}) {
  const request = Readable.from([typeof body === 'string' ? body : JSON.stringify(body)])
  request.url = '/__dev/voice/session'
  request.method = method
  request.headers = { origin, host: '127.0.0.1:5175', 'content-type': type }
  request.socket = { remoteAddress: address }
  const result = {}
  const response = { writeHead(status, headers) { result.status = status; result.headers = headers }, end(data) { result.body = JSON.parse(data) } }
  await handler(request, response, vi.fn())
  return result
}

describe('local GPT-Live session relay', () => {
  it('keeps the key server-side and uses only the Live API with client delegation', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ session: { id: 'live_test', hidden: 'omit' }, transport: { sdp: 'answer', type: 'webrtc' }, secret: 'not-returned' }) })
    const result = await call(createSessionHandler({ apiKey: 'test-sentinel-not-a-key', fetcher }))
    expect(result.status).toBe(201)
    expect(result.headers['Cache-Control']).toBe('no-store')
    expect(result.body).toEqual({ session: { id: 'live_test' }, transport: { type: 'webrtc', sdp: 'answer' } })
    const [url, options] = fetcher.mock.calls[0]
    expect(url).toBe('https://api.openai.com/v1/live/sessions')
    const request = JSON.parse(options.body)
    expect(request.session.model).toBe('gpt-live-1')
    expect(request.session.delegation.type).toBe('client')
    expect(request.session.store).toBe(false)
    expect(request.session.audio.format).toBeUndefined()
    expect(JSON.stringify(result)).not.toContain('test-sentinel')
  })
  it('validates locality, origin, method, and JSON before calling OpenAI', async () => {
    const fetcher = vi.fn()
    const handler = createSessionHandler({ apiKey: 'test', fetcher })
    expect((await call(handler, { address: '192.168.1.2' })).status).toBe(403)
    expect((await call(handler, { origin: 'http://unrelated.example' })).status).toBe(403)
    expect((await call(handler, { method: 'GET' })).status).toBe(405)
    expect((await call(handler, { type: 'text/plain' })).status).toBe(415)
    expect((await call(handler, { body: { sdp: '' } })).status).toBe(400)
    expect((await call(handler, { body: 'bad-json' })).status).toBe(400)
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('handles a missing key and sanitizes denied access', async () => {
    expect((await call(createSessionHandler({}))).status).toBe(503)
    const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({ error: 'secret details' }) })
    const result = await call(createSessionHandler({ apiKey: 'test', fetcher }))
    expect(result.status).toBe(502)
    expect(result.body.error).toContain('access was denied')
    expect(JSON.stringify(result)).not.toContain('secret details')
  })
  it('limits session attempts and rejects oversized payloads', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 429 })
    const handler = createSessionHandler({ apiKey: 'test', fetcher })
    expect((await call(handler, { body: 'a'.repeat(66000) })).status).toBe(413)
    for (let i = 0; i < 5; i++) await call(handler)
    const result = await call(handler)
    expect(result.status).toBe(429)
    expect(fetcher).toHaveBeenCalledTimes(5)
  })
})
