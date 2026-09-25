import { StrictMode } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useDashboard } from './useDashboard'

function Strict({ children }) { return <StrictMode>{children}</StrictMode> }

describe('dashboard orchestration', () => {
  it('does not announce historical data or duplicate StrictMode effects', async () => {
    const { result, unmount } = renderHook(() => useDashboard(), { wrapper: Strict })
    await waitFor(() => expect(result.current.incidents).toHaveLength(3))
    expect(result.current.notices).toHaveLength(0)
    await act(async () => result.current.trigger('fall', 'AD-02'))
    expect(result.current.incidents).toHaveLength(4)
    expect(result.current.notices).toHaveLength(1)
    await act(async () => result.current.refresh())
    expect(result.current.notices).toHaveLength(1)
    unmount()
  })
  it('marks paused monitoring stale and resets without replaying the backlog', async () => {
    const { result } = renderHook(() => useDashboard())
    await waitFor(() => expect(result.current.incidents).toHaveLength(3))
    act(() => result.current.setPaused(true))
    expect(result.current.stale).toBe(true)
    await act(async () => result.current.trigger('fall', 'AD-02'))
    expect(result.current.notices).toHaveLength(1)
    await act(async () => result.current.reset())
    expect(result.current.incidents).toHaveLength(3)
    expect(result.current.notices).toHaveLength(0)
  })
})
