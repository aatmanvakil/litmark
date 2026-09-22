/**
 * The source panel: PDF.js rendering with a highlight overlay.
 *
 * Coordinate contract — stored rectangles are `[x0, y0, x1, y1]` in [0,1],
 * relative to the visible page after its native rotation and crop box have
 * been applied, origin top-left. PDF.js builds its viewport from that same
 * visible box, so the conversion below is an explicit multiply by the
 * viewport's width and height. CSS pixel values are never stored, and the
 * overlay is recomputed on every zoom change rather than scaled.
 */

import { useEffect, useMemo, useRef, useState } from 'preact/hooks'
import * as pdfjs from 'pdfjs-dist'
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist'
// Bundled locally by Vite — nothing is fetched from a CDN at runtime.
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

import { api, type DocumentRecord, type ReferenceRecord } from '../api'

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

const ZOOM_STEPS = [0.6, 0.75, 0.9, 1, 1.15, 1.3, 1.5, 1.75, 2, 2.5, 3]
// Breathing room so the page is not flush against the panel edges.
const PAGE_MARGIN = 24

/** The zoom step closest to an arbitrary scale, so Fit hands over smoothly. */
function nearestZoomIndex(scale: number): number {
  let best = 0
  for (let index = 1; index < ZOOM_STEPS.length; index += 1) {
    if (Math.abs(ZOOM_STEPS[index]! - scale) < Math.abs(ZOOM_STEPS[best]! - scale)) {
      best = index
    }
  }
  return best
}

export interface PdfSelection {
  documentId: string
  pageNumber: number
  quote: string
  prefix: string
  suffix: string
}

export interface PdfViewerProps {
  document: DocumentRecord
  highlight: ReferenceRecord | null
  /** Jump target, used when a citation is opened from the editor. */
  gotoPage: number | null
  onSelection: (selection: PdfSelection | null) => void
  onClose?: () => void
}

