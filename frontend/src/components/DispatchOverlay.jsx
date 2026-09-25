import { useEffect, useRef } from 'react'
import { Ambulance, Check, HardHat, ShieldCheck, X } from 'lucide-react'
import gsap from 'gsap'
import { responderServices } from '../data/demoData'

const icons = { medical: Ambulance, safety: HardHat, rescue: ShieldCheck }

export default function DispatchOverlay({ dispatch, site, now, onClose }) {
  const panel = useRef(null)
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const context = gsap.context(() => {
      gsap.from(panel.current, { y: 15, opacity: 0, duration: 0.35, ease: 'power2.out', clearProps: 'all' })
      gsap.from('[data-response-team]', { x: -8, opacity: 0, stagger: 0.12, duration: 0.4, delay: 0.12, clearProps: 'all' })
    }, panel)
    return () => context.revert()
  }, [])
  const elapsed = now - Date.parse(dispatch.createdAt)
  return <section ref={panel} className="dispatch-overlay" aria-label="Simulated emergency response">
    <header><span className="dispatch-heading-icon"><ShieldCheck size={18} /></span><div><span className="eyebrow">HUMAN-APPROVED RESPONSE</span><h3>Help is being coordinated.</h3></div><button className="icon-button" onClick={onClose} aria-label="Hide dispatch visualization"><X size={14} /></button></header>
    <p className="dispatch-destination">{site?.name} <span>{dispatch.incidentId}</span></p>
    <div className="response-team-grid">{dispatch.services.map((id, index) => {
      const team = responderServices.find(item => item.id === id)
      const Icon = icons[id] || ShieldCheck
      const stage = elapsed > 6000 + index * 1000 ? 'Unit en route' : elapsed > 2000 + index * 500 ? 'Acknowledged' : 'Request prepared'
      return <div key={id} className="response-team-tile" data-response-team style={{ '--team-color': team?.color || '#83bdba' }}><Icon size={23} /><strong>{team?.unit || id}</strong><span><Check size={10} />{stage}</span><div className="response-progress"><i style={{ width: `${Math.min(100, Math.max(5, elapsed / 250))}%` }} /></div></div>
    })}</div>
    <footer><span className="simulation-pill">SIMULATION</span>No real services contacted. Routes and unit movement are illustrative.</footer>
  </section>
}
