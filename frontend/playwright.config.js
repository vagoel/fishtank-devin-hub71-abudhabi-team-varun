import { defineConfig } from '@playwright/test'

const api = process.env.E2E_MODE === 'api'
const port = api ? 5177 : 5175
const baseURL = process.env.PLAYWRIGHT_BASE_URL || `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  testMatch: api ? 'incidents.spec.js' : 'dashboard.spec.js',
  outputDir: './browser-artifacts',
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  use: { baseURL, channel: 'chrome', viewport: { width: 1440, height: 960 }, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: process.env.PLAYWRIGHT_BASE_URL ? undefined : { command: `npm run ${api ? 'dev:api' : 'dev:demo'} -- --host 127.0.0.1 --port ${port} --strictPort`, url: baseURL, reuseExistingServer: !process.env.CI },
})