export function PdfViewer(props: PdfViewerProps) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [zoomIndex, setZoomIndex] = useState(3)
  // A letter page at 100% is wider than this panel, which would clip the text
  // and hide half the highlight. Fitting the width is the useful default; any
  // manual zoom takes over from there.
  const [fitWidth, setFitWidth] = useState(true)
  const [available, setAvailable] = useState<number | null>(null)
  const [fittedScale, setFittedScale] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pageInput, setPageInput] = useState('1')
  const scroller = useRef<HTMLDivElement | null>(null)
  const scale = fitWidth ? (fittedScale ?? 1) : (ZOOM_STEPS[zoomIndex] ?? 1)

  // Track the panel width so the fitted scale follows a resized pane.
  useEffect(() => {
    const node = scroller.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width
      if (width) setAvailable(width)
    })
    observer.observe(node)
    setAvailable(node.clientWidth)
    return () => observer.disconnect()
  }, [pdf])

  const zoomTo = (index: number): void => {
    setFitWidth(false)
    setZoomIndex(Math.min(Math.max(0, index), ZOOM_STEPS.length - 1))
  }

  const url = useMemo(() => api.pdfUrl(props.document.document_id), [props.document.document_id])

  useEffect(() => {
    let cancelled = false
    setError(null)
    setPdf(null)
    const task = pdfjs.getDocument({ url, isEvalSupported: false })
    task.promise.then(
      (loaded) => {
        if (cancelled) {
          void loaded.destroy()
          return
        }
        setPdf(loaded)
      },
      (reason: unknown) => {
        if (cancelled) return
        setError(
          reason instanceof Error
            ? `This PDF could not be displayed: ${reason.message}`
            : 'This PDF could not be displayed.',
        )
      },
    )
    return () => {
      cancelled = true
      void task.destroy()
    }
  }, [url])

  // Follow an opened citation.
  useEffect(() => {
    if (props.gotoPage && props.gotoPage >= 1) {
      setPageNumber(props.gotoPage)
      setPageInput(String(props.gotoPage))
    }
  }, [props.gotoPage])

  const total = pdf?.numPages ?? props.document.page_count ?? 1
  const highlightRects =
    props.highlight &&
    props.highlight.page_number === pageNumber &&
    props.highlight.document_id === props.document.document_id
      ? (props.highlight.rects ?? [])
      : []

  const go = (next: number): void => {
    const clamped = Math.min(Math.max(1, next), total)
    setPageNumber(clamped)
    setPageInput(String(clamped))
    props.onSelection(null)
  }

  return (
    <section class="pdf" aria-label="Source panel">
      <header class="pdf-bar">
        <div class="pdf-title" title={props.document.original_filename}>
          <strong>{props.document.display_title}</strong>
          <span class="muted">{props.document.original_filename}</span>
        </div>
        {props.onClose && (
          <button type="button" class="ghost" onClick={props.onClose} title="Close the source panel">
            ✕
          </button>
        )}
      </header>

      <div class="pdf-controls">
        <button type="button" onClick={() => go(pageNumber - 1)} disabled={pageNumber <= 1}>
          ‹
        </button>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            const parsed = Number.parseInt(pageInput, 10)
            if (Number.isFinite(parsed)) go(parsed)
          }}
        >
          <input
            type="text"
            value={pageInput}
            onInput={(event) => setPageInput((event.target as HTMLInputElement).value)}
            aria-label="Page number"
            size={3}
          />
        </form>
        <span class="muted">of {total}</span>
        <button type="button" onClick={() => go(pageNumber + 1)} disabled={pageNumber >= total}>
          ›
        </button>
        <span class="spacer" />
        <button
          type="button"
          onClick={() => zoomTo(nearestZoomIndex(scale) - 1)}
          disabled={!fitWidth && zoomIndex === 0}
          title="Zoom out"
        >
          −
        </button>
        <span class="muted zoom">{Math.round(scale * 100)}%</span>
        <button
          type="button"
          onClick={() => zoomTo(nearestZoomIndex(scale) + 1)}
          disabled={!fitWidth && zoomIndex === ZOOM_STEPS.length - 1}
          title="Zoom in"
        >
          +
        </button>
        <button
          type="button"
          class={fitWidth ? 'fit active' : 'fit'}
          onClick={() => setFitWidth(true)}
          title="Fit the page to the panel width"
        >
          Fit
        </button>
      </div>

      {props.highlight && props.highlight.page_number !== pageNumber && (
        <p class="pdf-note">
          The selected evidence is on page {props.highlight.page_number}.{' '}
          <button type="button" class="link" onClick={() => go(props.highlight!.page_number)}>
            Go there
          </button>
        </p>
      )}
      {props.highlight?.status === 'page_only' && props.highlight.page_number === pageNumber && (
        <p class="pdf-note">
          Page-only citation: the exact passage was not matched, so nothing is highlighted.
        </p>
      )}
      {props.highlight?.document_sha256_matches === false && (
        <p class="pdf-note warn">
          This PDF's bytes differ from the ones the citation was made against; the
          highlight may be misplaced.
        </p>
      )}

      {error !== null && <p class="pdf-error">{error}</p>}

      {pdf && !error && (
        <PdfPage
          pdf={pdf}
          pageNumber={pageNumber}
          scale={scale}
          fitWidth={fitWidth}
          available={available}
          onFitted={setFittedScale}
          scrollerRef={scroller}
          rects={highlightRects}
          documentId={props.document.document_id}
          onSelection={props.onSelection}
        />
      )}
      {!pdf && !error && <p class="pdf-loading">Opening the PDF…</p>}
    </section>
  )
}

interface PdfPageProps {
  pdf: PDFDocumentProxy
  pageNumber: number
  scale: number
  fitWidth: boolean
  available: number | null
  onFitted: (scale: number) => void
  scrollerRef: { current: HTMLDivElement | null }
  rects: number[][]
  documentId: string
  onSelection: (selection: PdfSelection | null) => void
}

