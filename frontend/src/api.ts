import type { ChatResponse, FetchJobStatus } from './types'

const SESSION_STORAGE_KEY = 'irdai-bot.session-id'

export function getSessionId(): string {
  let id = localStorage.getItem(SESSION_STORAGE_KEY)
  if (!id) {
    id = crypto.randomUUID()
    localStorage.setItem(SESSION_STORAGE_KEY, id)
  }
  return id
}

export async function sendMessage(message: string, sessionId: string): Promise<ChatResponse> {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId }),
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new Error(`Request failed (${res.status}): ${detail || res.statusText}`)
  }
  return res.json()
}

export async function getFetchJob(jobId: string): Promise<FetchJobStatus | null> {
  const res = await fetch(`/api/fetch-jobs/${encodeURIComponent(jobId)}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`Status check failed (${res.status})`)
  return res.json()
}

export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch('/api/health')
    return res.ok
  } catch {
    return false
  }
}
