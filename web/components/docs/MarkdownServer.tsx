// Server-rendered markdown (perf T1-7 / #503). The /docs route is static
// prose, so the parser (react-markdown + remark-gfm + rehype-highlight,
// ~130 kB of client JS before) now runs at build time and the browser gets
// HTML. Only the interactive leaves (CodeBlock, DocImage) hydrate.

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { markdownComponents } from './markdown-components';

export default function MarkdownServer({ content }: { content: string }) {
  return (
    <div className="prose-campfire">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