function PdfPage(props: PdfPageProps) {
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const textLayer = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState<{ width: number; height: number } | null>(null)

  useEffect(() => {
    let cancelled = false
    let page: PDFPageProxy | null = null

    void (async () => {
      page = await props.pdf.getPage(props.pageNumber)
      if (cancelled) return

      // Default rotation: PDF.js applies the page's own /Rotate, and the
      // viewport is sized from the crop box — the same visible box the stored
      // rectangles are normalized against.
      // When fitting, derive the scale from the unscaled page width so the
      // page exactly fills the panel; report it back for the zoom readout.
      let effective = props.scale
      if (props.fitWidth && props.available) {
        const base = page.getViewport({ scale: 1 })
        effective = Math.max(0.1, (props.available - PAGE_MARGIN) / base.width)
        props.onFitted(effective)
      }
      const viewport = page.getViewport({ scale: effective })
      const ratio = window.devicePixelRatio || 1
      const target = canvas.current
      if (!target) return

      target.width = Math.floor(viewport.width * ratio)
      target.height = Math.floor(viewport.height * ratio)
      target.style.width = `${Math.floor(viewport.width)}px`
      target.style.height = `${Math.floor(viewport.height)}px`
      setSize({ width: viewport.width, height: viewport.height })

      const context = target.getContext('2d')
      if (!context) return
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
      context.clearRect(0, 0, viewport.width, viewport.height)
      await page.render({ canvasContext: context, viewport }).promise
      if (cancelled) return

      // A selectable text layer, so a reader can select a passage to cite.
      const layer = textLayer.current
      if (layer) {
        layer.replaceChildren()
        layer.style.width = `${Math.floor(viewport.width)}px`
        layer.style.height = `${Math.floor(viewport.height)}px`
        const built = new pdfjs.TextLayer({
          textContentSource: page.streamTextContent(),
          container: layer,
          viewport,
        })
        await built.render()
      }
    })()

    return () => {
      cancelled = true
      page?.cleanup()
    }
  }, [props.pdf, props.pageNumber, props.scale, props.fitWidth, props.available])

  const captureSelection = (): void => {
    const selection = window.getSelection()
    const layer = textLayer.current
    if (!selection || selection.isCollapsed || !layer) {
      props.onSelection(null)
      return
    }
    if (!layer.contains(selection.anchorNode) || !layer.contains(selection.focusNode)) {
      return
    }
    const quote = selection.toString().replace(/\s+/g, ' ').trim()
    if (quote.length < 3) {
      props.onSelection(null)
      return
    }
    // Surrounding text lets the server disambiguate a repeated passage.
    const whole = (layer.textContent ?? '').replace(/\s+/g, ' ')
    const at = whole.indexOf(quote)
    props.onSelection({
      documentId: props.documentId,
      pageNumber: props.pageNumber,
      quote,
      prefix: at > 0 ? whole.slice(Math.max(0, at - 60), at) : '',
      suffix: at >= 0 ? whole.slice(at + quote.length, at + quote.length + 60) : '',
    })
  }

  return (
    <div class="pdf-scroll" ref={props.scrollerRef}>
      <div class="pdf-page" onMouseUp={captureSelection} onTouchEnd={captureSelection}>
        <canvas ref={canvas} />
        <div class="pdf-text-layer" ref={textLayer} />
        {size && (
          <div class="pdf-overlay" style={{ width: `${size.width}px`, height: `${size.height}px` }}>
            {props.rects.map((rect, index) => {
              const [x0, y0, x1, y1] = rect as [number, number, number, number]
              return (
                <div
                  key={`${index}-${x0}-${y0}`}
                  class="pdf-highlight"
                  style={{
                    left: `${x0 * size.width}px`,
                    top: `${y0 * size.height}px`,
                    width: `${Math.max(1, (x1 - x0) * size.width)}px`,
                    height: `${Math.max(1, (y1 - y0) * size.height)}px`,
                  }}
                />
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
