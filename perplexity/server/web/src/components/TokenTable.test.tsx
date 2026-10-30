import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ClientInfo } from 'lib/api'
import { TokenTable } from './TokenTable'

const account = (
  id: string,
  overrides: Partial<ClientInfo> = {}
): ClientInfo => ({
  id,
  enabled: true,
  available: true,
  state: 'normal',
  weight: 80,
  request_count: 0,
  fail_count: 0,
  pro_fail_count: 0,
  last_heartbeat_at: null,
  subscription_tier: 'pro',
  ...overrides
})
const clients = [
  account('primary-pro'),
  account('backup-max', { enabled: false, available: false }),
  account('expired-free', { available: false, state: 'offline' })
]
const props = {
  clients,
  adminToken: 'fixture-admin',
  isAuthenticated: true,
  fallbackToAuto: true,
  incognitoEnabled: false,
  onToast: vi.fn(),
  onRefresh: vi.fn(),
  onAddClick: vi.fn(),
  onConfirmDelete: vi.fn(),
  onFallbackChange: vi.fn(),
  onIncognitoChange: vi.fn()
}

describe('account list', () => {
  it('combines search and status filters, then restores the list', () => {
    render(<TokenTable {...props} />)
    fireEvent.change(screen.getByLabelText('Search accounts'), {
      target: { value: ' PRIMARY ' }
    })
    expect(screen.getByText('Showing 1 of 3 accounts')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Account status'), {
      target: { value: 'disabled' }
    })
    expect(screen.getByText('No matching accounts')).toBeInTheDocument()
    fireEvent.click(
      screen.getByRole('button', { name: 'Reset search and filters' })
    )
    expect(screen.getByText('Showing 3 of 3 accounts')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Account status'), {
      target: { value: 'unavailable' }
    })
    expect(screen.getByText('expired-free')).toBeInTheDocument()
    expect(screen.getByText('backup-max')).toBeInTheDocument()
    expect(screen.getByText('Showing 2 of 3 accounts')).toBeInTheDocument()
  })

  it('labels an offline account correctly instead of calling it backoff', () => {
    render(<TokenTable {...props} />)
    const row = screen.getByText('expired-free').closest('tr')!
    expect(within(row).getByText('OFFLINE')).toBeInTheDocument()
    expect(within(row).queryByText('BACKOFF')).not.toBeInTheDocument()
  })

  it('distinguishes loading, connection failure and a genuinely empty pool', () => {
    const { rerender } = render(<TokenTable {...props} clients={[]} loading />)
    expect(screen.getByText('Loading accounts…')).toBeInTheDocument()
    expect(screen.queryByText('No accounts yet')).not.toBeInTheDocument()
    rerender(<TokenTable {...props} clients={[]} loadError="Offline" />)
    expect(
      screen.getByText(/Account status is unavailable/)
    ).toBeInTheDocument()
    expect(screen.queryByText('No accounts yet')).not.toBeInTheDocument()
    rerender(<TokenTable {...props} clients={[]} />)
    expect(screen.getByText('No accounts yet')).toBeInTheDocument()
  })

  it('keeps guest controls and unloaded settings unavailable', () => {
    const { rerender } = render(
      <TokenTable {...props} isAuthenticated={false} />
    )
    expect(screen.getByRole('button', { name: '+ Add account' })).toBeDisabled()
    expect(screen.queryByText('primary-pro')).not.toBeInTheDocument()
    rerender(<TokenTable {...props} settingsReady={false} />)
    expect(screen.getByRole('button', { name: 'NORMAL' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'DOWNGRADE' })).toBeDisabled()
  })
})
