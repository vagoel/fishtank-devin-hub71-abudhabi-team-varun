export function createGptLiveVoice({ endpoint, onState, onEvent, onError, onActivity, fetcher = fetch }) {
  let peer
  let channel
  let microphone
  let speaker
  let analyserContext
  let animation
  let ready = false
  let closing = false
  let disposed = false
  let started = false
  let sequence = 0
  let timeout
  let startedTimer
  let abort
  let inputMuted = false
  let outputMuted = false
  const pending = new Map()

  function emit(type, content, delegationId = null) {
    if (!ready || closing || channel?.readyState !== 'open') return false
    const eventId = `aman-${++sequence}`
    const event = { type, event_id: eventId }
    if (content !== undefined) { event.content = String(content).slice(0, 1100); event.delegation_id = delegationId }
    pending.set(eventId, type)
    if (pending.size > 100) pending.delete(pending.keys().next().value)
    channel.send(JSON.stringify(event))
    return true
  }

  function cleanup() {
    ready = false
    disposed = true
    clearTimeout(timeout)
    clearTimeout(startedTimer)
    abort?.abort()
    cancelAnimationFrame(animation)
    microphone?.getTracks().forEach(track => track.stop())
    channel?.close()
    peer?.close()
    if (speaker) { speaker.pause(); speaker.srcObject = null }
    analyserContext?.close().catch(() => {})
    pending.clear()
    onActivity?.({ input: 0, output: 0 })
  }

  function monitor(input, output) {
    try {
      analyserContext = new AudioContext()
      const analysers = [input, output].map(stream => {
        const analyser = analyserContext.createAnalyser()
        analyser.fftSize = 256
        analyserContext.createMediaStreamSource(stream).connect(analyser)
        return analyser
      })
      const samples = new Uint8Array(128)
      let last = 0
      const tick = time => {
        if (disposed) return
        if (time - last > 100) {
          const levels = analysers.map(analyser => { analyser.getByteTimeDomainData(samples); return Math.min(1, Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length) * 8) })
          onActivity?.({ input: inputMuted ? 0 : levels[0], output: outputMuted ? 0 : levels[1] })
          last = time
        }
        animation = requestAnimationFrame(tick)
      }
      animation = requestAnimationFrame(tick)
    } catch { onActivity?.({ input: 0, output: 0 }) }
  }

  async function start() {
    if (started || disposed) return
    started = true
    onState('connecting')
    abort = new AbortController()
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone access requires HTTPS or localhost.')
      microphone = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      if (disposed) { microphone.getTracks().forEach(track => track.stop()); return }
      peer = new RTCPeerConnection()
      speaker = new Audio()
      speaker.autoplay = true
      peer.addEventListener('track', event => {
        const remote = new MediaStream([event.track])
        speaker.srcObject = remote
        speaker.play().catch(() => onError('Audio playback was blocked. Use the speaker control to enable playback.'))
        monitor(microphone, remote)
      })
      microphone.getTracks().forEach(track => peer.addTrack(track, microphone))
      channel = peer.createDataChannel('oai-events')
      channel.addEventListener('message', ({ data }) => {
        let event
        try { event = JSON.parse(data) } catch { return }
        if (event.type === 'session.started') { ready = true; clearTimeout(startedTimer); onState('connected') }
        if (event.client_event_id) pending.delete(event.client_event_id)
        if (event.type === 'error') {
          if (event.error?.client_event_id) pending.delete(event.error.client_event_id)
          onError('A voice command was rejected. Incident monitoring remains available.')
        }
        onEvent(event)
        if (event.type === 'session.closed') { closing = true; cleanup(); onState('disconnected') }
      })
      channel.addEventListener('close', () => {
        if (!disposed) { cleanup(); onState('disconnected'); if (!closing) onError('Voice disconnected without confirmed final usage. Reconnect manually when ready.') }
      })
      peer.addEventListener('connectionstatechange', () => {
        if (!disposed && peer.connectionState === 'failed') { cleanup(); onState('disconnected'); onError('Voice connection failed. Use Reconnect to start a new session.') }
      })
      const offer = await peer.createOffer()
      await peer.setLocalDescription(offer)
      if (peer.iceGatheringState !== 'complete') await new Promise((resolve, reject) => {
        const finish = error => {
          clearTimeout(iceTimer)
          peer.removeEventListener('icegatheringstatechange', check)
          abort.signal.removeEventListener('abort', cancelled)
          if (error) reject(error)
          else resolve()
        }
        const check = () => { if (peer.iceGatheringState === 'complete') finish() }
        const cancelled = () => finish(new DOMException('Voice connection cancelled', 'AbortError'))
        const iceTimer = setTimeout(() => finish(new Error('Voice connection timed out while gathering network candidates.')), 10000)
        peer.addEventListener('icegatheringstatechange', check)
        abort.signal.addEventListener('abort', cancelled, { once: true })
        check()
      })
      if (disposed) return
      const response = await fetcher(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sdp: peer.localDescription.sdp }), signal: AbortSignal.any([abort.signal, AbortSignal.timeout(20000)]) })
      let result
      try { result = await response.json() } catch { throw new Error('Voice session endpoint is unavailable. Configure the server session URL.') }
      if (!response.ok) throw new Error(result.error || `Voice setup failed (${response.status}).`)
      if (!result.session?.id || !result.transport?.sdp) throw new Error('Invalid Live session response.')
      onEvent({ type: 'session.created.local', session: { id: result.session.id } })
      await peer.setRemoteDescription({ type: 'answer', sdp: result.transport.sdp })
      if (!ready) startedTimer = setTimeout(() => { if (!ready) { onError('GPT-Live did not confirm session startup.'); cleanup(); onState('disconnected') } }, 15000)
    } catch (error) {
      if (!disposed) { cleanup(); onState('disconnected'); onError(error.name === 'NotAllowedError' ? 'Microphone permission was denied. Visual incident alerts remain active.' : error.message) }
    }
  }

  function close() {
    if (disposed || closing) return
    microphone?.getTracks().forEach(track => { track.enabled = false })
    if (speaker) speaker.muted = true
    if (!ready || channel?.readyState !== 'open') { cleanup(); onState('disconnected'); return }
    onState('closing')
    emit('session.close')
    closing = true
    timeout = setTimeout(() => { cleanup(); onState('disconnected'); onError('Voice stopped; final session usage was not confirmed.') }, 15000)
  }

  return {
    start, close, dispose: cleanup,
    append: (kind, content, delegationId = null) => emit(`session.${kind}.append`, content, delegationId),
    muteInput(muted) { inputMuted = muted; microphone?.getTracks().forEach(track => { track.enabled = !muted }); emit(`session.input_audio.${muted ? 'mute' : 'unmute'}`) },
    muteOutput(muted) { outputMuted = muted; if (speaker) { speaker.muted = muted; if (!muted) speaker.play().catch(() => onError('Select Enable voice again if the browser blocks playback.')) } },
  }
}
