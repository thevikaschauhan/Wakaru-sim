import { describe, it, expect, vi } from 'vitest'

import { computeRetryDelay, requestWithRetry } from './index.js'

const fail = () => Promise.reject(new Error('boom'))

describe('requestWithRetry — method gating (issue #18)', () => {
  it('does NOT retry a failing POST without an idempotency key', async () => {
    const fn = vi.fn(fail)
    await expect(
      requestWithRetry(fn, { method: 'POST', retries: 3, delay: 0 })
    ).rejects.toThrow('boom')
    expect(fn).toHaveBeenCalledTimes(1) // single attempt, no retry
  })

  it('retries a failing POST up to N times when an idempotency key is supplied', async () => {
    const fn = vi.fn(fail)
    await expect(
      requestWithRetry(fn, { method: 'POST', idempotencyKey: 'key-123', retries: 3, delay: 0 })
    ).rejects.toThrow('boom')
    expect(fn).toHaveBeenCalledTimes(4) // 1 initial + 3 retries, same closure (same key)
  })

  it('retries a failing safe GET', async () => {
    const fn = vi.fn(fail)
    await expect(
      requestWithRetry(fn, { method: 'GET', retries: 2, delay: 0 })
    ).rejects.toThrow('boom')
    expect(fn).toHaveBeenCalledTimes(3) // 1 + 2 retries
  })

  it('defaults to retryable GET semantics when no options are passed', async () => {
    const fn = vi.fn(fail)
    await expect(requestWithRetry(fn, { delay: 0 })).rejects.toThrow('boom')
    expect(fn).toHaveBeenCalledTimes(4) // default method GET, retries 3
  })

  it('returns the result on success without retrying', async () => {
    const fn = vi.fn(() => Promise.resolve({ ok: true }))
    const res = await requestWithRetry(fn, { method: 'POST' })
    expect(res).toEqual({ ok: true })
    expect(fn).toHaveBeenCalledTimes(1)
  })
})

describe('computeRetryDelay — 429 / backoff (issue #18)', () => {
  it('honours Retry-After on a 429 (seconds -> ms)', () => {
    const err = { response: { status: 429, headers: { 'retry-after': '2' } } }
    expect(computeRetryDelay(err, 1000, 0)).toBe(2000)
  })

  it('caps a huge Retry-After', () => {
    const err = { response: { status: 429, headers: { 'retry-after': '99999' } } }
    expect(computeRetryDelay(err, 1000, 0)).toBe(60000)
  })

  it('uses capped exponential backoff without a Retry-After header', () => {
    expect(computeRetryDelay(new Error('x'), 1000, 0)).toBe(1000)
    expect(computeRetryDelay(new Error('x'), 1000, 1)).toBe(2000)
    expect(computeRetryDelay(new Error('x'), 1000, 2)).toBe(4000)
    expect(computeRetryDelay(new Error('x'), 1000, 10)).toBe(30000) // capped
  })

  it('ignores a non-numeric (HTTP-date) Retry-After and falls back to backoff', () => {
    const err = {
      response: { status: 429, headers: { 'retry-after': 'Wed, 21 Oct 2025 07:28:00 GMT' } },
    }
    expect(computeRetryDelay(err, 1000, 0)).toBe(1000)
  })

  it('treats an empty Retry-After as absent (no 0ms instant retry)', () => {
    const err = { response: { status: 429, headers: { 'retry-after': '' } } }
    expect(computeRetryDelay(err, 1000, 1)).toBe(2000) // backoff, not 0
  })
})
