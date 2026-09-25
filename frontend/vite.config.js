import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'
import { fileURLToPath } from 'node:url'
import { gptLiveSessionPlugin } from './dev/gptLiveSession.js'
import { defaultIncidentTarget, resolveDataMode } from './src/config.js'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), ['VITE_', 'INCIDENT_'])
  const live = resolveDataMode({ ...env, MODE: mode }) === 'api'
  const proxy = { '/api': 'http://localhost:8000' }
  if (live) {
    const target = new URL(env.INCIDENT_API_TARGET || defaultIncidentTarget)
    if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password || target.search || target.hash) throw new Error('INCIDENT_API_TARGET must be an HTTP(S) API base URL without credentials, query, or fragment.')
    Object.assign(proxy, {
      '^/api/v1/incidents(?:\\?|$)': {
        target: target.toString(), changeOrigin: true,
        rewrite: path => path.replace(/^\/api/, ''),
        bypass(req, res) {
          if (req.method !== 'GET') {
            res.writeHead(405, { 'Content-Type': 'application/json', Allow: 'GET' })
            res.end(JSON.stringify({ error: 'The incident proxy is read-only.' }))
            return false
          }
        },
      },
    })
  }
  const incidentProxy = live ? { '^/api/v1/incidents(?:\\?|$)': proxy['^/api/v1/incidents(?:\\?|$)'] } : {}
  return {
    plugins: [react(), gptLiveSessionPlugin()],
    resolve: { dedupe: ['react', 'react-dom'] },
    test: {
      include: ['src/**/*.test.{js,jsx}', 'dev/**/*.test.js'],
      environment: 'jsdom',
      env: { VITE_DATA_MODE: 'demo', VITE_API_BASE_URL: '/api' },
      setupFiles: ['./src/testSetup.js'],
      restoreMocks: true,
    },
    server: { proxy: { ...incidentProxy, '/api': proxy['/api'] } },
  }
})
