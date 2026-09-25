import { ArrowLeft, ArrowUpRight, Bell, ChevronRight, CircleCheck, Clock3, HardHat, Radio, ShieldCheck, Thermometer, TriangleAlert, UserRound, WifiOff } from 'lucide-react'
import { incidentTypes, responderServices } from '../data/demoData'
import { isActive, localTime, statusLabels, timeAgo } from '../domain/incidents'
import { EmptyState, Sparkline, StatusBadge } from './Primitives'

const typeIcons = { fall: TriangleAlert, temperature: Thermometer, distress: Bell }

function IncidentCard({ incident, sites, workers, now, onSelect }) {
  const site = sites.find(item => item.id === incident.siteId)
  const worker = workers.find(item => item.id === incident.workerId)
  const Icon = typeIcons[incident.type] || Bell
  return <button className={`incident-card ${incident.severity} ${isActive(incident) ? '' : 'closed'}`} onClick={() => onSelect(incident)}>
    <div className="incident-card-top"><span className={`incident-icon ${incident.severity}`}><Icon size={17} /></span><span className="eyebrow">{isActive(incident) ? incident.severity : statusLabels[incident.status]}</span><time>{timeAgo(incident.createdAt, now)}</time></div>
    <h3>{incidentTypes[incident.type]?.label || incident.type}</h3><p>{site?.name || incident.siteId}</p>
    <div className="incident-card-bottom"><span><UserRound size={12} />{worker?.name || incident.workerId}</span><ChevronRight size={15} /></div>
  </button>
}

export function WorkerDetail({ worker, incidents, onIncident }) {
  if (!worker) return null
  return <div className="worker-detail">
    <div className="worker-identity"><span className="worker-avatar">{worker.name.split(' ').map(part => part[0]).join('')}</span><span><strong>{worker.name}</strong><small>{worker.role} · {worker.id}</small></span><StatusBadge status={worker.connectivity === 'online' ? 'healthy' : 'offline'}>{worker.connectivity}</StatusBadge></div>
    <div className="worker-readings"><span><small>Band surface</small><strong>{worker.temperature == null ? '—' : `${worker.temperature}°`}<small> C</small></strong></span><span><small>Wearable</small><strong className="device-id">{worker.bandId}</strong></span></div>
    {!!worker.readings?.length && <Sparkline values={worker.readings} tone={incidents.some(incident => incident.type === 'temperature' && isActive(incident)) ? 'warning' : 'healthy'} />}
    <div className="chart-caption"><span>Recent readings</span><span>Not core body temperature</span></div>
    <p className="small-note">Assigned site location · no individual GPS tracking</p>
    {!!incidents.length && <div className="worker-history">{incidents.map(incident => <button key={incident.id} onClick={() => onIncident(incident)}>{incident.id}<span>{statusLabels[incident.status]}</span><ChevronRight size={13} /></button>)}</div>}
  </div>
}

