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
import { PdfViewer, type PdfMark, type PdfSelection } from './pdf/PdfViewer'
import { Sidebar, UNFILED } from './Sidebar'

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

  // Scope is a browser-session choice, not server state: it narrows what the
  // next message can reach, it does not select a saved conversation.
  const [activeCollectionId, setActiveCollectionId] = useState<string | null>(null)
  // Unfiled is computed client-side, so it is not a scope the server knows.
  const scopedCollection =
    activeCollectionId && activeCollectionId !== UNFILED
      ? (state?.collections ?? []).find((c) => c.collection_id === activeCollectionId) ?? null
      : null

  const [target, setTarget] = useState<MainTarget>({ kind: 'empty' })
  const [buffer, setBuffer] = useState<Buffer | null>(null)
  const [dirty, setDirty] = useState(false)
  const [selection, setSelection] = useState('')

  const [openDocument, setOpenDocument] = useState<DocumentRecord | null>(null)
  const [highlight, setHighlight] = useState<ReferenceRecord | null>(null)
  const [gotoPage, setGotoPage] = useState<number | null>(null)
  const [pdfSelection, setPdfSelection] = useState<PdfSelection | null>(null)
  const [insertNotice, setInsertNotice] = useState<string | null>(null)
  // Every reference in the open document, so the viewer can show all marks
  // and colour them by where they are cited from.
  const [documentReferences, setDocumentReferences] = useState<ReferenceRecord[]>([])
  const [gotoNonce, setGotoNonce] = useState(0)

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
    if (
      latest.type === 'document_updated' ||
      latest.type === 'job_updated' ||
      latest.type === 'collection_updated'
    ) {
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

  const reloadReferences = useCallback(async (): Promise<void> => {
    if (!openDocument) {
      setDocumentReferences([])
      return
    }
    try {
      const payload = await api.listReferences(openDocument.document_id)
      setDocumentReferences(payload.references)
    } catch {
      setDocumentReferences([])
    }
  }, [openDocument?.document_id])

  useEffect(() => {
    void reloadReferences()
  }, [reloadReferences])

  // Citing notes change as the user types, so refresh when the buffer is saved.
  useEffect(() => {
    if (buffer?.revision) void reloadReferences()
  }, [buffer?.revision, reloadReferences])

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
        setGotoNonce((value) => value + 1)
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
      setGotoPage(1)
      setGotoNonce((value) => value + 1)
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
      setGotoNonce((value) => value + 1)
      setHighlight(null)
    },
    [openDocumentById],
  )

  const writeBibliography = useCallback(async (): Promise<void> => {
    try {
      const result = await api.writeBibliography()
      const count = `${result.entries} ${result.entries === 1 ? 'entry' : 'entries'}`
      const headline = result.written
        ? `Wrote ${result.path} — ${count}.`
        : `${result.path} was already up to date — ${count}.`
      // Incomplete PDF metadata is reported rather than filled in, so the
      // warnings travel with the result instead of being hidden.
      setInsertNotice([headline, ...result.warnings].join(' '))
    } catch (error) {
      setInsertNotice(
        error instanceof ApiError
          ? error.body.message
          : 'The bibliography could not be written.',
      )
    }
  }, [])

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

  const marks = useMemo<PdfMark[]>(() => {
    const currentNoteId = target.kind === 'note' ? target.noteId : null
    return documentReferences
      .filter((reference) => (reference.rects?.length ?? 0) > 0)
      .map((reference) => {
        const citedBy = reference.cited_by ?? []
        const inCurrentNote = citedBy.some(
          (citation) => citation.kind === 'note' && citation.id === currentNoteId,
        )
        const tone: PdfMark['tone'] =
          reference.reference_id === highlight?.reference_id
            ? 'active'
            : inCurrentNote
              ? 'current-note'
              : citedBy.length > 0
                ? 'other-note'
                : 'uncited'
        const where =
          citedBy.length === 0
            ? 'not cited in any note yet'
            : citedBy.map((citation) => citation.title).join(', ')
        const quote = reference.quote ? `“${reference.quote.slice(0, 90)}” — ` : ''
        return {
          referenceId: reference.reference_id,
          pageNumber: reference.page_number,
          rects: reference.rects ?? [],
          tone,
          label: `${quote}${where} (${reference.reference_id})`,
        }
      })
  }, [documentReferences, target, highlight?.reference_id])

  const docKey = useMemo(() => {
    if (target.kind === 'note') return `note:${target.noteId}`
    if (target.kind === 'summary') return `summary:${target.documentId}`
    return 'empty'
  }, [target])

  if (fatal !== null) {
    return (
      <main class="fatal">
        <h1>Litmark</h1>
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
            collections={state.collections ?? []}
            unfiled={state.unfiled ?? []}
            activeCollectionId={activeCollectionId}
            onSelectCollection={setActiveCollectionId}
            onCreateCollection={async (kind, name, parentId) => {
              await api.createCollection(kind, name, [], parentId ?? null)
              await refreshState()
            }}
            onMoveCollection={async (collectionId, parentId) => {
              await api.moveCollection(collectionId, parentId)
              await refreshState()
            }}
            onRenameCollection={async (collectionId, name) => {
              await api.renameCollection(collectionId, name)
              await refreshState()
            }}
            onDeleteCollection={async (collectionId) => {
              await api.deleteCollection(collectionId)
              if (activeCollectionId === collectionId) setActiveCollectionId(null)
              await refreshState()
            }}
            onAssign={async (collectionId, documentId, member) => {
              if (member) await api.addToCollection(collectionId, [documentId])
              else await api.removeFromCollection(collectionId, documentId)
              await refreshState()
            }}
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
            onWriteBibliography={writeBibliography}
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
              marks={marks}
              gotoPage={gotoPage}
              gotoNonce={gotoNonce}
              onSelection={setPdfSelection}
              onActivateMark={(referenceId) => {
                const reference = documentReferences.find(
                  (item) => item.reference_id === referenceId,
                )
                if (reference) setHighlight(reference)
              }}
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
              context={{ ...context, collection_id: scopedCollection?.collection_id ?? null }}
              events={events}
              scopeName={scopedCollection?.name ?? null}
              scopeCount={scopedCollection?.document_count ?? 0}
              scopeDocumentIds={scopedCollection?.documents ?? null}
              onClearScope={() => setActiveCollectionId(null)}
              onContextChange={setContext}
              onOpenSource={(id) => void openSource(id)}
              onOpenDocument={openDocumentById}
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
