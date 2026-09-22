/**
 * The application shell: project sidebar, main editor, source panel, chat drawer.
 *
 * Each of the three side regions can collapse, and the chat drawer is capped so
 * it cannot squeeze the editor and the PDF into unreadable columns. On a narrow
 * window the source panel takes over the main view, with an explicit way back.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'

import {
  ApiError,
  api,
  type ChangeRecord,
  type Conversation,
  type DocumentRecord,
  type MessageContext,
  type NoteSummary,
  type ProjectState,
  type ReferenceRecord,
  type SearchHit,
} from './api'
import { Chat } from './chat/Chat'
import { MarkdownEditor } from './editor/MarkdownEditor'
import { EventStream, type ServerEvent } from './events'
import { PdfViewer, type PdfSelection } from './pdf/PdfViewer'
import { Sidebar } from './Sidebar'

type MainTarget =
  | { kind: 'note'; noteId: string }
  | { kind: 'summary'; documentId: string }
  | { kind: 'empty' }

interface Buffer {
  text: string
  revision: string | null
}

const NARROW_QUERY = '(max-width: 1100px)'

export function App() {
  const [state, setState] = useState<ProjectState | null>(null)
  const [fatal, setFatal] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState<ServerEvent[]>([])

  const [target, setTarget] = useState<MainTarget>({ kind: 'empty' })
  const [buffer, setBuffer] = useState<Buffer | null>(null)
  const [dirty, setDirty] = useState(false)
  const [selection, setSelection] = useState('')

  const [openDocument, setOpenDocument] = useState<DocumentRecord | null>(null)
  const [highlight, setHighlight] = useState<ReferenceRecord | null>(null)
  const [gotoPage, setGotoPage] = useState<number | null>(null)
  const [pdfSelection, setPdfSelection] = useState<PdfSelection | null>(null)
  const [insertNotice, setInsertNotice] = useState<string | null>(null)

  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [context, setContext] = useState<MessageContext>({})
  const [changes, setChanges] = useState<ChangeRecord[]>([])

  const [showSidebar, setShowSidebar] = useState(true)
  const [showChat, setShowChat] = useState(true)
  const [showSource, setShowSourcePanel] = useState(true)
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW_QUERY).matches)
  const [sourceTakesOver, setSourceTakesOver] = useState(false)

  const insertRef = useRef<((markdown: string) => void) | null>(null)
  const stream = useRef<EventStream | null>(null)

  // -------------------------------------------------------------- loading

  const refreshState = useCallback(async (): Promise<ProjectState | null> => {
    try {
      const next = await api.state()
      setState(next)
      return next
    } catch (error) {
      setFatal(error instanceof Error ? error.message : 'The project could not be loaded.')
      return null
    }
  }, [])

  useEffect(() => {
    void (async () => {
      const next = await refreshState()
      if (!next) return

      const conversationId =
        next.conversations[0]?.conversation_id ??
        (await api.createConversation('Research chat')).conversation_id
      setConversation(await api.conversation(conversationId))

      if (next.notes.length > 0) setTarget({ kind: 'note', noteId: next.notes[0]!.note_id })

      const source = new EventStream(setConnected)
      source.subscribe((event) => setEvents((current) => [...current, event]))
      source.start(next.latest_seq)
      stream.current = source
    })()

    const media = window.matchMedia(NARROW_QUERY)
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches)
    media.addEventListener('change', onChange)
    return () => {
      media.removeEventListener('change', onChange)
      stream.current?.close()
    }
  }, [refreshState])

  // Load the buffer for whatever the main area is showing.
  useEffect(() => {
    void (async () => {
      if (target.kind === 'note') {
        const note = await api.readNote(target.noteId)
        setBuffer({ text: note.text, revision: note.revision })
      } else if (target.kind === 'summary') {
        const summary = await api.summary(target.documentId)
        setBuffer({
          text: summary.text ?? '',
          revision: summary.revision,
        })
      } else {
        setBuffer(null)
      }
    })()
  }, [target])

  // Keep the note context chip in step with what is being edited.
  useEffect(() => {
    setContext((current) => ({
      ...current,
      note_id: target.kind === 'note' ? target.noteId : null,
      note_revision: target.kind === 'note' ? (buffer?.revision ?? null) : null,
    }))
  }, [target, buffer?.revision])

  useEffect(() => {
    setContext((current) => ({ ...current, selection: selection || null }))
  }, [selection])

  // React to server events that change files or documents under us.
  useEffect(() => {
    if (events.length === 0) return
    const latest = events[events.length - 1]!
    if (latest.type === 'document_updated' || latest.type === 'job_updated') {
      void refreshState()
    }
    if (latest.type === 'file_changed') {
      void refreshState()
      void api.changes().then((payload) => setChanges(payload.changes))
      // A clean buffer follows the file; a dirty one is left alone and the
      // editor surfaces a conflict if the user then saves.
      if (!dirty) void reloadBuffer()
    }
  }, [events])

  const reloadBuffer = useCallback(async (): Promise<void> => {
    if (target.kind === 'note') {
      const note = await api.readNote(target.noteId)
      setBuffer({ text: note.text, revision: note.revision })
    } else if (target.kind === 'summary') {
      const summary = await api.summary(target.documentId)
      setBuffer({ text: summary.text ?? '', revision: summary.revision })
    }
  }, [target])

  const reloadConversation = useCallback(async (): Promise<void> => {
    if (!conversation) return
    setConversation(await api.conversation(conversation.conversation_id))
  }, [conversation?.conversation_id])

  // ------------------------------------------------------------- actions

  const save = useCallback(
    async (text: string, expectedRevision: string | null) => {
      if (target.kind === 'note') {
        const note = await api.saveNote(target.noteId, text, expectedRevision)
        setBuffer({ text: note.text, revision: note.revision })
        setDirty(false)
        void refreshState()
        return { revision: note.revision }
      }
      if (target.kind === 'summary') {
        const result = await api.saveSummary(target.documentId, text, expectedRevision)
        setBuffer({ text, revision: result.revision })
        setDirty(false)
        void refreshState()
        return { revision: result.revision }
      }
      throw new Error('Nothing is open to save.')
    },
    [target, refreshState],
  )

  const openSource = useCallback(
    async (referenceId: string): Promise<void> => {
      try {
        const reference = await api.getReference(referenceId)
        setHighlight(reference)
        setGotoPage(reference.page_number)
        setShowSourcePanel(true)
        if (narrow) setSourceTakesOver(true)
        setContext((current) => ({
          ...current,
          reference_id: reference.reference_id,
          page_number: reference.page_number,
        }))
        if (reference.missing_source) {
          setInsertNotice(
            reference.message ??
              'The cited document is no longer in this project. The reference was kept.',
          )
          return
        }
        const document =
          state?.documents.find((item) => item.document_id === reference.document_id) ??
          (await api.getDocument(reference.document_id))
        setOpenDocument(document)
      } catch (error) {
        setInsertNotice(
          error instanceof ApiError
            ? error.body.message
            : 'That source could not be opened.',
        )
      }
    },
    [state?.documents, narrow],
  )

  const insertReference = useCallback(
    async (withQuote: boolean): Promise<void> => {
      if (!pdfSelection) return
      try {
        const reference = await api.createReference({
          document_id: pdfSelection.documentId,
          page_number: pdfSelection.pageNumber,
          quote: pdfSelection.quote,
          prefix: pdfSelection.prefix,
          suffix: pdfSelection.suffix,
        })
        const snippet = withQuote
          ? `\n\n> ${reference.quote ?? pdfSelection.quote}\n>\n> ${reference.markdown_link}\n`
          : ` ${reference.markdown_link}`
        insertRef.current?.(snippet)
        setHighlight(reference)
        setInsertNotice(reference.message ?? null)
        setPdfSelection(null)
      } catch (error) {
        // An ambiguous or absent passage is reported, never guessed at.
        setInsertNotice(
          error instanceof ApiError
            ? error.body.message
            : 'That passage could not be turned into a reference.',
        )
      }
    },
    [pdfSelection],
  )

  const createNote = useCallback(async (): Promise<void> => {
    const note = await api.createNote('Untitled note')
    await refreshState()
    setTarget({ kind: 'note', noteId: note.note_id })
  }, [refreshState])

  const importFiles = useCallback(
    async (files: File[]): Promise<void> => {
      const pdfs = files.filter(
        (file) => file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'),
      )
      if (pdfs.length === 0) {
        setInsertNotice('Only PDF import is supported in this version.')
        return
      }
      const result = await api.uploadDocuments(pdfs)
      await refreshState()
      const messages: string[] = []
      for (const item of result.imported) {
        if (item.duplicate) {
          messages.push(`${item.document.display_title} was already imported.`)
        }
      }
      for (const failure of result.errors) {
        messages.push(`${failure.filename}: ${failure.message}`)
      }
      setInsertNotice(messages.length > 0 ? messages.join(' ') : null)
      const first = result.imported[0]?.document
      if (first) {
        setOpenDocument(first)
        setShowSourcePanel(true)
      }
    },
    [refreshState],
  )

  const openDocumentById = useCallback(
    (documentId: string): void => {
      const document = state?.documents.find((item) => item.document_id === documentId)
      if (!document) return
      setOpenDocument(document)
      setHighlight(null)
      setShowSourcePanel(true)
      if (narrow) setSourceTakesOver(true)
      setContext((current) => ({
        ...current,
        document_ids: Array.from(new Set([...(current.document_ids ?? []), documentId])),
      }))
    },
    [state?.documents, narrow],
  )

  const jumpToHit = useCallback(
    async (hit: SearchHit): Promise<void> => {
      openDocumentById(hit.document_id)
      setGotoPage(hit.page_number)
      setHighlight(null)
    },
    [openDocumentById],
  )

  const undo = useCallback(
    async (change: ChangeRecord): Promise<void> => {
      try {
        await api.undo(change.change_id)
        await reloadBuffer()
        setChanges((await api.changes()).changes)
      } catch (error) {
        setInsertNotice(
          error instanceof ApiError
            ? `Undo was refused: ${error.body.message}`
            : 'Undo failed.',
        )
      }
    },
    [reloadBuffer],
  )

  // ---------------------------------------------------------------- render

  const docKey = useMemo(() => {
    if (target.kind === 'note') return `note:${target.noteId}`
    if (target.kind === 'summary') return `summary:${target.documentId}`
    return 'empty'
  }, [target])

  if (fatal !== null) {
    return (
      <main class="fatal">
        <h1>Research Workspace</h1>
        <p>{fatal}</p>
      </main>
    )
  }

  if (!state) {
    return (
      <main class="fatal">
        <p>Opening the project…</p>
      </main>
    )
  }

  const notesById = new Map<string, NoteSummary>(
    state.notes.map((note) => [note.note_id, note]),
  )
  const currentTitle =
    target.kind === 'note'
      ? (notesById.get(target.noteId)?.title ?? target.noteId)
      : target.kind === 'summary'
        ? `Summary — ${
            state.documents.find((d) => d.document_id === target.documentId)?.display_title ??
            target.documentId
          }`
        : 'Nothing open'

  const showSourceRegion = showSource && openDocument !== null && (!narrow || sourceTakesOver)
  const showMain = !narrow || !sourceTakesOver

  return (
    <div
      class={[
        'shell',
        showChat ? '' : 'chat-collapsed',
        showSourceRegion ? '' : 'source-hidden',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <header class="topbar">
        <button
          type="button"
          class="ghost"
          onClick={() => setShowSidebar(!showSidebar)}
          title="Toggle the project sidebar"
        >
          ☰
        </button>
        <h1>{state.project.name}</h1>
        <span class="muted path" title={state.project.root}>
          {state.project.root}
        </span>
        <span class="spacer" />
        {!connected && <span class="badge warn">Reconnecting…</span>}
        {!state.agent.ready && (
          <span class="badge" title={state.agent.message}>
            Agent not configured
          </span>
        )}
        <button
          type="button"
          class={showChat ? 'ghost active' : 'ghost'}
          onClick={() => setShowChat(!showChat)}
          title="Toggle the chat drawer"
        >
          Chat
        </button>
      </header>

      {insertNotice !== null && (
        <div class="banner" role="status">
          <span>{insertNotice}</span>
          <button type="button" class="ghost" onClick={() => setInsertNotice(null)}>
            ✕
          </button>
        </div>
      )}

      <div class="body">
        {showSidebar && (
          <Sidebar
            documents={state.documents}
            notes={state.notes}
            changes={changes}
            activeTarget={target}
            onOpenNote={(noteId) => {
              setTarget({ kind: 'note', noteId })
              setSourceTakesOver(false)
            }}
            onOpenSummary={(documentId) => {
              setTarget({ kind: 'summary', documentId })
              setSourceTakesOver(false)
            }}
            onOpenDocument={openDocumentById}
            onNewNote={() => void createNote()}
            onImport={(files) => void importFiles(files)}
            onSearchJump={(hit) => void jumpToHit(hit)}
            onSummarize={async (documentId) => {
              const result = await api.summarize(documentId)
              if (!result.queued) setInsertNotice(result.reason ?? 'No summary was queued.')
            }}
            onReextract={(documentId) => void api.reextract(documentId)}
            onUndo={(change) => void undo(change)}
            onLoadChanges={async () => setChanges((await api.changes()).changes)}
          />
        )}

        {showMain && (
          <main class="main">
            <div class="main-head">
              <h2>{currentTitle}</h2>
              {narrow && openDocument && (
                <button type="button" class="ghost" onClick={() => setSourceTakesOver(true)}>
                  Show source
                </button>
              )}
            </div>
            {buffer && target.kind !== 'empty' ? (
              <MarkdownEditor
                docKey={docKey}
                text={buffer.text}
                revision={buffer.revision}
                save={save}
                onOpenSource={(id) => void openSource(id)}
                onSelectionChange={setSelection}
                onDirtyChange={setDirty}
                registerInsert={(insert) => {
                  insertRef.current = insert
                }}
              />
            ) : (
              <div class="empty">
                <p>Open a note, or drag PDFs onto the sidebar to import them.</p>
                <button type="button" onClick={() => void createNote()}>
                  New note
                </button>
              </div>
            )}
          </main>
        )}

        {showSourceRegion && openDocument && (
          <aside class="source">
            {narrow && (
              <button
                type="button"
                class="ghost back"
                onClick={() => setSourceTakesOver(false)}
              >
                ← Back to the note
              </button>
            )}
            {pdfSelection && (
              <div class="selection-actions">
                <span class="muted">
                  “{pdfSelection.quote.slice(0, 60)}
                  {pdfSelection.quote.length > 60 ? '…' : ''}”
                </span>
                <button type="button" onClick={() => void insertReference(false)}>
                  Insert reference
                </button>
                <button type="button" onClick={() => void insertReference(true)}>
                  Insert quotation and reference
                </button>
              </div>
            )}
            <PdfViewer
              document={openDocument}
              highlight={highlight}
              gotoPage={gotoPage}
              onSelection={setPdfSelection}
              onClose={() => {
                setOpenDocument(null)
                setSourceTakesOver(false)
              }}
            />
          </aside>
        )}

        {showChat && (
          <aside class="chat-drawer">
            <Chat
              conversation={conversation}
              agent={state.agent}
              documents={state.documents}
              context={context}
              events={events}
              onContextChange={setContext}
              onOpenSource={(id) => void openSource(id)}
              onReload={() => {
                void reloadConversation()
                if (!dirty) void reloadBuffer()
              }}
            />
          </aside>
        )}
      </div>
    </div>
  )
}
