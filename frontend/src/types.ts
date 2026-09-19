export interface ChartSeries {
  key: string
  label: string
}

export interface ChartSpec {
  type: 'line' | 'bar'
  title: string
  x_key: string
  x_label: string
  y_label: string
  series: ChartSeries[]
  data: Record<string, string | number>[]
}

export interface FetchJobRef {
  job_id: string
  insurer: string
  fetching: string[]
}

export interface ChatResponse {
  answer: string
  flagged: boolean
  // One chart per distinctly-shaped query result this turn — empty when
  // nothing was chartable, more than one for a "plot this in multiple
  // graphs" question that ran a separate query per breakdown.
  charts: ChartSpec[]
  fetch_jobs: FetchJobRef[]
}

export interface FetchJobProgress {
  stage?: string
  filings_done?: number
  filings_total?: number
}

export interface FetchJobStatus {
  job_id: string
  insurer: string
  status: 'running' | 'succeeded' | 'failed'
  fetching: string[]
  found_new_data: boolean
  now_available: string[]
  still_unavailable: string[]
  progress: FetchJobProgress
  error: string | null
  started_at: string
  finished_at: string | null
}

// 'waiting': polling (auto-started, or resumed by Reload) or paused waiting for Reload
// 'unavailable': jobs finished without finding the data
export type FetchState = 'waiting' | 'unavailable'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  charts?: ChartSpec[]
  flagged?: boolean
  pending?: boolean
  error?: boolean
  // Set on an answer whose data is being fetched in the background.
  question?: string
  fetchJobs?: FetchJobRef[]
  fetchState?: FetchState
  fetchProgress?: string
}
