import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createGptLiveVoice } from './gptLiveVoice'

let peers
let track
let fetcher
let onState
let onEvent
let onError
let clients

class MockChannel extends EventTarget {
  readyState = 'open'
  sent = []
  send(data) { this.sent.push(JSON.parse(data)) }
  close() { this.readyState = 'closed'; this.dispatchEvent(new Event('close')) }
  emit(event) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(event) })) }
}

class MockPeer extends EventTarget {
  iceGatheringState = 'complete'
  connectionState = 'connected'
  constructor() { super(); peers.push(this) }
  addTrack() {}
  createDataChannel() { this.channel = new MockChannel(); return this.channel }
  async createOffer() { return { type: 'offer', sdp: 'v=0\r\na=mock' } }
  async setLocalDescription(offer) { this.localDescription = offer }
  async setRemoteDescription(answer) { this.remoteDescription = answer }
  close() { this.connectionState = 'closed' }
}

beforeEach(() => {
  vi.useFakeTimers()
  peers = []
  clients = []
  track = { stop: vi.fn(), enabled: true }
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) } })
  vi.stubGlobal('RTCPeerConnection', MockPeer)
  vi.stubGlobal('Audio', class { play = vi.fn().mockResolvedValue(); pause = vi.fn(); muted = false })
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ session: { id: 'live_test' }, transport: { type: 'webrtc', sdp: 'v=0\r\na=answer' } }) })
  onState = vi.fn(); onEvent = vi.fn(); onError = vi.fn()
})
afterEach(() => { clients.forEach(client => client.dispose()); vi.useRealTimers(); vi.unstubAllGlobals() })

function client() {
  const value = createGptLiveVoice({ endpoint: '/__dev/voice/session', onState, onEvent, onError, fetcher })
  clients.push(value)
  return value
}

describe('GPT-Live WebRTC protocol', () => {
  it('does not capture audio until requested and gates commands on session.started', async () => {
    const voice = client()
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    await voice.start()
    expect(fetcher).toHaveBeenCalledOnce()
    expect(peers[0].remoteDescription.type).toBe('answer')
    expect(voice.append('commentary', 'A new incident needs review.')).toBe(false)
    expect(peers[0].channel.sent).toEqual([])
    peers[0].channel.emit({ type: 'session.started', session: { id: 'live_test' } })
    expect(voice.append('commentary', 'A new incident needs review.')).toBe(true)
    expect(peers[0].channel.sent[0]).toMatchObject({ type: 'session.commentary.append', delegation_id: null, content: 'A new incident needs review.' })
    expect(peers[0].channel.sent.some(event => event.type === 'session.start' || event.type === 'response.create')).toBe(false)
  })
  it('preserves simultaneous transcript events and does not confuse context acknowledgments with playback', async () => {
    const voice = client()
    await voice.start()
    const input = { type: 'session.input_transcript.delta', delta: 'Where is', start_ms: 100, end_ms: 200 }
    const output = { type: 'session.output_transcript.delta', delta: 'Saadiyat.', start_ms: 120, end_ms: 250 }
    peers[0].channel.emit(input)
    peers[0].channel.emit(output)
    peers[0].channel.emit({ type: 'session.commentary.appended', client_event_id: 'unrelated' })
    expect(onEvent).toHaveBeenCalledWith(input)
    expect(onEvent).toHaveBeenCalledWith(output)
    expect(onState).not.toHaveBeenCalledWith('spoken')
  })
  it('mutes capture locally and waits for graceful finalization before releasing transports', async () => {
    const voice = client()
    await voice.start()
    peers[0].channel.emit({ type: 'session.started' })
    voice.muteInput(true)
    expect(track.enabled).toBe(false)
    expect(peers[0].channel.sent.at(-1).type).toBe('session.input_audio.mute')
    voice.muteInput(false)
    expect(track.enabled).toBe(true)
    voice.close()
    expect(peers[0].channel.sent.at(-1).type).toBe('session.close')
    expect(peers[0].connectionState).not.toBe('closed')
    expect(voice.append('thinking', 'Late context')).toBe(false)
    peers[0].channel.emit({ type: 'session.closed', usage: { seconds: 17 } })
    expect(track.stop).toHaveBeenCalled()
    expect(peers[0].connectionState).toBe('closed')
    expect(onState).toHaveBeenLastCalledWith('disconnected')
  })
  it('cleans up on denied microphone permission without creating a paid session', async () => {
    navigator.mediaDevices.getUserMedia.mockRejectedValue(new DOMException('Denied', 'NotAllowedError'))
    await client().start()
    expect(fetcher).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('permission was denied'))
  })
  it('reports missing access without falling back to Realtime', async () => {
    fetcher.mockResolvedValue({ ok: false, json: async () => ({ error: 'GPT-Live-1 access was denied.' }) })
    await client().start()
    expect(onError).toHaveBeenCalledWith('GPT-Live-1 access was denied.')
    expect(fetcher).toHaveBeenCalledOnce()
    expect(track.stop).toHaveBeenCalled()
  })
  it('times out incomplete finalization without claiming final usage', async () => {
    const voice = client()
    await voice.start()
    peers[0].channel.emit({ type: 'session.started' })
    voice.close()
    await vi.advanceTimersByTimeAsync(15000)
    expect(onError).toHaveBeenCalledWith(expect.stringContaining('not confirmed'))
    expect(track.stop).toHaveBeenCalled()
  })
  it('releases a microphone acquired after the component was disposed', async () => {
    let resolve
    navigator.mediaDevices.getUserMedia.mockImplementation(() => new Promise(done => { resolve = done }))
    const voice = client()
    const start = voice.start()
    voice.dispose()
    resolve({ getTracks: () => [track] })
    await start
    expect(track.stop).toHaveBeenCalled()
    expect(fetcher).not.toHaveBeenCalled()
  })
})
