import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

/**
 * The release page document (P6b-3 / P6c-2): the markdown an admin pushed
 * with `campfire dr page push`, rendered above the release's cards. Figures
 * are referenced by bare name or `./name` and resolve to the page-asset
 * route, which authorizes through the viewer's manifest rows and redirects
 * to the archived object. Raw HTML is skipped; react-markdown's default url
 * transform keeps links to http(s)/mailto only.
 */
export function pageAssetHref(tag: string, src: string): string | null {
  const name = src.replace(/^\.\//, '');
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) return null;
  return `/api/dr/page-asset?tag=${encodeURIComponent(tag)}&name=${encodeURIComponent(name)}`;
}

function isExternal(src: string): boolean {
  return /^(https?:)?\/\//i.test(src) || src.startsWith('/');
}

export function ReleasePageDoc({ tag, markdown, assets }: { tag: string; markdown: string; assets: ReadonlySet<string> }) {
  const components: Components = {
    img: ({ src, alt }) => {
      const s = typeof src === 'string' ? src : '';
      if (!s) return null;
      const href = isExternal(s) ? s : assets.has(s.replace(/^\.\//, '')) ? pageAssetHref(tag, s) : null;
      if (!href) return null;
      // eslint-disable-next-line @next/next/no-img-element
      return <img src={href} alt={alt ?? ''} className="max-w-full rounded-lg border border-border" loading="lazy" />;
    },
    a: ({ href, children }) => (
      <a href={href} className="text-primary hover:underline" target={href && isExternal(href) ? '_blank' : undefined} rel="noreferrer">
        {children}
      </a>
    ),
  };
  return (
    <div className="prose-campfire">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={components}>
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
