// Client-side docs building blocks. The server renderer (MarkdownServer) is
// deliberately NOT re-exported here: importing it through a barrel that also
// carries the client renderer drags the parser into the importing route's
// client bundle. Import it from './MarkdownServer' directly.
export { default as MarkdownRenderer } from './MarkdownRenderer';
export type { TOCItem } from '@/lib/docs/toc';
export { default as TableOfContents } from './TableOfContents';
export { default as DocNavigation } from './DocNavigation';
