// Server-only loader for the Updates feed.
//
// Reads every markdown file in `lib/updates/content/`, parses YAML frontmatter
// with gray-matter, and returns typed, sorted entries. Imported only from
// server components (`app/page.tsx`, `app/updates/page.tsx`), which are
// statically rendered, so the filesystem reads happen at build time.

import fs from 'node:fs';
import path from 'node:path';
import matter from 'gray-matter';
import type { UpdateEntry, UpdateCategory, UpdateLink } from './types';
import { UPDATE_CATEGORIES } from './types';

const CONTENT_DIR = path.join(process.cwd(), 'lib/updates/content');

function normalizeDate(value: unknown): string {
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (typeof value === 'string') {
    const d = new Date(value);
    if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
    return value;
  }
  return '';
}

function normalizeCategory(value: unknown): UpdateCategory {
  return UPDATE_CATEGORIES.includes(value as UpdateCategory)
    ? (value as UpdateCategory)
    : 'data';
}

function normalizeLinks(value: unknown): UpdateLink[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (l): l is UpdateLink =>
      !!l &&
      typeof l === 'object' &&
      typeof (l as UpdateLink).label === 'string' &&
      typeof (l as UpdateLink).href === 'string'
  );
}

/** First non-empty paragraph of the body, with markdown markup stripped. */
function deriveSummary(body: string): string {
  const firstPara = body.trim().split(/\n\s*\n/)[0] ?? '';
  return firstPara
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '') // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // links → text
    .replace(/[*_`>#]/g, '') // emphasis / code / blockquote / heading marks
    .replace(/\s+/g, ' ')
    .trim();
}

/** All updates, sorted: pinned first, then newest date first. */
export function getAllUpdates(): UpdateEntry[] {
  let files: string[];
  try {
    files = fs.readdirSync(CONTENT_DIR).filter((f) => f.endsWith('.md'));
  } catch {
    return [];
  }

  const entries: UpdateEntry[] = files.map((file) => {
    const raw = fs.readFileSync(path.join(CONTENT_DIR, file), 'utf8');
    const { data, content } = matter(raw);
    const body = content.trim();
    const summary =
      typeof data.summary === 'string' && data.summary.trim()
        ? data.summary.trim()
        : deriveSummary(body);

    return {
      slug: file.replace(/\.md$/, ''),
      title: String(data.title ?? file.replace(/\.md$/, '')),
      date: normalizeDate(data.date),
      category: normalizeCategory(data.category),
      summary,
      body,
      links: normalizeLinks(data.links),
      pinned: Boolean(data.pinned),
    };
  });

  return entries.sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    if (a.date !== b.date) return a.date < b.date ? 1 : -1;
    return a.slug < b.slug ? 1 : -1; // stable tiebreak, newest-ish first
  });
}

/** The `limit` most recent entries, plus the total count (for the "view all" link). */
export function getRecentUpdates(limit: number): {
  entries: UpdateEntry[];
  total: number;
} {
  const all = getAllUpdates();
  return { entries: all.slice(0, limit), total: all.length };
}