export default function IncidentPanel({ dashboard, selectedSite, selectedIncident, selectedWorkerId, onSelectIncident, onBack, onSelectWorker, onReview, onDismiss, onDispatch, onResolve, filter, onFilter }) {
  const { incidents, workers, sites, dispatches, now, busy, mode } = dashboard
  const worker = workers.find(item => item.id === (selectedIncident?.workerId || selectedWorkerId))
  const site = selectedSite || sites.find(item => item.id === selectedIncident?.siteId)
  const active = incidents.filter(isActive)
  const list = incidents.filter(incident => (!selectedSite || incident.siteId === selectedSite.id) && (filter === 'history' ? !isActive(incident) : isActive(incident) && (filter === 'all' || incident.severity === filter)))
  const dispatch = dispatches.find(item => item.incidentId === selectedIncident?.id)
  if (selectedIncident) return <aside className="incident-panel detail-panel">
    <div className="panel-heading"><button className="text-button" onClick={onBack}><ArrowLeft size={15} />Incident inbox</button><span className="mono subtle">{selectedIncident.id}</span></div>
    <div className="panel-scroll">
      <div className="detail-heading"><StatusBadge status={selectedIncident.severity}>{selectedIncident.severity} incident</StatusBadge><h2>{incidentTypes[selectedIncident.type]?.label || selectedIncident.type}</h2><p><MapSite />{site?.name || selectedIncident.siteId}</p><span className="detail-time"><Clock3 size={12} />{localTime(selectedIncident.createdAt)} GST · {statusLabels[selectedIncident.status]}</span></div>
      <div className={`evidence-block ${selectedIncident.severity}`}><span className="eyebrow">REPORTED EVIDENCE</span><strong>{selectedIncident.evidence?.value || 'Pending'}</strong><span>{selectedIncident.evidence?.label || 'Sensor evidence pending'}</span><small>{selectedIncident.evidence?.source}</small></div>
      <WorkerDetail worker={worker} incidents={[]} onIncident={onSelectIncident} />
      <section className="analysis-card"><h3><span className="analysis-mark"><Radio size={14} /></span>Agent assessment<small>{mode === 'demo' ? 'DEMO' : 'DEVIN'}</small></h3>{selectedIncident.analysis?.status === 'ready' ? <><p>{selectedIncident.analysis.summary}</p><div className="recommendation"><ArrowUpRight size={16} /><p>{selectedIncident.analysis.recommendation}</p></div></> : <p>{selectedIncident.analysis?.status === 'failed' ? 'Analysis is unavailable. Sensor evidence and human review remain available.' : 'Analysis is pending. You can review the reported evidence now.'}</p>}</section>
      {dispatch && <section className="dispatch-card"><h3><ShieldCheck size={15} />Coordinated response <small>SIMULATED</small></h3>{dispatch.services.map((service, i) => { const team = responderServices.find(item => item.id === service); const elapsed = now - Date.parse(dispatch.createdAt); const stage = elapsed > 6000 + i * 1000 ? 'Unit en route' : elapsed > 2000 + i * 500 ? 'Acknowledged' : 'Request prepared'; return <div className="dispatch-team" key={service}><span className="dispatch-pulse" style={{ '--team-color': team?.color }} /><span><strong>{team?.name || service}</strong><small>{stage} · demo</small></span><CircleCheck size={15} /></div> })}<p className="small-note">Illustrative route and response. No real services contacted.</p></section>}
      <section className="timeline"><h3>Incident timeline</h3>{selectedIncident.timeline.map((entry, index) => <div key={`${entry.at}-${index}`}><span className="timeline-node" /><time>{localTime(entry.at)}</time><p>{entry.label}</p></div>)}</section>
    </div>
    <div className="review-actions">
      {['new', 'reviewing'].includes(selectedIncident.status) ? <><p><ShieldCheck size={13} />Your decision. No automatic dispatch.</p>{selectedIncident.status === 'new' && <button className="button secondary wide" disabled={busy} onClick={() => onReview(selectedIncident)}>Acknowledge & review</button>}<button className="button primary wide" disabled={busy || mode !== 'demo'} onClick={() => onDispatch(selectedIncident)}>Coordinate response <ArrowUpRight size={15} /></button><button className="text-button quiet" disabled={busy} onClick={() => onDismiss(selectedIncident)}>Mark as false alarm</button>{mode !== 'demo' && <small>Dispatch integration is not connected in API mode.</small>}</> : selectedIncident.status === 'dispatched' ? <button className="button primary wide" disabled={busy} onClick={() => onResolve(selectedIncident)}><CircleCheck size={16} />Mark incident resolved</button> : <StatusBadge status={selectedIncident.status}>{statusLabels[selectedIncident.status]}</StatusBadge>}
    </div>
  </aside>

  return <aside className="incident-panel">
    <div className="panel-heading"><h2>{selectedSite ? 'Site overview' : 'Incident inbox'}<span className="count-badge">{selectedSite ? incidents.filter(item => item.siteId === selectedSite.id && isActive(item)).length : active.length}</span></h2>{selectedSite ? <button className="icon-button" onClick={onBack} aria-label="Back to all incidents"><ArrowLeft size={16} /></button> : <Bell size={16} className="subtle" />}</div>
    {selectedSite && <div className="selected-site-header"><span className="eyebrow">{selectedSite.id} / {selectedSite.district}</span><h3>{selectedSite.name}</h3><div><span><HardHat size={14} />{workers.filter(item => item.siteId === selectedSite.id).length} workers</span><span><Radio size={13} />{selectedSite.connectivity}</span></div></div>}
    <div className="feed-tabs" role="group" aria-label="Filter incidents">{[['all', 'Active'], ['critical', 'Critical'], ['warning', 'Warnings'], ['history', 'History']].map(([value, label]) => <button aria-pressed={filter === value} className={filter === value ? 'active' : ''} key={value} onClick={() => onFilter(value)}>{label}</button>)}</div>
    <div className="panel-scroll">
      {worker && <WorkerDetail worker={worker} incidents={incidents.filter(item => item.workerId === worker.id)} onIncident={onSelectIncident} />}
      <div className="incident-list">{list.map(incident => <IncidentCard key={incident.id} incident={incident} workers={workers} sites={sites} now={now} onSelect={onSelectIncident} />)}{!list.length && <EmptyState icon={CircleCheck} title={filter === 'history' ? 'No closed incidents' : 'Nothing needs review'}>{filter === 'history' ? 'Resolved and dismissed incidents will appear here.' : 'New events will appear here as they arrive.'}</EmptyState>}</div>
      {selectedSite && <section className="site-workers"><h3>On-site roster <span>{workers.filter(item => item.siteId === selectedSite.id).length}</span></h3>{workers.filter(item => item.siteId === selectedSite.id).map(item => <button key={item.id} className={item.id === selectedWorkerId ? 'selected' : ''} onClick={() => onSelectWorker(item.id)}><span className="mini-avatar">{item.name.split(' ').map(part => part[0]).join('')}</span><span><strong>{item.name}</strong><small>{item.role} · {item.id}</small></span>{item.connectivity === 'offline' ? <WifiOff size={14} /> : <ChevronRight size={14} />}</button>)}</section>}
    </div>
    <div className="inbox-footer"><ShieldCheck size={14} /><p>Intelligence assists.<br /><strong>People make the call.</strong></p><span className="footer-grid" /></div>
  </aside>
}

function MapSite() { return <HardHat size={13} /> }
