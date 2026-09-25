import { createDemoData, demoAnalysis, makeIncident } from '../data/demoData'
import { transitionIncident } from '../domain/incidents'

export function createDemoApi() {
  let state = createDemoData()
  let sequence = 1042
  const clone = () => structuredClone(state)
  const touch = () => { state.cursor = String(Number(state.cursor) + 1) }
  const heartbeat = () => {
    const now = new Date().toISOString()
    state.serverTime = now
    state.sites.forEach(site => { if (site.connectivity === 'online') site.lastSeen = now })
    state.workers.forEach(worker => { if (worker.connectivity === 'online') worker.lastSeen = now })
    state.incidents.forEach(incident => {
      if (incident.analysis.status === 'pending' && Date.now() - Date.parse(incident.createdAt) > 3500) {
        incident.analysis = demoAnalysis(incident.type)
        incident.revision++
        incident.timeline.push({ at: now, label: 'Demo analysis available for human review' })
      }
    })
    state.dispatches.forEach(dispatch => {
      const elapsed = Date.now() - Date.parse(dispatch.createdAt)
      const incident = state.incidents.find(item => item.id === dispatch.incidentId)
      if (incident?.status === 'dispatching' && elapsed > 6000) {
        Object.assign(incident, transitionIncident(incident, 'dispatched'))
      }
    })
  }
  return {
    mode: 'demo',
    async loadSnapshot() { heartbeat(); return clone() },
    async pollChanges() { heartbeat(); return clone() },
    async reviewIncident(id, revision, status, options) {
      const incident = state.incidents.find(item => item.id === id)
      if (!incident || incident.revision !== revision) throw new Error('This incident changed. Review the latest information and try again.')
      Object.assign(incident, transitionIncident(incident, status, options))
      touch()
      return clone()
    },
    async simulateDispatch(id, revision, services) {
      const incident = state.incidents.find(item => item.id === id)
      if (!incident || incident.revision !== revision) throw new Error('This incident changed. Review it before confirming dispatch.')
      Object.assign(incident, transitionIncident(incident, 'confirmed', { services }))
      Object.assign(incident, transitionIncident(incident, 'dispatching'))
      state.dispatches.push({ id: `DSP-${id}`, incidentId: id, siteId: incident.siteId, services, createdAt: new Date().toISOString(), simulated: true })
      touch()
      return clone()
    },
    async trigger(type, siteId = 'AD-02') {
      const site = state.sites.find(item => item.id === siteId) || state.sites[0]
      if (type === 'offline') {
        site.connectivity = site.connectivity === 'online' ? 'offline' : 'online'
        state.workers.filter(worker => worker.siteId === site.id).forEach(worker => { worker.connectivity = site.connectivity })
      } else {
        const worker = state.workers.find(item => item.siteId === site.id)
        state.incidents.unshift(makeIncident(type, site.id, worker.id, ++sequence))
        if (type === 'temperature') { worker.temperature = 39.1; worker.readings = [35.5, 35.7, 36, 36.3, 36.5, 37, 37.3, 37.7, 38, 38.4, 38.8, 39.1] }
      }
      touch()
      return clone()
    },
    async reset() { state = createDemoData(); return clone() },
  }
}
