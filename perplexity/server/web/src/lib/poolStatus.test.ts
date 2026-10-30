import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchPoolStatus } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('account status responses', () => {
  it('rejects an HTTP error instead of treating it as account data', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response('{"message":"Unavailable"}', { status: 503 })
        )
    )
    await expect(fetchPoolStatus()).rejects.toThrow('503')
  })
  it('rejects a malformed success response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{"status":"error"}'))
    )
    await expect(fetchPoolStatus()).rejects.toThrow(
      'Invalid account status response'
    )
  })
})
