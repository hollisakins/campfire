// Table-of-contents extraction for the docs pages. Pure (no React), so the
// server-rendered docs route and the client markdown renderer share it.

export interface TOCItem {
  id: string;
  text: string;
  level: number;
}

/** Slug for a heading's anchor — the same rule the heading renderer applies. */
export function headingToId(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-');
}

/** Headings (h2–h4) of a markdown document, in order. */
export function extractHeadings(markdown: string): TOCItem[] {
  const headingRegex = /^(#{2,4})\s+(.+)$/gm;
  const headings: TOCItem[] = [];
  let match;

  while ((match = headingRegex.exec(markdown)) !== null) {
    const level = match[1].length;
    const text = match[2].trim();
    headings.push({ id: headingToId(text), text, level });
  }

  return headings;
}
