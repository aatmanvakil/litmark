/**
 * Project sidebar: Documents and Notes, search, import, and a change log.
 *
 * Each document shows its title, a summary preview, and its ingestion status.
 * Extraction and summary state are reported separately, so a summary that
 * failed never makes a readable PDF look unavailable.
 */

import { useEffect, useState } from 'preact/hooks'

import {
  api,
  type ChangeRecord,
  type Collection,
  type CollectionKind,
  type DocumentRecord,
  type NoteSummary,
  type SearchHit,
} from './api'

import { CollectionTree } from './CollectionTree'
import { ancestorsOf, buildTree, flatten, rollupDocuments } from './collections'

export const UNFILED = '__unfiled__'

export interface SidebarProps {
  documents: DocumentRecord[]
  notes: NoteSummary[]
  changes: ChangeRecord[]
  collections: Collection[]
  unfiled: string[]
  activeCollectionId: string | null
  activeTarget: { kind: string; noteId?: string; documentId?: string }
  onOpenNote: (noteId: string) => void
  onOpenSummary: (documentId: string) => void
  onOpenDocument: (documentId: string) => void
  onNewNote: () => void
  onImport: (files: File[]) => void
  onSearchJump: (hit: SearchHit) => void
  onSummarize: (documentId: string) => Promise<void>
  onReextract: (documentId: string) => void
  onWriteBibliography: () => Promise<void>
  onUndo: (change: ChangeRecord) => void
  onLoadChanges: () => Promise<void>
  onSelectCollection: (collectionId: string | null) => void
  onCreateCollection: (
    kind: CollectionKind,
    name: string,
    parentId?: string | null,
  ) => Promise<void>
  onMoveCollection: (collectionId: string, parentId: string | null) => Promise<void>
  onRenameCollection: (collectionId: string, name: string) => Promise<void>
  onDeleteCollection: (collectionId: string) => Promise<void>
  onAssign: (collectionId: string, documentId: string, member: boolean) => Promise<void>
}

