import axios from 'axios'

// 创建axios实例
const service = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:5001',
  timeout: 300000, // 5分钟超时（本体生成可能需要较长时间）
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
service.interceptors.request.use(
  config => {
    return config
  },
  error => {
    console.error('Request error:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器（容错重试机制）
service.interceptors.response.use(
  response => {
    const res = response.data
    
    // 如果返回的状态码不是success，则抛出错误
    if (!res.success && res.success !== undefined) {
      console.error('API Error:', res.error || res.message || 'Unknown error')
      return Promise.reject(new Error(res.error || res.message || 'Error'))
    }
    
    return res
  },
  error => {
    console.error('Response error:', error)
    
    // 处理超时
    if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
      console.error('Request timeout')
    }
    
    // 处理网络错误
    if (error.message === 'Network Error') {
      console.error('Network error - please check your connection')
    }
    
    return Promise.reject(error)
  }
)

// HTTP methods that are safe to retry blindly (no server-side state change).
// Issue #18: retrying a non-idempotent POST duplicates expensive LLM/simulation
// work, so a POST is only retried when the caller supplies an idempotency key
// (server-side idempotency is tracked in #12).
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

const MAX_BACKOFF_MS = 30000
const MAX_RETRY_AFTER_MS = 60000

// Compute the wait before the next retry. A 429 with a Retry-After header is
// honoured (capped); otherwise capped exponential backoff. Exported for tests.
export const computeRetryDelay = (error, baseDelay, attempt) => {
  const status = error?.response?.status
  const retryAfter = error?.response?.headers?.['retry-after']
  if (status === 429 && retryAfter != null) {
    const seconds = Number(retryAfter)
    // > 0 (not >= 0): an empty-string header coerces to 0 via Number(''), which
    // would mean a 0ms "instant" retry; RFC 7231 treats a malformed/empty
    // Retry-After as absent, so fall through to backoff instead.
    if (Number.isFinite(seconds) && seconds > 0) {
      return Math.min(seconds * 1000, MAX_RETRY_AFTER_MS)
    }
  }
  return Math.min(baseDelay * Math.pow(2, attempt), MAX_BACKOFF_MS)
}

// 带重试的请求函数
// requestFn: () => Promise — the request thunk (closes over its own method/body).
// options.method: the HTTP method, so we can refuse to retry non-idempotent ones.
// options.idempotencyKey: when set, a non-safe method MAY be retried (the caller
//   is responsible for sending the same key as a header on every attempt).
// options.retries: max retries (after the initial attempt) for retryable requests.
export const requestWithRetry = async (
  requestFn,
  { retries = 3, delay = 1000, method = 'GET', idempotencyKey = null } = {}
) => {
  const retryable = SAFE_METHODS.has(String(method).toUpperCase()) || Boolean(idempotencyKey)
  const maxAttempts = retryable ? retries : 0
  for (let attempt = 0; ; attempt++) {
    try {
      return await requestFn()
    } catch (error) {
      if (attempt >= maxAttempts) throw error
      console.warn(`Request failed, retrying (${attempt + 1}/${maxAttempts})...`)
      await new Promise(resolve => setTimeout(resolve, computeRetryDelay(error, delay, attempt)))
    }
  }
}

export default service
