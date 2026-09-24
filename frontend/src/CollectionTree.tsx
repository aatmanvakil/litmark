/**
 * The collection tree in the sidebar.
 *
 * Nested lists rather than `role="tree"`: a tree role without roving tabindex
 * and arrow keys is worse for a screen reader than plain nested lists, which
 * give native tab order and Enter/Space for free. A deliberate trade-off.
 *
 * The twisty and the selection control are siblings, never nested — a button
 * inside a button is invalid HTML and jsdom will not complain about it.
 */

import type { Collection } from './api'
import { rollupDocuments, type TreeNode } from './collections'

export interface CollectionTreeProps {
  nodes: TreeNode[]
  activeCollectionId: string | null
  /** Ancestors of the selection, which are in scope without being selected. */
  inScopeIds: Set<string>
  /** Collapsed ids. Empty means the whole tree is visible, which is the
   *  right default: a tree that starts shut hides the entire library. */
  collapsed: Set<string>
  onToggleExpanded: (collectionId: string) => void
  onSelect: (collectionId: string) => void
  onRename: (collection: Collection) => void
  onDelete: (collection: Collection) => void
  onAddChild: (parent: Collection) => void
  onMove: (collection: Collection) => void
}

export function CollectionTree(props: CollectionTreeProps) {
  if (props.nodes.length === 0) return null
  return (
    <ul class="list tree">
      {props.nodes.map((node) => (
        <TreeRow key={node.collection.collection_id} node={node} {...props} />
      ))}
    </ul>
  )
}

function TreeRow({ node, ...props }: { node: TreeNode } & CollectionTreeProps) {
  const collection = node.collection
  const id = collection.collection_id
  const hasChildren = node.children.length > 0
  const isOpen = !props.collapsed.has(id)
  const childrenId = `subtree-${id}`

  const direct = collection.documents.length
  const total = rollupDocuments(node).size

  const selected = props.activeCollectionId === id
  const inScope = !selected && props.inScopeIds.has(id)

  return (
    <li>
      <div class="entry-row">
        {hasChildren ? (
          <button
            type="button"
            class="twisty"
            aria-expanded={isOpen}
            aria-controls={childrenId}
            aria-label={isOpen ? `Collapse ${collection.name}` : `Expand ${collection.name}`}
            onClick={() => props.onToggleExpanded(id)}
          >
            {isOpen ? '▾' : '▸'}
          </button>
        ) : (
          <span class="twisty spacer" aria-hidden="true" />
        )}
        <button
          type="button"
          class={selected ? 'entry active' : inScope ? 'entry in-scope' : 'entry'}
          title={collection.path ?? collection.name}
          onClick={() => props.onSelect(id)}
        >
          <strong>{collection.name}</strong>
          <span class="muted">
            {total === direct
              ? `${direct} ${direct === 1 ? 'paper' : 'papers'}`
              : `${direct} · ${total} with subcollections`}
          </span>
        </button>
      </div>
      <div class="entry-actions">
        <button type="button" class="link" onClick={() => props.onAddChild(collection)}>
          + Subtopic
        </button>
        <button type="button" class="link" onClick={() => props.onRename(collection)}>
          Rename
        </button>
        <button type="button" class="link" onClick={() => props.onMove(collection)}>
          Move…
        </button>
        <button type="button" class="link" onClick={() => props.onDelete(collection)}>
          Remove
        </button>
      </div>
      {hasChildren && isOpen && (
        <ul class="list tree" id={childrenId}>
          {node.children.map((child) => (
            <TreeRow key={child.collection.collection_id} node={child} {...props} />
          ))}
        </ul>
      )}
    </li>
  )
}
