import { useCallback, useEffect, useRef, useState } from 'react'
import { createGptLiveVoice } from '../services/gptLiveVoice'
import { currentVoiceContext, delegationContext, incidentBriefing } from '../services/voiceContext'

export function useGptLiveVoice(dashboard, selectedIncidentId) {
  const [state, setState] = useState('disconnected')
  const [error, setError] = useState('')
  const [inputMuted, setInputMuted] = useState(false)
  const [outputMuted, setOutputMuted] = useState(false)
  const [transcripts, setTranscripts] = useState({ input: '', output: '', fragments: [] })
  const [usage, setUsage] = useState(0)
  const [activity, setActivity] = useState({ input: 0, output: 0 })
  const [localAlerts, setLocalAlerts] = useState(false)
  const transport = useRef(null)
  const latest = useRef({ dashboard, selectedIncidentId })
  useEffect(() => { latest.current = { dashboard, selectedIncidentId } }, [dashboard, selectedIncidentId])
  const previousContext = useRef('')
  const seen = useRef(new Set())
  const delegations = useRef(new Set())
  const mounted = useRef(true)
  const connecting = useRef(false)
  const chimeContext = useRef(null)
  const endpoint = import.meta.env.VITE_VOICE_SESSION_URL || (import.meta.env.DEV ? '/__dev/voice/session' : '/api/voice/session')

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; transport.current?.close(); chimeContext.current?.close().catch(() => {}); window.speechSynthesis?.cancel() }
  }, [])

  const start = useCallback(async () => {
    if (connecting.current) return
    connecting.current = true
    transport.current?.dispose()
    setError(''); setUsage(0); setTranscripts({ input: '', output: '', fragments: [] }); setInputMuted(false); setOutputMuted(false)
    previousContext.current = ''
    seen.current = new Set(latest.current.dashboard.notices.map(notice => notice.noticeId))
    delegations.current.clear()
    transport.current = createGptLiveVoice({
      endpoint,
      onState(next) { if (mounted.current) { setState(next); if (next === 'disconnected') connecting.current = false } },
      onError(message) { if (mounted.current) setError(message) },
      onActivity(value) { if (mounted.current) setActivity(value) },
      onEvent(event) {
        if (!mounted.current) return
        if (event.type === 'session.input_transcript.delta' || event.type === 'session.output_transcript.delta') {
          const speaker = event.type.includes('.input_') ? 'input' : 'output'
          setTranscripts(current => ({ ...current, [speaker]: `${current[speaker]}${event.delta || ''}`.slice(-5000), fragments: [...current.fragments, { speaker, text: event.delta || '', startMs: event.start_ms, endMs: event.end_ms }].slice(-400) }))
        }
        if (event.type === 'session.usage.updated' || event.type === 'session.closed') { if (Number.isFinite(event.usage?.seconds)) setUsage(event.usage.seconds) }
        if (event.type === 'session.delegation.created' && !delegations.current.has(event.delegation?.id)) {
          const { dashboard: current, selectedIncidentId: id } = latest.current
          const context = delegationContext(event, current, id)
          if (context) { delegations.current.add(context.delegationId); transport.current?.append('commentary', context.content, context.delegationId) }
        }
      },
    })
    await transport.current.start()
  }, [endpoint])

  useEffect(() => {
    if (state !== 'connected') return
    const context = currentVoiceContext(dashboard, selectedIncidentId)
    if (previousContext.current === context) return
    const initial = previousContext.current === ''
    if (transport.current?.append('thinking', context)) previousContext.current = context
    if (initial) transport.current?.append('instructions', 'Briefly introduce yourself as Aman, the control-room voice assistant. Say that voice monitoring is now enabled, briefly state the current active-incident count from the application context, and invite questions about the selected incident. Then listen. Do not claim any service has been contacted.')
  }, [state, dashboard, selectedIncidentId])

  useEffect(() => {
    const fresh = dashboard.notices.filter(notice => !seen.current.has(notice.noticeId))
    if (!fresh.length) return
    fresh.forEach(notice => seen.current.add(notice.noticeId))
    if (seen.current.size > 1000) seen.current = new Set(dashboard.notices.map(notice => notice.noticeId))
    const briefing = fresh.slice(0, 2).map(notice => incidentBriefing(notice, dashboard)).join(' ').slice(0, 1000)
    if (state === 'connected') {
      if (fresh.some(notice => notice.severity === 'critical')) transport.current?.append('instructions', 'Prioritize the next critical incident briefing. Keep it brief, do not overlap a lengthy explanation, and allow the operator to interrupt.')
      transport.current?.append('commentary', briefing)
    }
    if (localAlerts && !outputMuted) {
      const audio = chimeContext.current
      if (audio && audio.state === 'running') {
        const oscillator = audio.createOscillator()
        const gain = audio.createGain()
        oscillator.connect(gain); gain.connect(audio.destination)
        oscillator.frequency.setValueAtTime(660, audio.currentTime)
        oscillator.frequency.setValueAtTime(880, audio.currentTime + 0.12)
        gain.gain.setValueAtTime(0.06, audio.currentTime)
        gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.35)
        oscillator.start(); oscillator.stop(audio.currentTime + 0.36)
      }
      if (state !== 'connected' && 'speechSynthesis' in window) {
        window.speechSynthesis.cancel()
        const speech = new SpeechSynthesisUtterance(briefing)
        speech.rate = 1
        window.speechSynthesis.speak(speech)
      }
    }
  }, [dashboard, state, localAlerts, outputMuted])

  async function toggleLocalAlerts() {
    if (!localAlerts) {
      try { chimeContext.current ||= new AudioContext(); await chimeContext.current.resume() } catch { setError('Local audio is unavailable in this browser.') }
    } else window.speechSynthesis?.cancel()
    setLocalAlerts(!localAlerts)
  }

  return {
    state, error, inputMuted, outputMuted, transcripts, usage, activity, localAlerts, start,
    clearError: () => setError(''), stop: () => transport.current?.close(), toggleLocalAlerts,
    toggleInput: () => { transport.current?.muteInput(!inputMuted); setInputMuted(!inputMuted) },
    toggleOutput: () => { transport.current?.muteOutput(!outputMuted); if (!outputMuted) window.speechSynthesis?.cancel(); setOutputMuted(!outputMuted) },
  }
}
