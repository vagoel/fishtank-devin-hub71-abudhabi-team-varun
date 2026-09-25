import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ReviewDialog from './ReviewDialog'
import { createDemoData } from '../data/demoData'

const incident = createDemoData().incidents[0]

describe('human approval boundary', () => {
  it('requires an explicit confirmation and at least one service', () => {
    const confirm = vi.fn()
    render(<ReviewDialog kind="dispatch" incident={incident} onClose={vi.fn()} onConfirm={confirm} />)
    expect(confirm).not.toHaveBeenCalled()
    for (const checkbox of screen.getAllByRole('checkbox')) fireEvent.click(checkbox)
    expect(screen.getByRole('button', { name: 'Confirm simulated dispatch' }).disabled).toBe(true)
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByRole('button', { name: 'Confirm simulated dispatch' }))
    expect(confirm).toHaveBeenCalledExactlyOnceWith(['medical'])
  })
  it('requires a reason before recording a false alarm', () => {
    const confirm = vi.fn()
    render(<ReviewDialog kind="dismiss" incident={incident} onClose={vi.fn()} onConfirm={confirm} />)
    const button = screen.getByRole('button', { name: 'Confirm false alarm' })
    expect(button.disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Supervisor verified worker is safe.' } })
    fireEvent.click(button)
    expect(confirm).toHaveBeenCalledWith({ reason: 'Supervisor verified worker is safe.' })
  })
  it('blocks duplicate actions while saving', () => {
    render(<ReviewDialog kind="dispatch" incident={incident} busy onClose={vi.fn()} onConfirm={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Saving…' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Go back' }).disabled).toBe(true)
  })
})
