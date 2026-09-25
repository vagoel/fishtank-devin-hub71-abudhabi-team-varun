import { test, expect } from '@playwright/test'

test('renders the city and all four custom landmarks without runtime errors', async ({ page }, testInfo) => {
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Abu Dhabi.' })).toBeVisible()
  await expect(page.locator('.incident-card')).toHaveCount(3)
  await expect(page.locator('.site-beacon')).toHaveCount(6, { timeout: 30000 })
  await expect(page.locator('.map-loading')).toHaveCount(0)
  await page.waitForTimeout(2000)
  await page.screenshot({ path: testInfo.outputPath('overview.png'), fullPage: true })
  for (const name of ['Etihad', 'Louvre', 'Grand Mosque', 'Aldar HQ']) {
    await page.getByRole('button', { name, exact: true }).click()
    await page.waitForTimeout(1500)
    await page.screenshot({ path: testInfo.outputPath(`${name.replaceAll(' ', '-')}.png`) })
  }
  expect(errors).toEqual([])
})

test('requires human approval for dispatch and records a false alarm', async ({ page }, testInfo) => {
  await page.goto('/')
  await page.getByRole('button', { name: /Possible fall detected/ }).click()
  await expect(page.getByRole('heading', { name: 'Possible fall detected' })).toBeVisible()
  await page.getByRole('button', { name: 'Acknowledge & review' }).click()
  await page.getByRole('button', { name: /Coordinate response/ }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.locator('.dispatch-card')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('approval.png'), fullPage: true })
  await page.getByRole('button', { name: 'Confirm simulated dispatch' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('.dispatch-team')).toHaveCount(3)
  await expect(page.getByRole('button', { name: 'Mark incident resolved' })).toBeEnabled({ timeout: 15000 })
  await page.screenshot({ path: testInfo.outputPath('response.png'), fullPage: true })
  await page.getByRole('button', { name: 'Mark incident resolved' }).click()
  await expect(page.locator('.review-actions')).toContainText('Resolved')
  await page.getByRole('button', { name: 'Incident inbox', exact: true }).click()
  await page.getByRole('button', { name: /Elevated band temperature/ }).click()
  await page.getByRole('button', { name: 'Mark as false alarm' }).click()
  await expect(page.getByRole('button', { name: 'Confirm false alarm' })).toBeDisabled()
  await page.getByRole('textbox', { name: 'Reason for dismissal' }).fill('Supervisor verified the band was removed during a break.')
  await page.getByRole('button', { name: 'Confirm false alarm' }).click()
  await expect(page.locator('.review-actions')).toContainText('False alarm')
})

test('injects events, reports stale data, and supports worker search', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Demo scenarios' }).click()
  await page.getByRole('button', { name: 'Possible fall', exact: true }).click()
  await expect(page.locator('.incident-card')).toHaveCount(4)
  await expect(page.locator('.incident-toast')).toContainText('CRITICAL EVENT RECEIVED')
  await page.getByRole('button', { name: 'Pause polling' }).click()
  await expect(page.locator('.connection-chip')).toContainText('Polling paused')
  await page.getByRole('button', { name: 'Resume polling' }).click()
  await page.getByRole('button', { name: 'Close demo scenarios' }).click()
  await page.getByRole('button', { name: 'Browse workers' }).click()
  await page.getByRole('textbox', { name: 'Search workers' }).fill('WK-014')
  await expect(page.locator('.explorer-results > button')).toHaveCount(1)
  await page.locator('.explorer-results > button').click()
  await expect(page.locator('.worker-detail')).toContainText('WK-014')
})

test('remains usable on mobile and when microphone permission is denied', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(() => { navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Denied', 'NotAllowedError')) })
  await page.goto('/')
  await page.getByRole('button', { name: 'Enable voice' }).click()
  await expect(page.getByRole('alert')).toContainText('Microphone permission was denied')
  await expect(page.locator('.incident-card')).toHaveCount(3)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await expect(page.locator('.site-beacon')).toHaveCount(6, { timeout: 30000 })
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true })
})

test('keeps incident review usable without WebGL and supports keyboard cancellation', async ({ page }) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (type, ...options) {
      if (type.includes('webgl')) return null
      return original.call(this, type, ...options)
    }
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'City map unavailable' })).toBeVisible()
  await page.getByRole('button', { name: /Possible fall detected/ }).click()
  await page.getByRole('button', { name: /Coordinate response/ }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('.dispatch-overlay')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Coordinate response/ })).toBeFocused()
})

test('client bundle excludes the server key and session handler', async ({ page }) => {
  const scripts = []
  page.on('response', response => {
    if (response.request().resourceType() === 'script') scripts.push(response.text().catch(() => ''))
  })
  await page.goto('/')
  await expect(page.locator('.site-beacon')).toHaveCount(6, { timeout: 30000 })
  const content = (await Promise.all(scripts)).join('\n')
  expect(content.length).toBeGreaterThan(10000)
  expect(content).toContain('session.started')
  expect(content).not.toContain('AMAN_BUILD_SECRET_SENTINEL')
  expect(content).not.toContain('createSessionHandler')
})

test('retains the incident inbox if the lazy map bundle cannot load', async ({ page }) => {
  await page.route(/AbuDhabiMap[^/]*\.(js|jsx|css)(\?.*)?$/, route => route.abort())
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Map view unavailable' })).toBeVisible()
  await expect(page.locator('.incident-card')).toHaveCount(3)
  await page.getByRole('button', { name: /Possible fall detected/ }).click()
  await expect(page.getByRole('button', { name: /Coordinate response/ })).toBeEnabled()
})

test('standalone demo still works when all incident API traffic is blocked', async ({ page }) => {
  const apiRequests = []
  await page.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/unreachable-api/') || url.hostname.includes('telemetry-backend-')) {
      apiRequests.push(url.pathname)
      return route.abort()
    }
    return route.continue()
  })
  await page.goto('/')
  await expect(page.locator('.demo-tag')).toContainText('DEMO ENVIRONMENT')
  await expect(page.locator('.metric-card').first()).toContainText('72')
  await expect(page.locator('.incident-card')).toHaveCount(3)
  await page.getByRole('button', { name: 'Demo scenarios' }).click()
  await page.getByRole('button', { name: 'Possible fall', exact: true }).click()
  await expect(page.locator('.incident-card')).toHaveCount(4)
  await page.waitForTimeout(2300)
  expect(apiRequests).toEqual([])
})
