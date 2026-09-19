import { useEffect, useRef, useState } from 'react'
import { getFetchJob, getSessionId, sendMessage } from '../api'
import type { ChatMessage, ChatResponse, FetchJobStatus } from '../types'
import { MessageBubble } from './MessageBubble'

const EXAMPLE_QUESTIONS = [
  "What was HDFC Life's total premium (L-4, annual) for FY2018-19 through FY2025-26? Show YoY growth.",
  'Compare HDFC Life and Axis Max Life total premium growth, FY2023-24 vs FY2024-25',
  "What is HDFC Life's expense ratio trend by fiscal year?",
  "I'm 38, can pay Rs 2.4 lakh a year for 10 years — which guaranteed plans can I buy, and how do their IRRs compare?",
  'Compare guaranteed-return illustrations for HDFC Life Sanchay Plus and Axis Max Life SWAG',
]

// Background fetch jobs can run for minutes (scraping + downloading +
// transcribing several filings), so a waiting message polls itself — the
// Reload button is a manual nudge (and how polling resumes after
// AUTO_POLL_MAX_TICKS gives up), not something the user must keep clicking.
const POLL_INTERVAL_MS = 4000
const AUTO_POLL_MAX_TICKS = 180 // ~12 minutes

function newId(): string {
  return crypto.randomUUID()
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function answerFields(res: ChatResponse, question: string): Partial<ChatMessage> {
  const waiting = res.fetch_jobs.length > 0
  return {
    pending: false,
    error: false,
    text: res.answer,
    charts: res.charts,
    flagged: res.flagged,
    question,
    fetchJobs: waiting ? res.fetch_jobs : undefined,
    fetchState: waiting ? 'waiting' : undefined,
    fetchProgress: undefined,
  }
}

function progressText(statuses: (FetchJobStatus | null)[]): string | undefined {
  const running = statuses.filter((s): s is FetchJobStatus => s?.status === 'running')
  const withTotal = running.filter((s) => (s.progress.filings_total ?? 0) > 0)
  if (withTotal.length === 0) return running[0]?.progress.stage
  const done = withTotal.reduce((n, s) => n + (s.progress.filings_done ?? 0), 0)
  const total = withTotal.reduce((n, s) => n + (s.progress.filings_total ?? 0), 0)
  const stage = withTotal[0].progress.stage
  return stage ? `${stage} (${done} of ${total} filings)` : `${done} of ${total} filings`
}

export function ChatWindow() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const sessionId = useRef(getSessionId())
  const scrollRef = useRef<HTMLDivElement>(null)
  // Message ids with an active poll loop, so a re-render (e.g. a new
  // message) never starts a second loop for the same one.
  const pollingIds = useRef(new Set<string>())

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  // Auto-start polling for any message that arrives waiting on fetch jobs.
  useEffect(() => {
    for (const m of messages) {
      if (m.fetchState === 'waiting' && m.fetchJobs?.length && !pollingIds.current.has(m.id)) {
        void poll(m.id, m.fetchJobs, m.question!)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages])

  function updateMessage(id: string, fields: Partial<ChatMessage>) {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...fields } : m)))
  }

  async function submit(text: string) {
    const question = text.trim()
    if (!question || sending) return

    const userMsg: ChatMessage = { id: newId(), role: 'user', text: question }
    const pendingMsg: ChatMessage = { id: newId(), role: 'assistant', text: '', pending: true }
    setMessages((prev) => [...prev, userMsg, pendingMsg])
    setInput('')
    setSending(true)

    try {
      const res = await sendMessage(question, sessionId.current)
      updateMessage(pendingMsg.id, answerFields(res, question))
    } catch (err) {
      updateMessage(pendingMsg.id, {
        pending: false,
        error: true,
        text: err instanceof Error ? err.message : 'Something went wrong.',
      })
    } finally {
      setSending(false)
    }
  }

  // Polls jobs until they're all done (or AUTO_POLL_MAX_TICKS is reached),
  // updating live progress text, then re-asks the question or reports it's
  // unavailable. Runs on its own — submit() and other messages' polling and
  // Reload buttons are unaffected while this is in flight.
  async function poll(messageId: string, jobs: NonNullable<ChatMessage['fetchJobs']>, question: string) {
    if (pollingIds.current.has(messageId)) return
    pollingIds.current.add(messageId)
    updateMessage(messageId, { fetchState: 'waiting' })

    try {
      let statuses: (FetchJobStatus | null)[] = []
      for (let tick = 1; tick <= AUTO_POLL_MAX_TICKS; tick++) {
        statuses = await Promise.all(jobs.map((j) => getFetchJob(j.job_id)))
        if (!statuses.some((s) => s?.status === 'running')) break
        updateMessage(messageId, { fetchProgress: progressText(statuses) })
        if (tick === AUTO_POLL_MAX_TICKS) return // still running — Reload resumes polling
        await sleep(POLL_INTERVAL_MS)
      }

      // A missing (null, e.g. after a server restart) or failed job is
      // retried by asking again, which starts a fresh fetch; found data:
      // asking again answers with it, curated + transcribed together.
      const foundData = statuses.some((s) => s?.found_new_data)
      const retry = statuses.some((s) => s === null || s.status === 'failed')
      if (!foundData && !retry) {
        updateMessage(messageId, { fetchState: 'unavailable', fetchProgress: undefined })
        return
      }
      updateMessage(messageId, { pending: true, fetchProgress: undefined })
      const res = await sendMessage(question, sessionId.current)
      updateMessage(messageId, answerFields(res, question))
    } catch {
      // Network hiccup while polling — Reload tries again rather than erroring.
      updateMessage(messageId, { pending: false, fetchState: 'waiting', fetchProgress: undefined })
    } finally {
      pollingIds.current.delete(messageId)
    }
  }

  return (
    <div className="chat-window">
      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty-state">
            <p>
              Ask about HDFC Life and Axis Max Life's IRDAI public disclosures, or about their
              guaranteed-return product quotes — eligibility, rate lookups, and illustrations.
            </p>
            <div className="example-chips">
              {EXAMPLE_QUESTIONS.map((q) => (
                <button key={q} type="button" className="example-chip" onClick={() => submit(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            message={m}
            onReload={() => m.fetchJobs && m.question && void poll(m.id, m.fetchJobs, m.question)}
            reloadDisabled={pollingIds.current.has(m.id)}
          />
        ))}
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          submit(input)
        }}
      >
        <input
          className="composer-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about premiums, expense ratios, claims, grievances, eligibility, quotes…"
          disabled={sending}
        />
        <button className="composer-send" type="submit" disabled={sending || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}
