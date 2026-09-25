import { useEffect, useRef, useState } from 'react'
import { Ambulance, HardHat, ShieldCheck, X, AlertTriangle, Check } from 'lucide-react'
import { incidentTypes, responderServices } from '../data/demoData'

const icons = { medical: Ambulance, safety: HardHat, rescue: ShieldCheck }

export default function ReviewDialog({ kind, incident, site, busy, error, onClose, onConfirm }) {
  const dialog = useRef(null)
  const [reason, setReason] = useState('')
  const [services, setServices] = useState(incidentTypes[incident.type]?.services || ['safety'])
  useEffect(() => {
    const element = dialog.current
    const returnFocus = document.activeElement
    element.showModal()
    return () => {
      element.close()
      if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus()
    }
  }, [])
  function submit(event) {
    event.preventDefault()
    onConfirm(kind === 'dismiss' ? { reason } : services)
  }
  return <dialog className="review-dialog" ref={dialog} onCancel={event => { event.preventDefault(); if (!busy) onClose() }} onClick={event => { if (event.target === dialog.current && !busy) onClose() }} aria-labelledby="review-title">
    <form onSubmit={submit}>
      <div className="dialog-top"><span className="dialog-symbol"><ShieldCheck size={24} /></span><button type="button" className="icon-button" onClick={onClose} aria-label="Close confirmation" disabled={busy}><X size={18} /></button></div>
      <p className="eyebrow">HUMAN APPROVAL REQUIRED</p>
      <h2 id="review-title">{kind === 'dismiss' ? 'Mark as a false alarm?' : 'Coordinate a response.'}</h2>
      <p className="dialog-description">{kind === 'dismiss' ? 'Record why this event does not need a response. The incident stays in the activity history.' : 'Select the teams required for this incident. Nothing is dispatched without your confirmation.'}</p>
      <div className="dialog-incident"><span>{incident.id}</span><strong>{incidentTypes[incident.type]?.short || incident.type}</strong><small>{site?.name} · {incident.workerId}</small></div>
      {kind === 'dismiss' ? <label className="field-label">Reason for dismissal<textarea autoFocus required minLength={5} maxLength={500} value={reason} onChange={event => setReason(event.target.value)} placeholder="For example, the supervisor verified the band was removed during a break." /></label> : <div className="service-options">{responderServices.map(service => {
        const Icon = icons[service.id]
        return <label key={service.id} className={`service-option ${services.includes(service.id) ? 'checked' : ''}`}><input type="checkbox" checked={services.includes(service.id)} onChange={() => setServices(current => current.includes(service.id) ? current.filter(id => id !== service.id) : [...current, service.id])} /><Icon size={22} /><span><strong>{service.name}</strong><small>{service.description}</small></span><span className="custom-check">{services.includes(service.id) && <Check size={13} />}</span></label>
      })}</div>}
      {kind !== 'dismiss' && <div className="simulation-disclosure"><AlertTriangle size={16} /><span>Demonstration only. No emergency service will be contacted. Suggested teams are not an official response protocol.</span></div>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="dialog-actions"><button className="button secondary" type="button" onClick={onClose} disabled={busy}>Go back</button><button className="button primary" disabled={busy || (kind === 'dispatch' && !services.length) || (kind === 'dismiss' && reason.trim().length < 5)}>{busy ? 'Saving…' : kind === 'dismiss' ? 'Confirm false alarm' : 'Confirm simulated dispatch'}</button></div>
    </form>
  </dialog>
}
