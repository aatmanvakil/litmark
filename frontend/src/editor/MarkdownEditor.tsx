/**
 * The note and summary editor.
 *
 * Save behaviour: debounced autosave, a visible indicator, Cmd/Ctrl-S for an
 * immediate save, the buffer preserved after a failed save, and an explicit
 * conflict prompt when the file changed underneath a dirty buffer.
 */

import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { history, historyKeymap, defaultKeymap, indentWithTab } from '@codemirror/commands'
import { EditorState, type Extension } from '@codemirror/state'
import { search, searchKeymap } from '@codemirror/search'
import {
  EditorView,
  keymap,
  drawSelection,
  highlightActiveLine,
  rectangularSelection,
  crosshairCursor,
} from '@codemirror/view'
import { useEffect, useRef, useState } from 'preact/hooks'

import { ApiError } from '../api'
import { livePreview, setSourceMode, sourceReferenceAt } from './livePreview'

export type SaveState =
  | { kind: 'clean' }
  | { kind: 'dirty' }
  | { kind: 'saving' }
  | { kind: 'saved'; at: number }
  | { kind: 'error'; message: string }
  | { kind: 'conflict'; message: string; theirs: string | null }

export interface MarkdownEditorProps {
  /** Identity of the buffer; changing it loads a different document. */
  docKey: string
  text: string
  revision: string | null
  readOnly?: boolean
  placeholder?: string
  save: (text: string, expectedRevision: string | null) => Promise<{ revision: string }>
  onOpenSource?: (referenceId: string) => void
  onSelectionChange?: (selection: string) => void
  onDirtyChange?: (dirty: boolean) => void
  registerInsert?: (insert: (markdown: string) => void) => void
}

const AUTOSAVE_DELAY_MS = 900

