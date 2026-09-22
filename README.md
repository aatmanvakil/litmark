# Research Workspace

A local research application: import papers, read short cited summaries, write
Markdown notes, inspect the cited PDF passages, and chat with a coding agent
that can search the project and edit its files.

**Import papers → ask questions → write a sourced note → inspect the evidence → refine the note.**

Everything you write stays as ordinary files in a movable project directory:
`notes/*.md`, `documents/<id>/original.pdf`, `references.json`. The application
is a convenient view onto those files, not their owner.

## Install

```bash
python -m pip install research-workspace          # core reader and editor
python -m pip install 'research-workspace[claude]'  # plus the agent adapter
```

Requires Python 3.11+. Tested on macOS and Linux. The published wheel contains
the compiled browser assets — **you do not need Node, npm, or a second web
server to run this application.**

With `pipx` or `uv`:

```bash
pipx install research-workspace
uv tool install research-workspace
```

## Use

```bash
research-workspace init ./my-research
research-workspace serve ./my-research --open
```

`serve` binds to `127.0.0.1`, prints the local URL, and serves exactly one
project per process. Reopening an existing project needs no initialization, and
`init` never overwrites existing material.

### Reaching it through a proxy or port-forward

By default the server binds to loopback *and* rejects any `Host` header that is
not a loopback name. That second check is what stops a malicious page from
using DNS rebinding to talk to your local server — but behind a reverse proxy
or a port-forward the header carries the proxy's hostname, so the check would
reject every real request. Widen it explicitly:

```bash
# Bind beyond loopback; any Host header is then accepted.
research-workspace serve ./my-research --host 0.0.0.0

# Or name the proxy's hostname and keep the check strict.
research-workspace serve ./my-research --host 0.0.0.0 --allow-host papers.internal
```

The per-process session credential embedded in the served page remains the
access control in both cases. On an untrusted network prefer an SSH tunnel,
which keeps the default loopback binding intact:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@the-machine
```

Check your environment at any time:

```bash
research-workspace doctor ./my-research
```

`doctor` reports on the workspace, the optional agent dependency, the runtime
it needs, and whether authentication is configured — without printing secrets.

## Agent configuration

The agent backend is the [Claude Agent SDK for
Python](https://code.claude.com/docs/en/agent-sdk/python), behind a
provider-neutral adapter. Provider credentials and billing are separate from
installing this application; a consumer chat subscription does not by itself
grant SDK access.

The reader and editor work with no agent account at all. Import and text
extraction still run; automatic summaries wait until an agent is configured,
and the interface says so plainly.

## Project layout

| Path | Purpose |
| --- | --- |
| `project.toml` | Schema version and non-secret project settings. |
| `documents/<id>/original.pdf` | Original immutable PDF. |
| `documents/<id>/metadata.json` | Identity, hash, filename, title, page count, status. |
| `documents/<id>/pages.json` | Per-page text, character geometry, offset mapping. |
| `documents/<id>/summary.md` | Editable, cited summary. |
| `notes/*.md` | User and agent notes. |
| `references.json` | Versioned source registry. |
| `.research/state.sqlite` | Conversations, events, jobs, run state, change log. |
| `.research/history/` | Previous file contents, for review and undo. |
| `.research/runs/` | Staged agent edits and run diagnostics. |

## Citations

A citation is an ordinary Markdown link with an application-specific
destination:

```markdown
The identification argument relies on a mobility restriction.
[Assumption 2, p. 12](source:ref-001)
```

`ref-001` resolves through `references.json`, which records the document, the
immutable PDF content hash, the one-based physical page number, the quoted
passage, and normalized highlight rectangles. Reference resolution validates
*where* a passage is, not whether it supports the claim — you assess that by
reading the evidence.

## Development

```bash
uv venv && uv pip install -e . --group dev
npm --prefix frontend install
npm --prefix frontend run build      # writes src/research_workspace/static/
uv run pytest
uv run research-workspace serve ./my-research --reload
```

`npm run dev` runs the frontend with hot reload against a server started
separately on port 8765.

### Tests

```bash
uv run pytest                     # server and acceptance criteria
uv run pytest --runslow           # plus: build the wheel, install it clean, serve it
npm --prefix frontend test        # renderer unit tests, and the bundle run in a DOM

# Real-browser checks (A2 live preview, A6 highlight geometry under zoom).
# Point it at a project whose open note contains a source: citation.
research-workspace serve ./my-research &
RW_BASE_URL=http://127.0.0.1:8765/ npm --prefix frontend run test:browser
```

The browser suite drives headless Firefox over Marionette. Headless Chromium is
deliberately not used: under some sandboxes it cannot reach loopback HTTP — it
returns an empty document where `curl` succeeds — so a Chromium suite passes
without having checked anything.

## Scope

v0.1 covers ingestion of text-based PDFs, cited summaries, Markdown live-preview
editing, PDF evidence with highlights, chat with one agent backend, and manual
references. Deferred: OCR, URL downloads, Word/HTML ingestion, Zotero
integration, semantic search, multiple agent providers, arbitrary analysis
execution, multi-user collaboration, full mobile editing, and public hosting.

## License

MIT.
