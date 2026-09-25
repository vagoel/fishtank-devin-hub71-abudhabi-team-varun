import { afterEach, describe, expect, it, vi } from 'vitest'
import { startPolling } from './polling'

afterEach(() => vi.useRealTimers())

describe('nonoverlapping polling', () => {
  it('does not overlap refreshes and aborts in-flight work on cleanup', async () => {
    vi.useFakeTimers()
    let signal
    let finish
    const task = vi.fn(input => { signal = input; return new Promise(resolve => { finish = resolve }) })
    const success = vi.fn()
    const loop = startPolling({ task, onSuccess: success, onError: vi.fn(), interval: 2000 })
    loop.refresh()
    await vi.advanceTimersByTimeAsync(6000)
    expect(task).toHaveBeenCalledTimes(1)
    loop.stop()
    expect(signal.aborted).toBe(true)
    finish('late response')
    await Promise.resolve()
    expect(success).not.toHaveBeenCalled()
  })
  it('backs off on error and reports recovery without duplicate work', async () => {
    vi.useFakeTimers()
    const task = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue('snapshot')
    const success = vi.fn()
    const failure = vi.fn()
    const loop = startPolling({ task, onSuccess: success, onError: failure, interval: 2000 })
    await vi.advanceTimersByTimeAsync(0)
    expect(failure).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(3999)
    expect(task).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(success).toHaveBeenCalledWith('snapshot', true)
    await vi.advanceTimersByTimeAsync(2000)
    expect(success).toHaveBeenLastCalledWith('snapshot', false)
    loop.stop()
  })
})