export function MarkdownEditor(props: MarkdownEditorProps) {
  const host = useRef<HTMLDivElement | null>(null)
  const view = useRef<EditorView | null>(null)
  const revision = useRef<string | null>(props.revision)
  const timer = useRef<number | null>(null)
  const saving = useRef(false)
  const [state, setState] = useState<SaveState>({ kind: 'clean' })
  const [showSource, setShowSource] = useState(false)
  // The citation the cursor is inside, if any. Drives the visible
  // "Open source" action, so the Cmd/Ctrl-click shortcut is not the only way in.
  const [sourceAtCursor, setSourceAtCursor] = useState<string | null>(null)

  // Keep a stable reference to the latest props for use inside CodeMirror.
  const latest = useRef(props)
  latest.current = props

  const flush = async (): Promise<void> => {
    const editor = view.current
    if (!editor || saving.current) return
    const text = editor.state.doc.toString()
    saving.current = true
    setState({ kind: 'saving' })
    try {
      const result = await latest.current.save(text, revision.current)
      revision.current = result.revision
      // A save that lands while the user keeps typing leaves the buffer dirty.
      const stillMatches = view.current?.state.doc.toString() === text
      setState(stillMatches ? { kind: 'saved', at: Date.now() } : { kind: 'dirty' })
      latest.current.onDirtyChange?.(!stillMatches)
      if (!stillMatches) schedule()
    } catch (error) {
      // The buffer is never discarded on a failed save.
      if (error instanceof ApiError && error.isConflict) {
        setState({
          kind: 'conflict',
          message: error.body.message,
          theirs: error.body.current_text ?? null,
        })
      } else {
        setState({
          kind: 'error',
          message: error instanceof Error ? error.message : 'Saving failed.',
        })
      }
    } finally {
      saving.current = false
    }
  }

  const schedule = (): void => {
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => void flush(), AUTOSAVE_DELAY_MS)
  }

  useEffect(() => {
    if (!host.current) return

    const extensions: Extension[] = [
      history(),
      drawSelection(),
      rectangularSelection(),
      crosshairCursor(),
      highlightActiveLine(),
      search({ top: true }),
      EditorView.lineWrapping,
      markdown({ base: markdownLanguage, codeLanguages: [] }),
      livePreview({
        onOpenSource: (id) => latest.current.onOpenSource?.(id),
        onOpenLink: (href) => window.open(href, '_blank', 'noopener,noreferrer'),
      }),
      keymap.of([
        {
          key: 'Mod-s',
          preventDefault: true,
          run: () => {
            if (timer.current !== null) window.clearTimeout(timer.current)
            void flush()
            return true
          },
        },
        ...searchKeymap,
        ...historyKeymap,
        ...defaultKeymap,
        indentWithTab,
      ]),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          setState({ kind: 'dirty' })
          latest.current.onDirtyChange?.(true)
          schedule()
        }
        if (update.selectionSet || update.docChanged) {
          const { from, to, head } = update.state.selection.main
          if (latest.current.onSelectionChange) {
            latest.current.onSelectionChange(
              from === to ? '' : update.state.doc.sliceString(from, to),
            )
          }
          setSourceAtCursor(sourceReferenceAt(update.state, head))
        }
      }),
      EditorView.editable.of(!props.readOnly),
    ]

    const editor = new EditorView({
      state: EditorState.create({ doc: props.text, extensions }),
      parent: host.current,
    })
    view.current = editor
    revision.current = props.revision
    setState({ kind: 'clean' })

    props.registerInsert?.((snippet: string) => {
      const target = view.current
      if (!target) return
      const at = target.state.selection.main.to
      target.dispatch({
        changes: { from: at, insert: snippet },
        selection: { anchor: at + snippet.length },
      })
      target.focus()
    })

    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
      editor.destroy()
      view.current = null
    }
    // Rebuilding on docKey is intentional: a different note is a different buffer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.docKey])

  // Reload a clean buffer when the file changed on disk; keep a dirty one.
  useEffect(() => {
    const editor = view.current
    if (!editor) return
    if (state.kind === 'dirty' || state.kind === 'saving' || state.kind === 'conflict') return
    if (props.revision === revision.current) return
    if (editor.state.doc.toString() === props.text) {
      revision.current = props.revision
      return
    }
    editor.dispatch({
      changes: { from: 0, to: editor.state.doc.length, insert: props.text },
    })
    revision.current = props.revision
    setState({ kind: 'clean' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.text, props.revision])

  const toggleSource = (): void => {
    const next = !showSource
    setShowSource(next)
    view.current?.dispatch({ effects: setSourceMode.of(next) })
    view.current?.focus()
  }

  const keepMine = (): void => {
    // Re-save against the revision the server reports as current.
    void (async () => {
      const editor = view.current
      if (!editor) return
      try {
        const current = await latest.current.save(editor.state.doc.toString(), null)
        revision.current = current.revision
        setState({ kind: 'saved', at: Date.now() })
      } catch (error) {
        setState({
          kind: 'error',
          message: error instanceof Error ? error.message : 'Saving failed.',
        })
      }
    })()
  }

  const takeTheirs = (): void => {
    if (state.kind !== 'conflict' || state.theirs === null) return
    const editor = view.current
    if (!editor) return
    editor.dispatch({
      changes: { from: 0, to: editor.state.doc.length, insert: state.theirs },
    })
    setState({ kind: 'clean' })
    void flush()
  }

  return (
    <div class="editor">
      <div class="editor-bar">
        <SaveIndicator state={state} />
        <div class="editor-actions">
          <button
            type="button"
            class="toggle open-source"
            data-action="open-source"
            disabled={sourceAtCursor === null}
            onClick={() => {
              if (sourceAtCursor) latest.current.onOpenSource?.(sourceAtCursor)
            }}
            title={
              sourceAtCursor
                ? `Open ${sourceAtCursor} — the cited page, with the passage highlighted`
                : 'Put the cursor in a citation to open its source (or Cmd/Ctrl-click it)'
            }
          >
            {sourceAtCursor ? `Open source · ${sourceAtCursor}` : 'Open source'}
          </button>
          <button
            type="button"
            class={showSource ? 'toggle active' : 'toggle'}
            onClick={toggleSource}
            title="Show the complete Markdown text without presentation decorations"
          >
            Source
          </button>
        </div>
      </div>

      {state.kind === 'conflict' && (
        <div class="conflict" role="alert">
          <strong>This file changed while you were editing it.</strong>
          <p>{state.message}</p>
          <p class="hint">
            Both versions are intact. Choose which one to keep — nothing is merged
            automatically.
          </p>
          <div class="conflict-actions">
            <button type="button" onClick={keepMine}>
              Keep my version
            </button>
            <button type="button" onClick={takeTheirs} disabled={state.theirs === null}>
              Use the version on disk
            </button>
          </div>
          {state.theirs !== null && (
            <details>
              <summary>Compare</summary>
              <div class="conflict-compare">
                <section>
                  <h4>On disk</h4>
                  <pre>{state.theirs}</pre>
                </section>
                <section>
                  <h4>Mine</h4>
                  <pre>{view.current?.state.doc.toString() ?? ''}</pre>
                </section>
              </div>
            </details>
          )}
        </div>
      )}

      <div class="editor-surface" ref={host} />
    </div>
  )
}

function SaveIndicator({ state }: { state: SaveState }) {
  switch (state.kind) {
    case 'clean':
      return <span class="save-state">Saved</span>
    case 'dirty':
      return <span class="save-state working">Unsaved changes…</span>
    case 'saving':
      return <span class="save-state working">Saving…</span>
    case 'saved':
      return <span class="save-state ok">Saved</span>
    case 'error':
      return (
        <span class="save-state error" title={state.message}>
          Not saved — {state.message}
        </span>
      )
    case 'conflict':
      return <span class="save-state error">Conflict</span>
  }
}