export function Sidebar(props: SidebarProps) {
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [noteHits, setNoteHits] = useState<{ note_id: string; title: string; snippet: string }[]>(
    [],
  )
  const [dragging, setDragging] = useState(false)
  const [searching, setSearching] = useState(false)
  const [assigning, setAssigning] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())
  const [assignFilter, setAssignFilter] = useState('')

  const tree = buildTree(props.collections)
  const nodesById = new Map(
    flatten(tree).map((node) => [node.collection.collection_id, node]),
  )

  // Ancestors of the selection are in scope without being selected, because a
  // parent's scope includes everything under it.
  const inScopeIds = new Set(
    props.activeCollectionId && props.activeCollectionId !== UNFILED
      ? ancestorsOf(props.collections, props.activeCollectionId)
      : [],
  )

  // Reveal a selection made elsewhere — the agent can change scope too.
  useEffect(() => {
    if (!props.activeCollectionId || props.activeCollectionId === UNFILED) return
    const reveal = ancestorsOf(props.collections, props.activeCollectionId)
    if (reveal.length === 0) return
    setCollapsed((current) => {
      if (!reveal.some((id) => current.has(id))) return current
      const next = new Set(current)
      for (const id of reveal) next.delete(id)
      return next
    })
  }, [props.activeCollectionId, props.collections])

  const active = props.collections.find(
    (collection) => collection.collection_id === props.activeCollectionId,
  )
  const scopeLabel = props.activeCollectionId === UNFILED ? 'Unfiled' : active?.name
  // A collection filters the list; "Unfiled" is computed from membership.
  const visible =
    props.activeCollectionId === null
      ? props.documents
      : props.activeCollectionId === UNFILED
        ? props.documents.filter((d) => props.unfiled.includes(d.document_id))
        : (() => {
            // The subtree, so the list matches what a scoped chat can read.
            const node = nodesById.get(props.activeCollectionId)
            const members = node ? rollupDocuments(node) : new Set<string>()
            return props.documents.filter((d) => members.has(d.document_id))
          })()

  const runSearch = async (value: string): Promise<void> => {
    setQuery(value)
    if (value.trim().length < 2) {
      setHits(null)
      setNoteHits([])
      return
    }
    setSearching(true)
    try {
      const serverScope =
        props.activeCollectionId === UNFILED ? null : props.activeCollectionId
      const result = await api.search(value, serverScope)
      setHits(result.passages)
      setNoteHits(result.notes)
    } finally {
      setSearching(false)
    }
  }

  return (
    <nav
      class={dragging ? 'sidebar dropping' : 'sidebar'}
      onDragOver={(event) => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault()
        setDragging(false)
        const files = Array.from(event.dataTransfer?.files ?? [])
        if (files.length > 0) props.onImport(files)
      }}
    >
      <div class="sidebar-tools">
        <input
          type="search"
          placeholder="Search papers and notes"
          value={query}
          onInput={(event) => void runSearch((event.target as HTMLInputElement).value)}
          aria-label="Search"
        />
        <label class="import" title="Import one or more PDFs">
          Import
          <input
            type="file"
            accept="application/pdf"
            multiple
            onChange={(event) => {
              const input = event.target as HTMLInputElement
              const files = Array.from(input.files ?? [])
              if (files.length > 0) props.onImport(files)
              input.value = ''
            }}
          />
        </label>
      </div>

      {hits !== null && (
        <section class="results">
          <h3>
            Results {searching && <span class="muted">searching…</span>}
          </h3>
          {hits.length === 0 && noteHits.length === 0 && (
            <p class="muted">Nothing matched.</p>
          )}
          {noteHits.map((hit) => (
            <button
              key={hit.note_id}
              type="button"
              class="result"
              onClick={() => props.onOpenNote(hit.note_id)}
            >
              <strong>{hit.title}</strong>
              <span class="muted">{hit.snippet}</span>
            </button>
          ))}
          {hits.map((hit, index) => (
            <button
              key={`${hit.document_id}-${hit.page_number}-${index}`}
              type="button"
              class="result"
              onClick={() => props.onSearchJump(hit)}
            >
              <strong>
                {hit.document_title} · p. {hit.page_label ?? hit.page_number}
              </strong>
              <span class="muted">{hit.snippet}</span>
            </button>
          ))}
        </section>
      )}

      <section class="collections">
        <div class="section-head">
          <h3>Collections</h3>
          <span class="head-actions">
            <button
              type="button"
              class="link"
              onClick={() => {
                const name = window.prompt('Name this project')
                if (name?.trim()) void props.onCreateCollection('project', name.trim())
              }}
            >
              + Project
            </button>
            <button
              type="button"
              class="link"
              onClick={() => {
                const name = window.prompt('Name this topic')
                if (name?.trim()) void props.onCreateCollection('topic', name.trim())
              }}
            >
              + Topic
            </button>
          </span>
        </div>
        {props.activeCollectionId !== null && (
          <button type="button" class="link scope-clear" onClick={() => props.onSelectCollection(null)}>
            Show all papers
          </button>
        )}
        {(['project', 'topic'] as CollectionKind[]).map((kind) => {
          // Only roots are grouped by kind; children inherit their root's.
          const group = tree.filter((node) => node.collection.kind === kind)
          if (group.length === 0) return null
          return (
            <div key={kind} class="collection-group">
              <h4 class="muted">{kind === 'project' ? 'Projects' : 'Topics'}</h4>
              <CollectionTree
                nodes={group}
                activeCollectionId={props.activeCollectionId}
                inScopeIds={inScopeIds}
                collapsed={collapsed}
                onToggleExpanded={(id) =>
                  setCollapsed((current) => {
                    const next = new Set(current)
                    if (next.has(id)) next.delete(id)
                    else next.add(id)
                    return next
                  })
                }
                onSelect={(id) =>
                  props.onSelectCollection(props.activeCollectionId === id ? null : id)
                }
                onAddChild={(parent) => {
                  const name = window.prompt(`Name the subtopic under "${parent.name}"`)
                  if (name?.trim())
                    void props.onCreateCollection('topic', name.trim(), parent.collection_id)
                }}
                onRename={(collection) => {
                  const name = window.prompt('Rename', collection.name)
                  if (name?.trim())
                    void props.onRenameCollection(collection.collection_id, name.trim())
                }}
                onMove={(collection) => {
                  const choices = props.collections.filter(
                    (other) => other.collection_id !== collection.collection_id,
                  )
                  const menu = choices
                    .map((other, index) => `${index + 1}. ${other.path ?? other.name}`)
                    .join('\n')
                  const answer = window.prompt(
                    `Move "${collection.name}" under which collection?\n\n` +
                      `${menu}\n\nEnter a number, or 0 for the top level.`,
                  )
                  if (answer === null) return
                  const index = Number.parseInt(answer, 10)
                  if (Number.isNaN(index)) return
                  const parent = index === 0 ? null : choices[index - 1]?.collection_id ?? null
                  void props.onMoveCollection(collection.collection_id, parent)
                }}
                onDelete={(collection) => {
                  const node = nodesById.get(collection.collection_id)
                  const children = node?.children.length ?? 0
                  const ok = window.confirm(
                    `Remove the ${collection.kind} "${collection.name}"?\n\n` +
                      'The papers in it are not deleted.' +
                      (children > 0
                        ? `\n\nIts ${children} subcollection${children === 1 ? '' : 's'} ` +
                          'will move up one level.'
                        : ''),
                  )
                  if (ok) void props.onDeleteCollection(collection.collection_id)
                }}
              />
            </div>
          )
        })}
        {props.unfiled.length > 0 && (
          <ul class="list">
            <li>
              <button
                type="button"
                class={props.activeCollectionId === UNFILED ? 'entry active' : 'entry'}
                onClick={() =>
                  props.onSelectCollection(
                    props.activeCollectionId === UNFILED ? null : UNFILED,
                  )
                }
              >
                <strong>Unfiled</strong>
                <span class="muted">{props.unfiled.length} papers</span>
              </button>
            </li>
          </ul>
        )}
        {props.collections.length === 0 && (
          <p class="muted">No projects or topics yet.</p>
        )}
      </section>

      <section>
        <div class="section-head">
          <h3>{scopeLabel ? `Documents · ${scopeLabel} (${visible.length})` : 'Documents'}</h3>
          <span class="head-actions">
            <button
              type="button"
              class="link"
              onClick={() => void props.onWriteBibliography()}
              title="Write references.bib into the project directory"
            >
              Write .bib
            </button>
            <a
              class="link"
              href={api.bibliographyUrl()}
              download="references.bib"
              title="Download a BibTeX file for these documents"
            >
              Download
            </a>
          </span>
        </div>
        {props.documents.length === 0 && (
          <p class="muted">
            No papers yet. Drag PDFs here, or use Import.
          </p>
        )}
        {props.documents.length > 0 && visible.length === 0 && (
          <p class="muted">No papers in this collection yet.</p>
        )}
        <ul class="list">
          {visible.map((document) => (
            <li key={document.document_id}>
              <button
                type="button"
                class={
                  props.activeTarget.documentId === document.document_id
                    ? 'entry active'
                    : 'entry'
                }
                onClick={() => props.onOpenSummary(document.document_id)}
                title={document.original_filename}
              >
                <strong>{document.display_title}</strong>
                <SummaryPreview document={document} />
                <IngestionStatus document={document} />
              </button>
              {assigning === document.document_id && (
                <div
                  class="assign-popover"
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') setAssigning(null)
                  }}
                >
                  <input
                    type="search"
                    placeholder="Filter collections"
                    value={assignFilter}
                    onInput={(event) =>
                      setAssignFilter((event.target as HTMLInputElement).value)
                    }
                    aria-label="Filter collections"
                  />
                  <ul>
                    {props.collections
                      .slice()
                      .sort((a, b) =>
                        (a.path ?? a.name).localeCompare(b.path ?? b.name),
                      )
                      .filter((collection) =>
                        (collection.path ?? collection.name)
                          .toLowerCase()
                          .includes(assignFilter.trim().toLowerCase()),
                      )
                      .map((collection) => {
                        const member = collection.documents.includes(document.document_id)
                        // A paper can be in scope through an ancestor without
                        // being a direct member; an unticked box beside an
                        // in-scope paper would otherwise read as a lie.
                        const node = nodesById.get(collection.collection_id)
                        const inherited =
                          !member && node
                            ? rollupDocuments(node).has(document.document_id)
                            : false
                        return (
                          <li key={collection.collection_id}>
                            <label>
                              <input
                                type="checkbox"
                                checked={member}
                                onChange={() =>
                                  void props.onAssign(
                                    collection.collection_id,
                                    document.document_id,
                                    !member,
                                  )
                                }
                              />
                              <span class="assign-path">
                                {collection.path ?? collection.name}
                              </span>
                              {inherited && (
                                <span class="muted"> · via a subcollection</span>
                              )}
                            </label>
                          </li>
                        )
                      })}
                  </ul>
                </div>
              )}
              <div class="entry-actions">
                {props.collections.length > 0 && (
                  <button
                    type="button"
                    class="link"
                    aria-expanded={assigning === document.document_id}
                    onClick={() =>
                      {
                        setAssignFilter('')
                        setAssigning(
                          assigning === document.document_id ? null : document.document_id,
                        )
                      }
                    }
                  >
                    Collections…
                  </button>
                )}
                <button
                  type="button"
                  class="link"
                  onClick={() => props.onOpenDocument(document.document_id)}
                >
                  Read PDF
                </button>
                {document.extraction.status === 'failed' && (
                  <button
                    type="button"
                    class="link"
                    onClick={() => props.onReextract(document.document_id)}
                  >
                    Retry extraction
                  </button>
                )}
                {document.extraction.status === 'ok' &&
                  document.summary.status !== 'unavailable' && (
                    <button
                      type="button"
                      class="link"
                      onClick={() => void props.onSummarize(document.document_id)}
                    >
                      {document.summary.status === 'ready' ? 'Regenerate' : 'Summarize'}
                    </button>
                  )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <div class="section-head">
          <h3>Notes</h3>
          <button type="button" class="link" onClick={props.onNewNote}>
            New
          </button>
        </div>
        <ul class="list">
          {props.notes.map((note) => (
            <li key={note.note_id}>
              <button
                type="button"
                class={props.activeTarget.noteId === note.note_id ? 'entry active' : 'entry'}
                onClick={() => props.onOpenNote(note.note_id)}
              >
                <strong>{note.title}</strong>
                <span class="muted">{note.note_id}.md</span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <details class="changes" onToggle={() => void props.onLoadChanges()}>
        <summary>Recent edits</summary>
        {props.changes.length === 0 && <p class="muted">No edits recorded yet.</p>}
        <ul>
          {props.changes.map((change) => (
            <li key={change.change_id}>
              <span>
                <strong>{change.origin}</strong> · {change.target_id}
                <br />
                <span class="muted">{change.summary}</span>
              </span>
              {change.undoable ? (
                <button type="button" class="link" onClick={() => props.onUndo(change)}>
                  Undo
                </button>
              ) : (
                <span class="muted">{change.undone_at ? 'undone' : '—'}</span>
              )}
            </li>
          ))}
        </ul>
      </details>
    </nav>
  )
}

function SummaryPreview({ document }: { document: DocumentRecord }) {
  const text = (document.summary_text ?? '')
    .replace(/^#.*$/gm, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
  if (!text) return null
  return <span class="preview">{text.slice(0, 120)}…</span>
}

function IngestionStatus({ document }: { document: DocumentRecord }) {
  const extraction = document.extraction
  const summary = document.summary
  const parts: { label: string; tone: 'ok' | 'warn' | 'error' | 'muted' }[] = []

  if (extraction.status === 'pending') parts.push({ label: 'queued for text extraction', tone: 'muted' })
  else if (extraction.status === 'running') parts.push({ label: 'extracting text…', tone: 'muted' })
  else if (extraction.status === 'failed')
    parts.push({
      label: extraction.kind === 'encrypted' ? 'encrypted — readable only' : 'extraction failed',
      tone: 'error',
    })
  else if (extraction.quality === 'none')
    parts.push({ label: 'no text layer (scan) — readable only', tone: 'warn' })
  else if (extraction.quality === 'partial')
    parts.push({ label: 'partly extracted', tone: 'warn' })

  if (summary.status === 'queued') parts.push({ label: 'summary queued', tone: 'muted' })
  else if (summary.status === 'ready') parts.push({ label: 'summary ready', tone: 'ok' })
  else if (summary.status === 'failed') parts.push({ label: 'summary failed', tone: 'error' })
  else if (summary.status === 'unavailable')
    parts.push({ label: 'no summary possible', tone: 'muted' })

  if (parts.length === 0) return null
  return (
    <span class="status-row">
      {parts.map((part) => (
        <span key={part.label} class={`status ${part.tone}`}>
          {part.label}
        </span>
      ))}
    </span>
  )
}
