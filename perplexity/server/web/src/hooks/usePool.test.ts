import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from 'lib/api'
import { usePool } from './usePool'

vi.mock('lib/api')

const pool = { total: 2, available: 1, mode: 'round_robin', clients: [] }
const config = {
  enable: true,
  interval: 6,
  question: 'fixture',
  tg_bot_token: null,
  tg_chat_id: null
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.fetchPoolStatus).mockResolvedValue(pool)
  vi.mocked(api.fetchFallbackConfig).mockResolvedValue({
    status: 'ok',
    config: { fallback_to_auto: false }
  })
  vi.mocked(api.fetchIncognitoConfig).mockResolvedValue({
    status: 'ok',
    config: { enabled: true }
  })
  vi.mocked(api.fetchHeartbeatConfig).mockResolvedValue({
    status: 'ok',
    config
  })
})

describe('account dashboard refresh', () => {
  it('loads settings immediately after login without reloading the page', async () => {
    const { result, rerender } = renderHook(({ token }) => usePool(token), {
      initialProps: { token: '' }
    })
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(api.fetchHeartbeatConfig).not.toHaveBeenCalled()
    expect(result.current.fallbackConfig.fallback_to_auto).toBe(false)
    rerender({ token: 'fixture-admin' })
    await waitFor(() => expect(result.current.hbConfig).toEqual(config))
    expect(api.fetchHeartbeatConfig).toHaveBeenCalledWith(
      'fixture-admin',
      expect.any(AbortSignal)
    )
  })

  it('keeps the account list available when a settings endpoint fails', async () => {
    vi.mocked(api.fetchHeartbeatConfig).mockRejectedValue(
      new Error('Settings unavailable')
    )
    const { result } = renderHook(() => usePool('fixture-admin'))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.data).toEqual(pool)
    expect(result.current.error).toBeNull()
    expect(result.current.configError).toContain('settings could not be loaded')
    expect(result.current.settingsReady).toBe(true)
  })

  it('keeps the last successful data during a failure and recovers on refresh', async () => {
    const { result } = renderHook(() => usePool(''))
    await waitFor(() => expect(result.current.lastSync).not.toBeNull())
    const lastSync = result.current.lastSync
    vi.mocked(api.fetchPoolStatus).mockRejectedValueOnce(new Error('Offline'))
    await act(async () => {
      await result.current.refreshData()
    })
    expect(result.current.data).toEqual(pool)
    expect(result.current.lastSync).toBe(lastSync)
    expect(result.current.error).toContain('Could not refresh')
    await act(async () => {
      await result.current.refreshData()
    })
    expect(result.current.error).toBeNull()
  })

  it('does not restore admin settings from a late response after logout', async () => {
    let finish!: (value: api.ApiResponse<api.HeartbeatConfig>) => void
    vi.mocked(api.fetchHeartbeatConfig).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve
        })
    )
    const { result, rerender } = renderHook(({ token }) => usePool(token), {
      initialProps: { token: 'fixture-admin' }
    })
    const signal = vi.mocked(api.fetchHeartbeatConfig).mock.calls[0][1]!
    rerender({ token: '' })
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(signal.aborted).toBe(true)
    await act(async () => {
      finish({ status: 'ok', config })
    })
    expect(result.current.hbConfig).toBeNull()
  })

  it('disables mode changes when their settings could not be loaded', async () => {
    vi.mocked(api.fetchFallbackConfig).mockRejectedValue(new Error('Offline'))
    const { result } = renderHook(() => usePool('fixture-admin'))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.settingsReady).toBe(false)
    expect(result.current.configError).not.toBeNull()
  })

  it('aborts outstanding reads when the dashboard unmounts', () => {
    vi.mocked(api.fetchPoolStatus).mockImplementation(
      () => new Promise(() => {})
    )
    const { unmount } = renderHook(() => usePool(''))
    const signal = vi.mocked(api.fetchPoolStatus).mock.calls[0][0]!
    unmount()
    expect(signal.aborted).toBe(true)
  })
})
