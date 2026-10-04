import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusStrip } from './StatusStrip'
import { type BoardLine } from './statusStrip'

const ui = { version: '0.4.2', commit: 'unknown', builtAt: 'unknown' }
const ok = (version: string) => ({ phase: 'ok', health: { status: 'ok', version } }) as const

describe('StatusStrip', () => {
  it('renders both versions in one bracket and no board half', () => {
    render(<StatusStrip ui={ui} health={ok('0.4.2')} />)
    expect(screen.getByTestId('strip-versions')).toHaveTextContent('[ UI 0.4.2 · API 0.4.2 ]')
    expect(screen.queryByTestId('strip-board')).toBeNull()
    expect(screen.queryByTestId('strip-mismatch')).toBeNull()
  })

  it('says UI and API differ, in words', () => {
    render(<StatusStrip ui={ui} health={ok('0.4.3')} />)
    expect(screen.getByTestId('strip-mismatch')).toHaveTextContent('UI and API differ')
  })

  it('shows name and id, firmware, and a tone class with its word', () => {
    const board: BoardLine = {
      kind: 'board',
      deviceId: 'a4cf12b3de90',
      name: 'shed',
      platform: 'esp32c6',
      firmware: 'fw 1.4.2 → 1.5.0',
      state: [
        { text: 'online', tone: 'ok' },
        { text: 'last update rolled back', tone: 'bad' },
      ],
    }
    render(<StatusStrip ui={ui} health={ok('0.4.2')} board={board} />)
    const strip = within(screen.getByTestId('strip-board'))
    expect(strip.getByText('shed')).toBeInTheDocument()
    expect(strip.getByText('(a4cf12b3de90)')).toBeInTheDocument()
    expect(strip.getByText('last update rolled back')).toHaveClass('bad')
    expect(strip.getByText('online')).toHaveClass('ok')
    expect(screen.getByTestId('strip-board')).toHaveTextContent(
      'esp32c6 · fw 1.4.2 → 1.5.0 · online · last update rolled back',
    )
  })

  it('renders the none and loading sentences', () => {
    const { rerender } = render(
      <StatusStrip ui={ui} health={ok('0.4.2')} board={{ kind: 'loading' }} />,
    )
    expect(screen.getByTestId('strip-board')).toHaveTextContent('Board: loading…')
    rerender(
      <StatusStrip
        ui={ui}
        health={ok('0.4.2')}
        board={{ kind: 'none', reason: 'not-selected' }}
      />,
    )
    expect(screen.getByTestId('strip-board')).toHaveTextContent('none selected')
  })
})
