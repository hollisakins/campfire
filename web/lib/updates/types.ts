// Types for the landing-page "Updates" feed.
// Entries are authored as markdown files in `lib/updates/content/*.md`
// (frontmatter + body) and loaded server-side by `loader.ts`.
//
// NOTE: keep this file free of Tailwind class strings — `lib/` is not scanned
// by Tailwind's content globs, so category → color mapping lives in the
// `components/updates/CategoryChip` component instead.

export type UpdateCategory = 'data' | 'pipeline' | 'client' | 'release';

/** Categories in display order, used for validation in the authoring tool. */
export const UPDATE_CATEGORIES: UpdateCategory[] = [
  'data',
  'pipeline',
  'client',
  'release',
];

export interface UpdateEntry {
  /** Derived from the filename (date-prefixed), used as the `/updates` anchor. */
  slug: string;
  title: string;
  /** ISO date (YYYY-MM-DD). */
  date: string;
  category: UpdateCategory;
  /** Short blurb shown in the landing feed (frontmatter `summary` or first paragraph). */
  summary: string;
  /** Full markdown body, rendered on the `/updates` page. */
  body: string;
  /** Pinned entries sort to the top regardless of date. */
  pinned: boolean;
  /** Program slugs this update is restricted to. Empty = public (everyone).
   *  Non-empty = visible only to viewers who can access ≥1 of these programs. */
  programs: string[];
}
