'use client';

import { useEffect, useState, type RefObject } from 'react';

/**
 * True once the referenced element has entered (or come within `rootMargin`
 * of) the viewport; stays true afterwards. Used to defer below-the-fold data
 * fetches so an object page's first paint isn't queued behind them (#499).
 * Without IntersectionObserver (or on the server) it resolves to true.
 */
export function useInView(
  ref: RefObject<Element | null>,
  { rootMargin = '200px' }: { rootMargin?: string } = {},
): boolean {
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === 'undefined') {
      setInView(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true);
          observer.disconnect();
        }
      },
      { rootMargin },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, rootMargin]);

  return inView;
}
