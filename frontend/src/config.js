export const defaultIncidentTarget = 'https://telemetry-backend-501582454609.asia-northeast1.run.app'

export function resolveDataMode(env = {}) {
  if (env.MODE === 'demo') return 'demo'
  if (env.MODE === 'live') return 'api'
  const value = env.VITE_DATA_MODE || 'demo'
  if (!['demo', 'api'].includes(value)) throw new Error('VITE_DATA_MODE must be demo or api. Use dev:demo for a guaranteed standalone demonstration.')
  return value
}
