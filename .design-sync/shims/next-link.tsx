// Sync shim for `next/link` — design previews have no Next router, so render a
// plain anchor (Next's Link is an <a> at runtime anyway). Faithful for static
// preview rendering; resolved via .design-sync/tsconfig.sync.json paths.
import React from 'react';

type LinkProps = Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & {
  href: string | { pathname?: string };
  prefetch?: boolean;
  replace?: boolean;
  scroll?: boolean;
  shallow?: boolean;
};

const Link = React.forwardRef<HTMLAnchorElement, LinkProps>(function Link(
  { href, prefetch, replace, scroll, shallow, children, ...rest },
  ref,
) {
  const h = typeof href === 'string' ? href : href?.pathname ?? '#';
  return (
    <a ref={ref} href={h} {...rest}>
      {children}
    </a>
  );
});

export default Link;
