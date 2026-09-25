import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createDemoData } from '../data/demoData'
import { useGptLiveVoice } from './useGptLiveVoice'

const mock = vi.hoisted(() => ({ options: null, append: vi.fn(() => true), start: vi.fn(), close: vi.fn(), dispose: vi.fn() }))
vi.mock('../services/gptLiveVoice', () => ({ createGptLiveVoice(options) { mock.options = options; return { ...mock, muteInput: vi.fn(), muteOutput: vi.fn() } } }))

beforeEach(() => { mock.options = null; mock.append.mockClear(); mock.start.mockClear() })
const makeDashboard = () => ({ ...createDemoData(), mode: 'demo', stale: false, notices: [] })

describe('voice UI state', () => {
  it('requires enablement, preserves full-duplex transcript timing, and counts usage cumulatively', async () => {
    const dashboard = makeDashboard()
    const { result } = renderHook(() => useGptLiveVoice(dashboard, null))
    expect(mock.options).toBeNull()
    await act(() => result.current.start())
    act(() => mock.options.onState('connected'))
    act(() => {
      mock.options.onEvent({ type: 'session.input_transcript.delta', delta: 'Where is ', start_ms: 10, end_ms: 40 })
      mock.options.onEvent({ type: 'session.output_transcript.delta', delta: 'Saadiyat.', start_ms: 20, end_ms: 50 })
      mock.options.onEvent({ type: 'session.input_transcript.delta', delta: 'the worker?', start_ms: 40, end_ms: 80 })
      mock.options.onEvent({ type: 'session.usage.updated', usage: { seconds: 12 } })
      mock.options.onEvent({ type: 'session.usage.updated', usage: { seconds: 15 } })
    })
    expect(result.current.transcripts.input).toBe('Where is the worker?')
    expect(result.current.transcripts.output).toBe('Saadiyat.')
    expect(result.current.transcripts.fragments).toHaveLength(3)
    expect(result.current.transcripts.fragments[1]).toMatchObject({ startMs: 20, endMs: 50 })
    expect(result.current.usage).toBe(15)
  })
  it('announces a new incident once and returns only read-only delegation context', async () => {
    const dashboard = makeDashboard()
    const { result, rerender } = renderHook(({ data }) => useGptLiveVoice(data, null), { initialProps: { data: dashboard } })
    await act(() => result.current.start())
    act(() => mock.options.onState('connected'))
    mock.append.mockClear()
    const notice = { ...dashboard.incidents[0], noticeId: 'new:1' }
    rerender({ data: { ...dashboard, notices: [notice] } })
    expect(mock.append.mock.calls.filter(([kind]) => kind === 'commentary')).toHaveLength(1)
    rerender({ data: { ...dashboard, notices: [notice] } })
    expect(mock.append.mock.calls.filter(([kind]) => kind === 'commentary')).toHaveLength(1)
    act(() => mock.options.onEvent({ type: 'session.delegation.created', delegation: { id: 'task-1', target: 'client' }, offset_ms: 500 }))
    expect(mock.append).toHaveBeenLastCalledWith('commentary', expect.stringContaining('No action was taken'), 'task-1')
  })
})
