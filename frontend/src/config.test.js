import { describe, expect, it } from 'vitest'
import { resolveDataMode } from './config'

describe('explicit data mode', () => {
  it('defaults to standalone demo and supports the environment switch', () => {
    expect(resolveDataMode({})).toBe('demo')
    expect(resolveDataMode({ VITE_DATA_MODE: 'demo' })).toBe('demo')
    expect(resolveDataMode({ VITE_DATA_MODE: 'api' })).toBe('api')
  })
  it('always uses demo for the explicit demo launch/build even with API environment settings', () => {
    expect(resolveDataMode({ MODE: 'demo', VITE_DATA_MODE: 'api', VITE_API_BASE_URL: 'unavailable' })).toBe('demo')
    expect(resolveDataMode({ MODE: 'live', VITE_DATA_MODE: 'demo' })).toBe('api')
  })
  it('does not silently turn a misspelled live mode into fictional data', () => {
    expect(() => resolveDataMode({ VITE_DATA_MODE: 'ap1' })).toThrow('VITE_DATA_MODE')
  })
})
