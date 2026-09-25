import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Activity, ArrowDownLeft, ArrowUpRight, Bell, Building2, ChevronDown, ChevronRight, CircleHelp, Crosshair, Globe2, HardHat, LayoutDashboard, Maximize2, Radio, RefreshCw, Search, ShieldCheck, TriangleAlert, Users, Wifi, X } from 'lucide-react'
import gsap from 'gsap'
import { useDashboard } from './hooks/useDashboard'
import { useGptLiveVoice } from './hooks/useGptLiveVoice'
import { isActive, localTime, needsReview, siteStatus, timeAgo } from './domain/incidents'
import { dataMode } from './services/dashboardApi'
import { incidentTypes } from './data/demoData'
import IncidentPanel from './components/IncidentPanel'
import ReviewDialog from './components/ReviewDialog'
import DemoControls from './components/DemoControls'
import DispatchOverlay from './components/DispatchOverlay'
import VoiceDock from './components/VoiceDock'
import { MapBoundary } from './components/Primitives'
import './App.css'

const AbuDhabiMap = lazy(() => import('./map/AbuDhabiMap'))

function ControlRoom() {
  const dashboard = useDashboard()
  const [selectedSiteId, setSelectedSiteId] = useState(null)
  const [selectedIncidentId, setSelectedIncidentId] = useState(null)
  const [selectedWorkerId, setSelectedWorkerId] = useState(null)
  const [filter, setFilter] = useState('all')
  const [view, setView] = useState(null)
  const [explorer, setExplorer] = useState(null)
  const [search, setSearch] = useState('')
  const [dialog, setDialog] = useState(null)
  const [hiddenDispatchId, setHiddenDispatchId] = useState(null)
  const [help, setHelp] = useState(false)
  const [uiError, setUiError] = useState('')
  const page = useRef(null)
  const voice = useGptLiveVoice(dashboard, selectedIncidentId)
  const { sites, workers, incidents, dispatches, now } = dashboard
  const live = dashboard.mode === 'api'
  const reportedDevices = new Set(workers.map(worker => worker.bandId)).size
  const metricCount = count => live && !dashboard.lastUpdated ? '—' : String(count).padStart(2, '0')
  const selectedSite = sites.find(site => site.id === selectedSiteId)
  const selectedIncident = incidents.find(incident => incident.id === selectedIncidentId)
  const active = incidents.filter(isActive)
  const critical = active.filter(incident => incident.severity === 'critical')
  const connected = workers.filter(worker => worker.connectivity === 'online' && now - Date.parse(worker.lastSeen) <= 30000).length
  const latestNotice = dashboard.notices.find(notice => !notice.id || incidents.some(incident => incident.id === notice.id && isActive(incident)))
  const selectedDispatch = selectedIncident && isActive(selectedIncident) ? dispatches.find(dispatch => dispatch.simulated && dispatch.incidentId === selectedIncident.id && dispatch.id !== hiddenDispatchId) : null

  useEffect(() => {
    const media = gsap.matchMedia()
    media.add('(prefers-reduced-motion: no-preference)', () => {
      const context = gsap.context(() => gsap.from('[data-enter]', { y: 12, opacity: 0, duration: 0.65, stagger: 0.055, ease: 'power2.out', clearProps: 'all' }), page)
      return () => context.revert()
    })
    return () => media.revert()
  }, [])

  function selectSite(id) {
    const site = sites.find(item => item.id === id)
    if (!site) return
    setSelectedSiteId(id); setSelectedIncidentId(null); setSelectedWorkerId(null); setFilter('all'); setExplorer(null)
    setView({ coordinates: site.coordinates, zoom: 15.1, at: Date.now() })
  }
  function selectIncident(incident) {
    const site = sites.find(item => item.id === incident.siteId)
    setSelectedIncidentId(incident.id); setSelectedSiteId(incident.siteId); setSelectedWorkerId(null)
    if (site) setView({ coordinates: site.coordinates, zoom: 15.4, at: Date.now() })
  }
  function resetSelection() { setSelectedSiteId(null); setSelectedIncidentId(null); setSelectedWorkerId(null) }
  async function confirm(value) {
    const success = dialog.kind === 'dismiss' ? await dashboard.review(dialog.incident, 'dismissed', value) : await dashboard.dispatch(dialog.incident, value)
    if (success) { setDialog(null); setHiddenDispatchId(null) }
  }
  const visibleSites = sites.filter(site => `${site.name} ${site.district} ${site.id}`.toLowerCase().includes(search.toLowerCase()))
  const visibleWorkers = workers.filter(worker => `${worker.name} ${worker.id} ${worker.bandId}`.toLowerCase().includes(search.toLowerCase()))

  return <div ref={page} className="control-room">
    <a className="skip-link" href="#incident-workspace">Skip to incident review</a>
    <header className="app-header">
      <a href="#" className="brand" onClick={event => { event.preventDefault(); resetSelection(); setView({ coordinates: [54.389, 24.474], zoom: 12.65, at: Date.now() }) }} aria-label="Aman control room home"><span className="brand-symbol"><ShieldCheck size={23} strokeWidth={1.5} /></span><span>AMAN<small>WORKFORCE INTELLIGENCE</small></span></a>
      <div className="header-location"><Globe2 size={15} /><span>United Arab Emirates</span><ChevronRight size={12} /><strong>Abu Dhabi</strong></div>
      <div className="header-right"><span className="demo-tag">{dashboard.mode === 'demo' ? 'DEMO ENVIRONMENT' : 'API MODE · READ-ONLY'}</span><div className="operator-avatar" title="Demo operator">CR</div><div className="operator-label"><strong>Control room</strong><small>Operations team</small></div></div>
    </header>
    <div className="app-body">
      <nav className="nav-rail" aria-label="Dashboard navigation">
        <button className={!explorer ? 'active' : ''} aria-label="City overview" title="City overview" onClick={() => { setExplorer(null); resetSelection() }}><LayoutDashboard size={20} /></button>
        <button className={explorer === 'sites' ? 'active' : ''} aria-label="Browse construction sites" title="Sites" onClick={() => { setExplorer(explorer === 'sites' ? null : 'sites'); setSearch('') }}><Building2 size={20} /></button>
        <button className={explorer === 'workers' ? 'active' : ''} aria-label="Browse workers" title="Workers" onClick={() => { setExplorer(explorer === 'workers' ? null : 'workers'); setSearch('') }}><Users size={20} /></button>
        <button aria-label="Show critical incidents" title="Critical incidents" onClick={() => { resetSelection(); setFilter('critical'); setExplorer(null) }}><Bell size={20} />{critical.length > 0 && <i className="nav-notification" />}</button>
        <span className="nav-spacer" />
        <span className="nav-status" title={dashboard.stale ? 'Data is stale' : 'Polling active'}><i className={`status-dot ${dashboard.stale ? 'offline' : 'healthy'}`} /></span>
        <button aria-label="About this demonstration" title="About this demo" onClick={() => setHelp(!help)}><CircleHelp size={19} /></button>
      </nav>
      <main className="main-content">
        <div className="page-heading" data-enter><div><div className="eyebrow"><span className="tiny-rule" />CITY OPERATIONS / 01</div><h1>Abu Dhabi<span className="heading-dot">.</span></h1><p>A connected view of your people, places, and priorities.</p></div><div className="heading-actions"><span className={`connection-chip ${dashboard.stale ? 'stale' : ''}`}><span className={`status-dot ${dashboard.stale ? 'offline' : 'healthy'}`} />{dashboard.paused ? 'Polling paused' : dashboard.connection === 'connecting' ? 'Connecting' : dashboard.stale ? 'Data stale' : live ? 'Feed connected' : 'Monitoring active'}</span><div className="clock"><strong>{localTime(now)}</strong><span>GULF STANDARD TIME · UTC+4</span></div><DemoControls dashboard={dashboard} siteId={selectedSiteId} /></div></div>
        <section className="metrics-grid" aria-label="Operations summary" data-enter>
          <div className="metric-card"><span className="metric-icon"><HardHat size={18} /></span><div className="metric-body"><span>{live ? 'Reported wearers' : 'Total workforce'}</span><strong>{metricCount(workers.length)}<small>workers</small></strong><p>{live ? 'Latest person reported per device' : `Across ${sites.length} monitored sites`}</p></div><div className="metric-decoration worker-stacks" aria-hidden="true">{[0, 1, 2, 3, 4].map(i => <i key={i} />)}</div></div>
          <div className="metric-card"><span className="metric-icon mint"><Radio size={18} /></span><div className="metric-body"><span>{live ? 'Devices in incident feed' : 'Connected wearables'}</span><strong>{live ? metricCount(reportedDevices) : dashboard.stale ? '—' : String(connected).padStart(2, '0')}<small>{live ? 'reported' : `/ ${workers.length}`}</small></strong><p><i className={`status-dot ${live || dashboard.stale ? 'offline' : 'healthy'}`} />{live ? 'Connectivity is not provided' : dashboard.stale ? 'Awaiting fresh telemetry' : `${workers.length - connected} bands offline`}</p></div><div className="connection-bars" aria-hidden="true">{sites.map(site => <i key={site.id} className={!dashboard.stale && site.connectivity === 'online' ? 'online' : ''} />)}</div></div>
          <button className="metric-card metric-button" onClick={() => { resetSelection(); setFilter('all') }}><span className="metric-icon amber"><Activity size={18} /></span><div className="metric-body"><span>Active incidents</span><strong>{metricCount(active.length)}<small>open</small></strong><p>{active.filter(needsReview).length} awaiting human review</p></div><ArrowUpRight size={17} className="metric-arrow" /></button>
          <button className="metric-card metric-button critical-metric" onClick={() => { resetSelection(); setFilter('critical') }}><span className="metric-icon coral"><TriangleAlert size={18} /></span><div className="metric-body"><span>Critical priority</span><strong>{metricCount(critical.length)}<small>{critical.length === 1 ? 'incident' : 'incidents'}</small></strong><p><i className={`status-dot ${critical.length ? 'critical' : live && dashboard.stale ? 'offline' : 'healthy'}`} />{live && !dashboard.lastUpdated ? 'Awaiting source reports' : live && dashboard.stale ? 'Last known incident count' : critical.length ? 'Requires your attention' : 'No critical alerts'}</p></div><ArrowUpRight size={17} className="metric-arrow" /></button>
        </section>
        {(dashboard.error || uiError) && <div className="error-banner" role="alert"><TriangleAlert size={16} /><span>{uiError || dashboard.error}</span><button onClick={() => { dashboard.clearError(); setUiError(''); dashboard.refresh() }}><RefreshCw size={13} />Retry</button></div>}
        {live && <div className="mode-notice"><span><Radio size={13} />Read-only incident feed · statuses owned by the source</span><span>Demo fallback: <code>VITE_DATA_MODE=demo</code> + restart, or <code>npm run dev:demo --workspace frontend</code></span></div>}
        <div className="operations-grid" id="incident-workspace" data-enter>
          <section className="map-panel" aria-label="Abu Dhabi map and site explorer">
            <div className="map-panel-header"><h2><Crosshair size={16} />{live ? 'Incident locations' : 'Live site map'}</h2><span className="map-header-divider" /><span className="map-panel-subtitle">{live ? 'REPORTED COORDINATES' : 'ABU DHABI REGION'}</span><div className="map-panel-actions"><button className={`text-button ${explorer ? 'selected' : ''}`} onClick={() => { setExplorer(explorer ? null : 'sites'); setSearch('') }}><Building2 size={14} />{sites.length} {live ? 'locations' : 'sites'}<ChevronDown size={12} /></button><button className="icon-button" onClick={() => { const element = document.querySelector('.map-panel'); if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); else element?.requestFullscreen?.().catch(() => setUiError('Fullscreen is unavailable in this browser.')) }} aria-label="Toggle map fullscreen"><Maximize2 size={15} /></button></div></div>
            <div className="map-content"><MapBoundary><Suspense fallback={<div className="map-loading"><span className="loading-orbit" />Loading the city view</div>}><AbuDhabiMap mode={dashboard.mode} sites={sites} incidents={incidents} dispatches={dispatches} selectedSiteId={selectedSiteId} stale={dashboard.stale} onSelectSite={selectSite} view={view} /></Suspense></MapBoundary>
              {explorer && <div className="site-explorer"><div className="popover-heading"><h3>{explorer === 'workers' ? 'Your workforce' : 'Monitored sites'}</h3><button className="icon-button" onClick={() => setExplorer(null)} aria-label="Close site explorer"><X size={15} /></button></div><label className="search-input"><Search size={15} /><input autoFocus value={search} onChange={event => setSearch(event.target.value)} placeholder={explorer === 'workers' ? 'Name, worker or band ID…' : 'Search site or district…'} aria-label={explorer === 'workers' ? 'Search workers' : 'Search construction sites'} /></label><div className="explorer-results">{explorer === 'sites' ? visibleSites.map(site => <button key={site.id} onClick={() => selectSite(site.id)}><span className={`site-monogram ${siteStatus(site, incidents, dashboard.stale)}`}>{site.code}</span><span><strong>{site.name}</strong><small>{site.district} · {site.id}</small></span><i className={`status-dot ${siteStatus(site, incidents, dashboard.stale)}`} /></button>) : visibleWorkers.map(worker => <button key={worker.id} onClick={() => { selectSite(worker.siteId); setSelectedWorkerId(worker.id) }}><span className="mini-avatar">{worker.name.split(' ').map(part => part[0]).join('')}</span><span><strong>{worker.name}</strong><small>{worker.id} · {worker.bandId}</small></span><ChevronRight size={13} /></button>)}{!(explorer === 'sites' ? visibleSites : visibleWorkers).length && <p className="empty-search">No matching records.</p>}</div><div className="explorer-footer">{dashboard.mode === 'demo' ? 'Fictional roster for this demonstration' : 'Source reports · demo assignments are labeled'}</div></div>}
              {latestNotice && <div className={`incident-toast ${latestNotice.severity || 'warning'}`} role="status"><span className="toast-icon"><Bell size={18} /></span><button className="toast-message" onClick={() => { if (latestNotice.id) selectIncident(incidents.find(item => item.id === latestNotice.id) || latestNotice); else { resetSelection(); setFilter('all') } }}><small>{latestNotice.severity === 'critical' ? 'CRITICAL EVENT RECEIVED' : 'MONITORING UPDATE'}</small><strong>{latestNotice.summary || incidentTypes[latestNotice.type]?.label}</strong><span>{sites.find(site => site.id === latestNotice.siteId)?.name || 'Open incident inbox'}<ArrowUpRight size={12} /></span></button><button className="icon-button" onClick={() => dashboard.dismissNotice(latestNotice.noticeId)} aria-label="Dismiss event notification"><X size={14} /></button></div>}
              {selectedDispatch && <DispatchOverlay key={selectedDispatch.id} dispatch={selectedDispatch} site={selectedSite} now={now} onClose={() => setHiddenDispatchId(selectedDispatch.id)} />}
              {!explorer && <button className="site-summary-card" onClick={() => { setExplorer('sites'); setSearch('') }}><span className="eyebrow">{live ? 'INCIDENT FOOTPRINT' : 'MONITORING FOOTPRINT'}</span><strong>{sites.length}<span>{live ? 'reported locations' : 'construction sites'}</span></strong><div className="site-health-track">{sites.map(site => <span key={site.id} className={siteStatus(site, incidents, dashboard.stale)} />)}</div><span className="site-summary-link">Explore locations <ArrowUpRight size={13} /></span></button>}
            </div>
            <div className="map-panel-footer"><span><Wifi size={12} />{dashboard.mode === 'demo' ? 'SIMULATED TELEMETRY' : 'INCIDENT API'}</span><span>Last sync {dashboard.lastUpdated ? timeAgo(dashboard.lastUpdated, now) : 'pending'}<i />{live ? 'Source-owned statuses' : 'Human approval enabled'}</span></div>
          </section>
          <IncidentPanel dashboard={dashboard} selectedSite={selectedSite} selectedIncident={selectedIncident} selectedWorkerId={selectedWorkerId} onSelectIncident={selectIncident} onBack={resetSelection} onSelectWorker={setSelectedWorkerId} onReview={incident => dashboard.review(incident, 'reviewing')} onDismiss={incident => { dashboard.clearError(); setDialog({ kind: 'dismiss', incident }) }} onDispatch={incident => { dashboard.clearError(); setDialog({ kind: 'dispatch', incident }) }} onResolve={incident => dashboard.review(incident, 'resolved')} filter={filter} onFilter={setFilter} />
        </div>
        <VoiceDock voice={voice} />
        <footer className="app-footer"><span><ShieldCheck size={12} />Built around people. Powered by awareness.</span><span>ABU DHABI CONTROL ROOM <i />{dashboard.mode === 'demo' ? 'DEMONSTRATION DATA' : 'API DATA'}</span></footer>
      </main>
    </div>
    {dialog && <ReviewDialog key={`${dialog.kind}-${dialog.incident.id}`} {...dialog} site={sites.find(site => site.id === dialog.incident.siteId)} busy={dashboard.busy} error={dashboard.error} onClose={() => setDialog(null)} onConfirm={confirm} />}
    {help && <div className="help-card"><button className="icon-button" onClick={() => setHelp(false)} aria-label="Close demo information"><X size={17} /></button><ShieldCheck size={24} /><h2>People make the call.</h2><p>Aman brings wearable events, worker information, and agent analysis into one control room.</p><p>{live ? 'Live mode reads heatguard.incident.v1 reports. Source-owned statuses are read-only; no vitals, heartbeat, or Devin assessment is inferred. Missing identities or coordinates use clearly labeled demo assignments.' : 'This demonstration uses fictional workers and sites, illustrative 3D landmarks, and simulated dispatch. Band surface readings are not core body temperature. No emergency service is contacted.'}</p><p>{live ? 'For the standalone demonstration, set VITE_DATA_MODE=demo and restart, or run npm run dev:demo --workspace frontend.' : 'Use Demo scenarios to inject events. Configure a server-only OpenAI key to enable GPT-Live-1.'}</p><button className="text-button" onClick={() => { setHelp(false); setExplorer('sites') }}>Explore monitored sites <ArrowDownLeft size={15} /></button></div>}
  </div>
}

export default function App() {
  return <ControlRoom key={dataMode} />
}
