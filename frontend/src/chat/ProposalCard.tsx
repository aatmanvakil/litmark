/**
 * A paper the agent offered, awaiting a human decision.
 *
 * One card per paper, one button per version: the versions of a work are a
 * single decision with several options, not several decisions. Choosing one
 * resolves the whole card.
 *
 * Nothing here downloads. The confirm call is the only path in the client
 * that causes a PDF to be fetched, and it exists because no agent tool can.
 */

import { useState } from 'preact/hooks'

import { api, ApiError, type AcquisitionVersion, type ProposalRecord } from '../api'

const VERSION_LABEL: Record<string, string> = {
  published: 'Published version',
  accepted_manuscript: 'Accepted manuscript',
  submitted_preprint: 'Preprint',
}

export interface ProposalCardProps {
  proposal: ProposalRecord
  onResolved: () => void
  onOpenDocument: (documentId: string) => void
}

export function ProposalCard({ proposal, onResolved, onOpenDocument }: ProposalCardProps) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const work = proposal.work
  const settled =
    proposal.status === 'confirmed' ||
    proposal.status === 'declined' ||
    proposal.status === 'superseded' ||
    proposal.status === 'expired'

  const confirm = async (version: AcquisitionVersion) => {
    setBusy(version.version_id)
    setError(null)
    try {
      await api.confirmProposal(proposal.proposal_id, version.version_id)
      onResolved()
    } catch (caught) {
      // A conflict means the card is stale rather than broken: something
      // already decided it, so reload instead of showing an error.
      if (caught instanceof ApiError && caught.status === 409) onResolved()
      else setError(caught instanceof Error ? caught.message : 'The download failed.')
    } finally {
      setBusy(null)
    }
  }

  const decline = async () => {
    setBusy('decline')
    try {
      await api.declineProposal(proposal.proposal_id)
      onResolved()
    } catch {
      onResolved()
    } finally {
      setBusy(null)
    }
  }

  if (proposal.status === 'declined') {
    return (
      <p class="proposal-dismissed muted">
        You passed on “{work.title ?? proposal.query}”.
      </p>
    )
  }
  if (proposal.status === 'superseded' || proposal.status === 'expired') {
    return (
      <p class="proposal-dismissed muted">
        The offer for “{work.title ?? proposal.query}” {proposal.status === 'expired'
          ? 'expired'
          : 'was replaced by a newer one'}.
      </p>
    )
  }

  return (
    <div class={proposal.status === 'pending' ? 'proposal pending' : 'proposal'}>
      <div class="proposal-work">
        <strong>{work.title ?? proposal.query}</strong>
        <span class="muted">
          {[work.authors, work.year, work.journal].filter(Boolean).join(' · ')}
        </span>
        {work.doi && <span class="muted">doi:{work.doi}</span>}
      </div>

      {proposal.status === 'confirmed' ? (
        <p class="proposal-outcome">
          Saved as <strong>{proposal.canonical_filename}</strong>
          {proposal.document_id && (
            <>
              {' '}
              <button
                type="button"
                class="link"
                onClick={() => onOpenDocument(proposal.document_id as string)}
              >
                Open
              </button>
            </>
          )}
        </p>
      ) : (
        <>
          {/* One filename for the whole card: it derives from the work, so
              it does not vary by version. */}
          <p class="proposal-filename muted">
            Will be saved as <strong>{proposal.canonical_filename}</strong>
          </p>
          <ul class="proposal-versions">
            {proposal.versions.map((version) => (
              <li key={version.version_id}>
                <span class="proposal-version">
                  {VERSION_LABEL[version.version_type] ?? version.version_type}
                  {version.host && <span class="muted"> · {hostOf(version.url)}</span>}
                  {version.license && <span class="muted"> · {version.license}</span>}
                </span>
                {version.retrievable ? (
                  <button
                    type="button"
                    class="proposal-confirm"
                    disabled={settled || busy !== null || proposal.status === 'processing'}
                    onClick={() => void confirm(version)}
                  >
                    {busy === version.version_id
                      ? 'Downloading…'
                      : `Download from ${hostOf(version.url)}`}
                  </button>
                ) : (
                  <span class="muted">
                    not available to download{version.reason ? ` — ${version.reason}` : ''}
                  </span>
                )}
              </li>
            ))}
          </ul>
          {proposal.status === 'failed' && proposal.error && (
            <p class="proposal-error" role="alert">
              That did not work: {proposal.error}. Pick a version to try again.
            </p>
          )}
          {error && (
            <p class="proposal-error" role="alert">
              {error}
            </p>
          )}
          <button
            type="button"
            class="link"
            disabled={busy !== null}
            onClick={() => void decline()}
          >
            Not this one
          </button>
        </>
      )}
    </div>
  )
}

function hostOf(url: string | null): string {
  if (!url) return 'the source'
  try {
    return new URL(url).hostname
  } catch {
    return 'the source'
  }
}
