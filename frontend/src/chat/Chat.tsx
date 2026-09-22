/**
 * The chat drawer.
 *
 * Shows the persistent conversation, context chips the user can change,
 * streaming replies, a stop button, and technical activity kept behind an
 * expandable detail view so the interface stays about the research.
 */

import { useEffect, useMemo, useRef, useState } from 'preact/hooks'

import {
  api,
  type AgentStatus,
  type ChatMessage,
  type Conversation,
  type DocumentRecord,
  type MessageContext,
  type RunRecord,
} from '../api'
import type { ServerEvent } from '../events'
import { renderMarkdown } from '../markdown'

export interface ToolActivity {
  id: string
  tool: string
  input?: Record<string, unknown>
  result?: Record<string, unknown>
  isError?: boolean
  finished: boolean
}

export interface ChatProps {
  conversation: Conversation | null
  agent: AgentStatus
  documents: DocumentRecord[]
  context: MessageContext
  events: ServerEvent[]
  onContextChange: (context: MessageContext) => void
  onOpenSource: (referenceId: string) => void
  onReload: () => void
}

export function Chat(props: ChatProps) {
  const [draft, setDraft] = useState('')
  const [streaming, setStreaming] = useState('')
  const [activity, setActivity] = useState<ToolActivity[]>([])
  const [activeRun, setActiveRun] = useState<string | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [notices, setNotices] = useState<string[]>([])
  const [sending, setSending] = useState(false)
  const scroller = useRef<HTMLDivElement | null>(null)
  const consumed = useRef(0)

  // Fold newly arrived events into the live view. Events carry durable
  // sequence IDs and are de-duplicated upstream, so replay after a reconnect
  // cannot double up a message or an edit notice.
  useEffect(() => {
    const fresh = props.events.slice(consumed.current)
    consumed.current = props.events.length
    if (fresh.length === 0) return

    let reload = false
    for (const event of fresh) {
      switch (event.type) {
        case 'run_started':
          setActiveRun(event.run_id)
          setStreaming('')
          setActivity([])
          setRunError(null)
          break
        case 'text_delta': {
          const text = String(event.text ?? '')
          if (event.final) {
            // The final block replaces the partial buffer rather than appending.
            setStreaming(text)
          } else {
            setStreaming((current) => current + text)
          }
          break
        }
        case 'tool_started':
          setActivity((current) => [
            ...current,
            {
              id: String(event.tool_use_id ?? `${event.seq}`),
              tool: String(event.tool ?? 'tool'),
              input: (event.input as Record<string, unknown>) ?? undefined,
              finished: false,
            },
          ])
          break
        case 'tool_finished':
          setActivity((current) =>
            current.map((item) =>
              item.id === String(event.tool_use_id ?? '') || (!item.finished && !event.tool_use_id)
                ? {
                    ...item,
                    finished: true,
                    isError: Boolean(event.is_error),
                    result: (event.result as Record<string, unknown>) ?? undefined,
                  }
                : item,
            ),
          )
          break
        case 'file_changed':
          setNotices((current) => [
            ...current,
            `Edited ${event.target_kind === 'summary' ? 'summary of' : 'note'} ${String(
              event.target_id,
            )}${event.summary ? ` — ${String(event.summary)}` : ''}`,
          ])
          reload = true
          break
        case 'message_added':
          if (event.role === 'assistant') {
            setStreaming('')
            reload = true
          }
          if (event.role === 'user') reload = true
          break
        case 'error':
          setRunError(String(event.message ?? 'The run failed.'))
          break
        case 'done':
          setActiveRun(null)
          if (event.terminal_reason === 'interrupted') {
            setRunError(
              String(
                event.message ??
                  'This run was interrupted. The conversation was kept; the reply was not finished.',
              ),
            )
          }
          if (event.terminal_reason === 'cancelled') {
            setNotices((current) => [
              ...current,
              'Stopped. Edits already committed are kept and listed above.',
            ])
          }
          reload = true
          break
        default:
          break
      }
    }
    if (reload) props.onReload()
  }, [props.events])

  useEffect(() => {
    const node = scroller.current
    if (node) node.scrollTop = node.scrollHeight
  }, [props.conversation?.messages.length, streaming, activity.length])

  const send = async (): Promise<void> => {
    const prompt = draft.trim()
    if (!prompt || !props.conversation || sending) return
    setSending(true)
    setRunError(null)
    setNotices([])
    try {
      const result = await api.sendMessage(
        props.conversation.conversation_id,
        prompt,
        props.context,
      )
      setActiveRun(result.run_id)
      setDraft('')
    } catch (error) {
      setRunError(error instanceof Error ? error.message : 'The message could not be sent.')
    } finally {
      setSending(false)
    }
  }

  const stop = async (): Promise<void> => {
    if (!activeRun) return
    try {
      await api.cancelRun(activeRun)
    } catch (error) {
      setRunError(error instanceof Error ? error.message : 'Stopping failed.')
    }
  }

  const interrupted = useMemo(
    () => (props.conversation?.runs ?? []).filter((run) => run.status === 'interrupted'),
    [props.conversation?.runs],
  )

  return (
    <section class="chat" aria-label="Chat">
      <header class="chat-head">
        <strong>Chat</strong>
        {props.conversation && (
          <a
            class="link"
            href={api.exportUrl(props.conversation.conversation_id, 'markdown')}
            download
          >
            Export
          </a>
        )}
      </header>

      {!props.agent.ready && (
        <div class="notice" role="status">
          <strong>No agent configured.</strong>
          <p>{props.agent.message}</p>
          <p class="hint">
            Notes and PDFs work without one. Summaries and chat need a configured
            provider.
          </p>
        </div>
      )}

      {interrupted.length > 0 && (
        <div class="notice" role="status">
          {interrupted.length === 1 ? 'A run was' : `${interrupted.length} runs were`} interrupted
          by a server restart. The transcript was kept.
        </div>
      )}

      <div class="chat-log" ref={scroller}>
        {(props.conversation?.messages ?? []).map((message) => (
          <Bubble key={message.message_id} message={message} onOpenSource={props.onOpenSource} />
        ))}

        {activity.length > 0 && (
          <details class="activity" open={activeRun !== null}>
            <summary>
              {activeRun ? 'Working' : 'Activity'} · {activity.length} tool
              {activity.length === 1 ? '' : 's'}
            </summary>
            <ul>
              {activity.map((item) => (
                <li key={item.id} class={item.isError ? 'tool error' : 'tool'}>
                  <code>{item.tool}</code>
                  <span class="muted">
                    {item.finished ? (item.isError ? 'failed' : 'done') : 'running…'}
                  </span>
                  {item.input && (
                    <pre>{JSON.stringify(item.input, null, 2).slice(0, 600)}</pre>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}

        {streaming && (
          <div class="bubble assistant streaming">
            <div
              class="prose"
              // eslint-disable-next-line react/no-danger
              dangerouslySetInnerHTML={{ __html: renderMarkdown(streaming) }}
            />
          </div>
        )}

        {notices.map((notice, index) => (
          <p key={index} class="chat-notice">
            {notice}
          </p>
        ))}

        {runError !== null && (
          <p class="chat-error" role="alert">
            {runError}
          </p>
        )}
      </div>

      <ContextChips
        context={props.context}
        documents={props.documents}
        onChange={props.onContextChange}
      />

      <form
        class="chat-compose"
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <textarea
          value={draft}
          placeholder={
            props.agent.ready
              ? 'Ask about the papers, or ask for a note to be written…'
              : 'Configure an agent to use chat'
          }
          disabled={!props.agent.ready}
          rows={3}
          onInput={(event) => setDraft((event.target as HTMLTextAreaElement).value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              void send()
            }
          }}
        />
        <div class="chat-buttons">
          {activeRun !== null ? (
            <button type="button" class="stop" onClick={() => void stop()}>
              Stop
            </button>
          ) : (
            <button type="submit" disabled={!props.agent.ready || draft.trim() === '' || sending}>
              Send
            </button>
          )}
        </div>
      </form>
    </section>
  )
}

function Bubble({
  message,
  onOpenSource,
}: {
  message: ChatMessage
  onOpenSource: (id: string) => void
}) {
  const html = useMemo(() => renderMarkdown(message.content), [message.content])
  return (
    <div class={`bubble ${message.role}`}>
      <div
        class="prose"
        onClick={(event) => {
          const target = event.target as HTMLElement
          const id = target.dataset?.sourceRef
          if (id) {
            event.preventDefault()
            onOpenSource(id)
          }
        }}
        // Markdown is sanitized in renderMarkdown before it reaches the DOM.
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  )
}

function ContextChips({
  context,
  documents,
  onChange,
}: {
  context: MessageContext
  documents: DocumentRecord[]
  onChange: (context: MessageContext) => void
}) {
  const attached = context.document_ids ?? []
  const toggle = (id: string): void => {
    onChange({
      ...context,
      document_ids: attached.includes(id)
        ? attached.filter((item) => item !== id)
        : [...attached, id],
    })
  }
  return (
    <div class="chips" aria-label="Message context">
      {context.note_id && <span class="chip">note: {context.note_id}</span>}
      {context.selection && (
        <span class="chip" title={context.selection}>
          selection: {context.selection.slice(0, 24)}
          {context.selection.length > 24 ? '…' : ''}
          <button type="button" onClick={() => onChange({ ...context, selection: null })}>
            ✕
          </button>
        </span>
      )}
      {context.reference_id && <span class="chip">source: {context.reference_id}</span>}
      {documents.map((document) => (
        <button
          key={document.document_id}
          type="button"
          class={attached.includes(document.document_id) ? 'chip toggle on' : 'chip toggle'}
          onClick={() => toggle(document.document_id)}
          title={
            attached.includes(document.document_id)
              ? 'Attached as starting context'
              : 'Attach as starting context'
          }
        >
          {document.display_title.slice(0, 28)}
        </button>
      ))}
    </div>
  )
}

export function pendingRunOf(runs: RunRecord[]): string | null {
  const running = runs.find((run) => run.status === 'running' || run.status === 'queued')
  return running ? running.run_id : null
}
