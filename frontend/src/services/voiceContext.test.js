import { describe, expect, it } from 'vitest'
import { createDemoData } from '../data/demoData'
import { currentVoiceContext, delegationContext, incidentBriefing } from './voiceContext'

const dashboard = { ...createDemoData(), mode: 'demo', stale: false }

describe('GPT-Live grounding', () => {
  it('labels demo data, keeps temperature distinctions, and never claims dispatch', () => {
    const briefing = incidentBriefing(dashboard.incidents[1], dashboard)
    expect(briefing).toContain('Demo incident')
    expect(briefing).toContain('band temperature')
    expect(briefing).toContain('No emergency services have been contacted')
    expect(briefing.length).toBeLessThan(1100)
  })
  it('marks stale data and does not invent pending analysis', () => {
    const context = currentVoiceContext({ ...dashboard, stale: true, incidents: [{ ...dashboard.incidents[0], analysis: { status: 'pending' } }] })
    expect(context).toContain('stale')
    expect(context).toContain('Not ready')
  })
  it('labels live fallback assignments and missing assessment data without implying current vitals', () => {
    const incident = { ...dashboard.incidents[0], incidentOnly: true, identityIsDemo: true, locationIsDemo: true, analysis: { status: 'unavailable' } }
    const live = { ...dashboard, mode: 'api', incidents: [incident] }
    expect(incidentBriefing(incident, live)).toContain('demo assignment')
    expect(currentVoiceContext(live)).toContain('Current vitals, device connectivity, and Devin assessments are not supplied')
    expect(currentVoiceContext(live)).toContain('Not provided by this endpoint')
  })
  it('handles client delegation metadata without pretending it is a tool call', () => {
    expect(delegationContext({ delegation: { target: 'responses', id: 'd1' } }, dashboard)).toBeNull()
    const result = delegationContext({ delegation: { target: 'client', id: 'd2' }, offset_ms: 900 }, dashboard)
    expect(result.delegationId).toBe('d2')
    expect(result.content).toContain('No action was taken')
    expect(result.content.length).toBeLessThanOrEqual(1100)
  })
})
