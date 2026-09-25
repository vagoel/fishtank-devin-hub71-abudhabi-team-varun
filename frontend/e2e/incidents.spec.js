import { test, expect } from '@playwright/test'

const report = {
  schema: 'heatguard.incident.v1', incident_id: 'sticks3-7ce8b1-9f3a01c2-17', device_id: 'sticks3-7ce8b1',
  person: { name: 'Ravi Kumar', trade: 'Steel fixer', crew: 'B-2' }, type: 'fall', status: 'suspected', severity: 'critical',
  occurred_at: '2026-09-25T12:41:07Z', location: { lat: 24.8974, lon: 55.161, label: 'Tower B · Level 4' },
  details: { impact_g: 7.5, freefall_ms: 361, message: 'Worker has not responded yet.' }, source: 'device',
}

async function routeFeed(page, state) {
  const methods = []
  await page.route('**/api/v1/incidents', route => {
    methods.push(route.request().method())
    return route.fulfill({ status: state.status || 200, contentType: 'application/json', body: JSON.stringify(state.payload) })
  })
  return methods
}

test('renders the incident contract, reports source details, and frames coordinates outside the original city bounds', async ({ page }, testInfo) => {
  const methods = await routeFeed(page, { payload: [{ ...report, source: 'api', alert_id: 'A-TEST', escalated: ['call', 'whatsapp'], updated_at: report.occurred_at, person: { ...report.person, worker_id: 'LIVE-7ce8b1' } }] })
  await page.goto('/')
  await expect(page.locator('.demo-tag')).toContainText('API MODE')
  await expect(page.locator('.incident-card')).toHaveCount(1)
  await expect(page.getByText('Connectivity is not provided', { exact: true })).toBeVisible()
  await expect(page.locator('.site-beacon')).toBeInViewport({ ratio: 0.9, timeout: 30000 })
  await page.getByRole('button', { name: /Possible fall detected/ }).click()
  await expect(page.locator('.worker-detail')).toContainText('Ravi Kumar')
  await expect(page.locator('.worker-detail')).toContainText('Steel fixer')
  await expect(page.locator('.worker-detail')).toContainText('B-2')
  await expect(page.locator('.evidence-block')).toContainText('7.5 g')
  await expect(page.locator('.reported-details')).toContainText('361 ms')
  await expect(page.locator('.reported-details')).toContainText('Worker has not responded yet.')
  await expect(page.locator('.reported-details')).toContainText('call, whatsapp')
  await expect(page.locator('.reported-details')).toContainText('not initiated by this dashboard')
  await expect(page.locator('.worker-detail')).toContainText('LIVE-7ce8b1')
  await expect(page.locator('.review-actions')).toContainText('Source status · read-only')
  await expect(page.getByRole('button', { name: /Coordinate response/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Acknowledge & review/ })).toHaveCount(0)
  await expect(page.getByText('Band surface', { exact: true })).toHaveCount(0)
  await expect(page.locator('.worker-detail .sparkline')).toHaveCount(0)
  expect(methods.every(method => method === 'GET')).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('live-incident.png'), fullPage: true })
})

test('polls new incidents without duplicates and preserves source outcomes in history', async ({ page }) => {
  const state = { payload: [{ ...report, severity: 'warning' }] }
  await routeFeed(page, state)
  await page.goto('/')
  await expect(page.locator('.incident-card')).toHaveCount(1)
  const second = { ...report, incident_id: 'incident-2', type: 'manual_sos' }
  state.payload = [report, second]
  await expect(page.locator('.incident-card')).toHaveCount(2, { timeout: 10000 })
  await expect(page.locator('.incident-toast')).toBeVisible()
  await page.waitForTimeout(2300)
  await expect(page.locator('.incident-card')).toHaveCount(2)
  state.payload = [report, { ...second, status: 'worker_ok', severity: 'info' }]
  await expect(page.locator('.incident-card')).toHaveCount(1, { timeout: 10000 })
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await expect(page.locator('.incident-card')).toContainText('Worker reports OK')
  await page.locator('.incident-card').click()
  await expect(page.locator('.review-actions')).toContainText('Worker reports OK')
  await expect(page.locator('.review-actions')).not.toContainText('False alarm')
})

test('keeps informational incidents blue and labels fallback identity and location as demo assignments', async ({ page }) => {
  await routeFeed(page, { payload: { incidents: [{ ...report, type: 'other', severity: 'info', person: null, location: null }] } })
  await page.goto('/')
  await page.getByRole('button', { name: 'Info', exact: true }).click()
  await expect(page.locator('.incident-card.info')).toHaveCount(1)
  await expect(page.locator('.site-beacon.info')).toHaveCount(1, { timeout: 30000 })
  await page.locator('.incident-card').click()
  await expect(page.locator('.detail-heading .assignment-note')).toContainText('Demo coordinates')
  await expect(page.locator('.worker-detail')).toContainText('Demo identity')
  await expect(page.locator('.worker-detail')).toContainText('Demo location assignment')
})

test('retains live reports during an outage without silently loading demo workers', async ({ page }) => {
  const state = { payload: [report], status: 200 }
  await routeFeed(page, state)
  await page.goto('/')
  await expect(page.locator('.incident-card')).toHaveCount(1)
  state.status = 503
  await expect(page.locator('.connection-chip')).toContainText('Data stale', { timeout: 15000 })
  await expect(page.locator('.incident-card')).toHaveCount(1)
  await expect(page.locator('.demo-tag')).toContainText('API MODE')
  await expect(page.getByRole('button', { name: 'Demo scenarios' })).toHaveCount(0)
  await expect(page.locator('.mode-notice')).toContainText('VITE_DATA_MODE=demo')
})

test('handles an undeployed endpoint without claiming a healthy workforce', async ({ page }) => {
  await routeFeed(page, { payload: { detail: 'Not Found' }, status: 404 })
  await page.goto('/')
  await expect(page.locator('.error-banner')).toContainText('GET /v1/incidents is not available')
  await expect(page.locator('.incident-card')).toHaveCount(0)
  await expect(page.locator('.critical-metric strong')).toContainText('—')
  await expect(page.locator('.critical-metric')).toContainText('Awaiting source reports')
  await expect(page.locator('.mode-notice')).toContainText('dev:demo')
})
