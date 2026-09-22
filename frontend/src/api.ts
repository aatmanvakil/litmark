/**
 * Typed client for the local server.
 *
 * Every request carries the per-process session credential that the server
 * injected into this page, so an unrelated website cannot drive the API.
 */

export const SESSION_TOKEN =
  document.querySelector<HTMLMetaElement>('meta[name="research-token"]')?.content ?? ''

export interface ApiErrorBody {
  code: string
  message: string
  expected_revision?: string | null
  actual_revision?: string | null
  current_text?: string | null
  proposed_text?: string | null
}

export class ApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.status = status
    this.body = body
  }

  get isConflict(): boolean {
    return this.status === 409
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('x-research-token', SESSION_TOKEN)
  if (init.body && !(init.body instanceof FormData)) {
    headers.set('content-type', 'application/json')
  }
  const response = await fetch(`/api${path}`, { ...init, headers })
  if (response.status === 204) return undefined as T
  const text = await response.text()
  const payload = text ? JSON.parse(text) : {}
  if (!response.ok) {
    const body: ApiErrorBody = payload?.error ?? {
      code: 'http_error',
      message: `${response.status} ${response.statusText}`,
    }
    throw new ApiError(response.status, body)
  }
  return payload as T
}

// ----------------------------------------------------------------- types

export interface NoteSummary {
  note_id: string
  title: string
  revision: string
  modified_at: string
}

export interface Note extends NoteSummary {
  text: string
}

export interface DocumentRecord {
  document_id: string
  sha256: string
  original_filename: string
  display_title: string
  title: string | null
  authors: string | null
  year: number | null
  page_count: number | null
  byte_size: number
  imported_at: string
  searchable: boolean
  extraction: {
    status: 'pending' | 'running' | 'ok' | 'failed'
    quality: 'ok' | 'partial' | 'none' | null
    warnings: string[]
    empty_pages?: number[]
    error: string | null
    kind?: string
    encrypted?: boolean
  }
  summary: {
    status: 'none' | 'queued' | 'ready' | 'failed' | 'unavailable'
    error: string | null
    generated_at: string | null
  }
  summary_text?: string | null
  summary_revision?: string
}

export interface AgentStatus {
  backend: string
  installed: boolean
  authenticated: boolean
  ready: boolean
  message: string
  detail: string | null
}

export interface ProjectState {
  project: { name: string; root: string; schema_version: number }
  agent: AgentStatus
  documents: DocumentRecord[]
  notes: NoteSummary[]
  conversations: ConversationSummary[]
  latest_seq: number
}

export interface PageRecord {
  page_number: number
  page_label: string | null
  width: number
  height: number
  rotation: number
  warnings: string[]
  text: string
}

export interface ReferenceRecord {
  reference_id: string
  document_id: string
  document_sha256: string
  page_number: number
  page_label?: string | null
  status: 'resolved' | 'ambiguous' | 'not_found' | 'page_only' | 'missing_source'
  quote?: string | null
  rects?: number[][]
  coordinate_space: string
  note?: string | null
  document_title?: string
  document_sha256_matches?: boolean
  missing_source?: boolean
  message?: string
}

export interface SearchHit {
  document_id: string
  document_title: string
  page_number: number
  page_label: string | null
  snippet: string
  quote: string
  score: number
}

export interface ConversationSummary {
  conversation_id: string
  title: string
  updated_at: string
}

export interface ChatMessage {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  run_id: string | null
  context: Record<string, unknown> | null
  created_at: string
}

export interface RunRecord {
  run_id: string
  status: string
  kind: string
  error: string | null
  started_at: string
  ended_at: string | null
}

export interface Conversation {
  conversation_id: string
  title: string
  backend_session_id: string | null
  messages: ChatMessage[]
  runs: RunRecord[]
}

export interface ChangeRecord {
  change_id: string
  target_kind: string
  target_id: string
  origin: string
  summary: string
  base_revision: string | null
  new_revision: string | null
  undoable: boolean
  undone_at: string | null
  created_at: string
}

export interface MessageContext {
  note_id?: string | null
  note_revision?: string | null
  selection?: string | null
  document_ids?: string[]
  reference_id?: string | null
  page_number?: number | null
}

// ------------------------------------------------------------------- api

