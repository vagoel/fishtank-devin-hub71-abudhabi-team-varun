export function startPolling({ task, onSuccess, onError, interval = 2000 }) {
  let stopped = false
  let timer
  let failures = 0
  let running = false
  let activeController
  async function run() {
    if (stopped || running) return
    clearTimeout(timer)
    running = true
    activeController = new AbortController()
    try {
      const value = await task(activeController.signal)
      if (!stopped) { onSuccess(value, failures > 0); failures = 0 }
    } catch (error) {
      if (!stopped) { failures++; onError(error) }
    } finally {
      running = false
      if (!stopped) timer = setTimeout(run, Math.min(30000, interval * 2 ** Math.min(failures, 4)))
    }
  }
  run()
  return { refresh: run, stop() { stopped = true; clearTimeout(timer); activeController?.abort() } }
}
