import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => cleanup())

if (typeof window !== 'undefined') {
  window.matchMedia ||= vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  HTMLDialogElement.prototype.showModal ||= function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close ||= function () { this.removeAttribute('open') }
}
