/**
 * Pure helpers for the collection tree.
 *
 * Kept out of the component so the cycle and orphan handling can be reasoned
 * about on its own: collections.json is hand-editable, and App has no error
 * boundary, so one bad row must not take the whole interface down.
 */

import type { Collection } from './api'

export interface TreeNode {
  collection: Collection
  children: TreeNode[]
  depth: number
}

/** Roots first, each with its subtree. Orphans and cycles surface as roots. */
export function buildTree(collections: Collection[]): TreeNode[] {
  const byId = new Map(collections.map((c) => [c.collection_id, c]))
  const childrenOf = new Map<string, Collection[]>()
  const roots: Collection[] = []

  for (const collection of collections) {
    const parent = collection.parent_id
    // A parent that is missing or self-referential makes this a root rather
    // than vanishing from the sidebar.
    if (!parent || parent === collection.collection_id || !byId.has(parent)) {
      roots.push(collection)
      continue
    }
    const siblings = childrenOf.get(parent) ?? []
    siblings.push(collection)
    childrenOf.set(parent, siblings)
  }

  const placed = new Set<string>()
  const build = (collection: Collection, depth: number): TreeNode => {
    placed.add(collection.collection_id)
    const children = (childrenOf.get(collection.collection_id) ?? [])
      .filter((child) => !placed.has(child.collection_id))
      .map((child) => build(child, depth + 1))
    return { collection, children, depth }
  }

  const tree = roots.map((root) => build(root, 0))
  // Anything still unplaced sits in a cycle; show it rather than lose it.
  for (const collection of collections) {
    if (!placed.has(collection.collection_id)) {
      tree.push(build(collection, 0))
    }
  }
  return tree
}

export function flatten(nodes: TreeNode[]): TreeNode[] {
  return nodes.flatMap((node) => [node, ...flatten(node.children)])
}

/** Every id in a subtree, the node itself first. */
export function subtreeIds(node: TreeNode): string[] {
  return [node.collection.collection_id, ...node.children.flatMap(subtreeIds)]
}

/** Documents in a collection or anything under it, counted once each. */
export function rollupDocuments(node: TreeNode): Set<string> {
  const found = new Set(node.collection.documents)
  for (const child of node.children) {
    for (const id of rollupDocuments(child)) found.add(id)
  }
  return found
}

/** The ancestors of one collection, so the tree can reveal a selection. */
export function ancestorsOf(collections: Collection[], id: string): string[] {
  const byId = new Map(collections.map((c) => [c.collection_id, c]))
  const chain: string[] = []
  const seen = new Set([id])
  let current = byId.get(id)?.parent_id ?? null
  while (current && byId.has(current) && !seen.has(current)) {
    chain.push(current)
    seen.add(current)
    current = byId.get(current)?.parent_id ?? null
  }
  return chain
}
