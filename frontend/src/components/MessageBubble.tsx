import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ChatMessage } from '../types'
import { ChartRenderer } from './ChartRenderer'

interface MessageBubbleProps {
  message: ChatMessage
  onReload: () => void
  reloadDisabled: boolean
}

export function MessageBubble({ message, onReload, reloadDisabled }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`message-row ${isUser ? 'message-row--user' : 'message-row--assistant'}`}>
      <div className={`bubble ${isUser ? 'bubble--user' : 'bubble--assistant'} ${message.error ? 'bubble--error' : ''}`}>
        {message.pending ? (
          <span className="typing-indicator" aria-label="Thinking">
            <span />
            <span />
            <span />
          </span>
        ) : (
          <>
            <div className="markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text}</ReactMarkdown>
            </div>
            {message.flagged && (
              <p className="flagged-note">
                The agent couldn't fully verify every figure in this answer against the queried data.
              </p>
            )}
            {message.charts?.map((spec, i) => (
              // Index key is fine here: charts render once per message and never reorder.
              <ChartRenderer key={i} spec={spec} />
            ))}
            {message.fetchState && (
              <FetchStatus
                state={message.fetchState}
                fetching={(message.fetchJobs ?? []).flatMap((j) => j.fetching.map((f) => `${insurerName(j.insurer)} ${f}`))}
                progress={message.fetchProgress}
                polling={reloadDisabled}
                onReload={onReload}
                reloadDisabled={reloadDisabled}
              />
            )}
          </>
        )}
      </div>
    </div>
  )
}

function insurerName(insurer: string): string {
  return insurer === 'hdfc_life' ? 'HDFC Life' : insurer === 'axis_max_life' ? 'Axis Max Life' : insurer
}

interface FetchStatusProps {
  state: NonNullable<ChatMessage['fetchState']>
  fetching: string[]
  progress?: string
  // True while a poll loop for this message is actively running (auto-started
  // on arrival, or resumed by Reload) — reflects the harness's own state, not
  // a separate flag, so it can never drift out of sync with what Reload does.
  polling: boolean
  onReload: () => void
  reloadDisabled: boolean
}

function FetchStatus({ state, fetching, progress, polling, onReload, reloadDisabled }: FetchStatusProps) {
  return (
    <div className="fetch-status" role="status" aria-live="polite">
      <div className="fetch-status-text">
        {state === 'unavailable' ? (
          <p className="fetch-status-title">This data still isn't available from the insurer's website.</p>
        ) : (
          <p className="fetch-status-title">
            {polling && <span className="spinner" aria-hidden="true" />}
            Getting data for you... Please wait
          </p>
        )}
        {state !== 'unavailable' && progress && <p className="fetch-status-detail">{progress}</p>}
        {fetching.length > 0 && <p className="fetch-status-detail">{fetching.join(', ')}</p>}
      </div>
      <button type="button" className="reload-button" onClick={onReload} disabled={reloadDisabled}>
        {polling ? 'Checking…' : 'Reload'}
      </button>
    </div>
  )
}
