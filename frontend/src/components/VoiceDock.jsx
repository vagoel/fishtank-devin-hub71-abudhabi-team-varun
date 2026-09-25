import { useState } from 'react'
import { ChevronDown, ChevronUp, MessageSquareText, Mic, MicOff, Radio, Square, Volume2, VolumeX, X } from 'lucide-react'

export default function VoiceDock({ voice }) {
  const [expanded, setExpanded] = useState(false)
  const connected = voice.state === 'connected'
  const moving = connected && (voice.activity.input > 0.03 || voice.activity.output > 0.03)
  return <section className={`voice-dock ${connected ? 'connected' : ''}`} aria-label="GPT-Live-1 voice assistant">
    {expanded && <div className="voice-transcript"><div className="popover-heading"><h3><MessageSquareText size={16} />Live transcript</h3><button className="icon-button" onClick={() => setExpanded(false)} aria-label="Collapse transcript"><ChevronDown size={17} /></button></div><div className="transcript-columns"><div><span className="eyebrow">OPERATOR {voice.activity.input > 0.03 ? '· SPEAKING' : ''}</span><p>{voice.transcripts.input || 'Your words will appear here when you speak.'}</p></div><div><span className="eyebrow">AMAN {voice.activity.output > 0.03 ? '· SPEAKING' : ''}</span><p>{voice.transcripts.output || 'Enable GPT-Live-1 to receive spoken briefings and ask questions.'}</p></div></div><div className="voice-footnote"><label><input type="checkbox" checked={voice.localAlerts} onChange={voice.toggleLocalAlerts} />Local alert cue + browser spoken fallback</label><span>Fallback is not GPT-Live. Voice sessions are billed while connected.</span></div></div>}
    {voice.error && <div className="voice-error" role="alert"><span>{voice.error}</span><button className="icon-button" onClick={voice.clearError} aria-label="Dismiss voice error"><X size={14} /></button></div>}
    <div className="voice-dock-main">
      <div className={`voice-emblem ${moving ? 'speaking' : ''}`}><Radio size={22} /></div>
      <div className="voice-description"><strong>Aman <span>VOICE ASSISTANT</span></strong><p>{connected ? 'Listening for you. Watching your sites.' : voice.state === 'connecting' ? 'Connecting to GPT-Live-1…' : voice.state === 'closing' ? 'Ending voice session…' : 'Your eyes on every site. Your voice in the room.'}</p></div>
      <div className={`voice-wave ${moving ? 'active' : ''}`} aria-hidden="true">{Array.from({ length: 29 }, (_, index) => <i key={index} style={{ '--bar': `${4 + Math.sin(index * 1.7) ** 2 * (moving ? 27 : 9)}px`, '--delay': `${index * 0.045}s` }} />)}</div>
      <div className="voice-model"><span className={`status-dot ${connected ? 'healthy' : 'offline'}`} />GPT-Live-1{connected && <small>{Math.floor(voice.usage / 60)}:{String(Math.floor(voice.usage % 60)).padStart(2, '0')}</small>}</div>
      {connected ? <div className="voice-buttons"><button className={`icon-button ${voice.inputMuted ? 'muted' : ''}`} onClick={voice.toggleInput} aria-label={voice.inputMuted ? 'Unmute microphone' : 'Mute microphone'}>{voice.inputMuted ? <MicOff size={17} /> : <Mic size={17} />}</button><button className="icon-button" onClick={voice.toggleOutput} aria-label={voice.outputMuted ? 'Enable speaker playback' : 'Mute speaker'}>{voice.outputMuted ? <VolumeX size={17} /> : <Volume2 size={17} />}</button><button className="button secondary" onClick={voice.stop}><Square size={12} />End voice</button></div> : <button className="button voice-enable" onClick={voice.start} disabled={voice.state !== 'disconnected'}><Mic size={16} />{voice.state === 'connecting' ? 'Connecting…' : voice.state === 'closing' ? 'Ending…' : 'Enable voice'}</button>}
      <button className="icon-button transcript-toggle" onClick={() => setExpanded(!expanded)} aria-label={expanded ? 'Hide voice transcript' : 'Show voice transcript'} aria-expanded={expanded}>{expanded ? <ChevronDown size={17} /> : <ChevronUp size={17} />}</button>
    </div>
  </section>
}