export const api = {
  state: () => request<ProjectState>('/state'),

  listNotes: () => request<{ notes: NoteSummary[] }>('/notes'),
  readNote: (id: string) => request<Note>(`/notes/${encodeURIComponent(id)}`),
  createNote: (title: string, text?: string) =>
    request<Note>('/notes', { method: 'POST', body: JSON.stringify({ title, text }) }),
  saveNote: (id: string, text: string, expected_revision: string | null) =>
    request<Note>(`/notes/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify({ text, expected_revision }),
    }),
  deleteNote: (id: string, revision: string) =>
    request<void>(
      `/notes/${encodeURIComponent(id)}?expected_revision=${encodeURIComponent(revision)}`,
      { method: 'DELETE' },
    ),

  listDocuments: () => request<{ documents: DocumentRecord[] }>('/documents'),
  getDocument: (id: string) => request<DocumentRecord>(`/documents/${encodeURIComponent(id)}`),
  uploadDocuments: (files: File[]) => {
    const form = new FormData()
    for (const file of files) form.append('files', file, file.name)
    return request<{
      imported: { document: DocumentRecord; duplicate: boolean }[]
      errors: { filename: string; message: string }[]
    }>('/documents', { method: 'POST', body: form })
  },
  pdfUrl: (id: string) =>
    `/api/documents/${encodeURIComponent(id)}/pdf?token=${encodeURIComponent(SESSION_TOKEN)}`,
  pages: (id: string) =>
    request<{
      document_id: string
      page_count: number
      quality: string
      warnings: string[]
      pages: PageRecord[]
    }>(`/documents/${encodeURIComponent(id)}/pages`),
  summary: (id: string) =>
    request<{
      document_id: string
      text: string | null
      revision: string | null
      status: string
      error: string | null
      extraction_quality: string | null
      extraction_warnings: string[]
    }>(`/documents/${encodeURIComponent(id)}/summary`),
  saveSummary: (id: string, text: string, expected_revision: string | null) =>
    request<{ document_id: string; revision: string }>(
      `/documents/${encodeURIComponent(id)}/summary`,
      { method: 'PUT', body: JSON.stringify({ text, expected_revision }) },
    ),
  reextract: (id: string) =>
    request<{ job_id: string }>(`/documents/${encodeURIComponent(id)}/extract`, {
      method: 'POST',
    }),
  summarize: (id: string) =>
    request<{ queued: boolean; run_id?: string; reason?: string }>(
      `/documents/${encodeURIComponent(id)}/summarize`,
      { method: 'POST' },
    ),
  deleteDocument: (id: string) =>
    request<{ deleted: string; references_marked_missing: string[] }>(
      `/documents/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),

  search: (query: string) =>
    request<{ query: string; passages: SearchHit[]; notes: { note_id: string; title: string; snippet: string }[] }>(
      `/search?q=${encodeURIComponent(query)}`,
    ),

  listReferences: () => request<{ references: ReferenceRecord[] }>('/references'),
  getReference: (id: string) => request<ReferenceRecord>(`/references/${encodeURIComponent(id)}`),
  createReference: (payload: {
    document_id: string
    page_number: number
    quote?: string
    prefix?: string
    suffix?: string
    start?: number
    end?: number
    allow_page_only?: boolean
  }) =>
    request<ReferenceRecord & { markdown_link: string; message: string | null }>('/references', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  conversation: (id: string) => request<Conversation>(`/conversations/${encodeURIComponent(id)}`),
  createConversation: (title = 'Conversation') =>
    request<Conversation>('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),
  sendMessage: (id: string, prompt: string, context: MessageContext) =>
    request<{ run_id: string; conversation_id: string; status: string }>(
      `/conversations/${encodeURIComponent(id)}/messages`,
      { method: 'POST', body: JSON.stringify({ prompt, context }) },
    ),
  cancelRun: (runId: string) =>
    request<{ run_id: string; status: string }>(`/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
    }),
  exportUrl: (id: string, format: 'markdown' | 'json') =>
    `/api/conversations/${encodeURIComponent(id)}/export?format=${format}&token=${encodeURIComponent(SESSION_TOKEN)}`,

  changes: () => request<{ changes: ChangeRecord[] }>('/changes'),
  undo: (changeId: string, expected_revision?: string) =>
    request<{ change_id: string; revision: string; undone: boolean }>(
      `/changes/${encodeURIComponent(changeId)}/undo`,
      { method: 'POST', body: JSON.stringify({ expected_revision: expected_revision ?? null }) },
    ),
}
