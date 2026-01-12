'use client';

import React, { useEffect, useState } from 'react';
import type { TOCItem } from './MarkdownRenderer';

interface TableOfContentsProps {
  items: TOCItem[];
}

export default function TableOfContents({ items }: TableOfContentsProps) {
  const [activeId, setActiveId] = useState<string>('');

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            setActiveId(entry.target.id);
          }
        });
      },
      {
        rootMargin: '-80px 0px -80% 0px',
        threshold: 0,
      }
    );

    // Observe all heading elements
    items.forEach((item) => {
      const element = document.getElementById(item.id);
      if (element) {
        observer.observe(element);
      }
    });

    return () => observer.disconnect();
  }, [items]);

  if (items.length === 0) {
    return null;
  }

  return (
    <nav className="hidden xl:block sticky top-24 self-start w-56 flex-shrink-0 pl-8">
      <h4 className="text-sm font-semibold text-text-primary dark:text-slate-200 mb-3">
        On this page
      </h4>
      <ul className="space-y-2 text-sm">
        {items.map((item) => (
          <li
            key={item.id}
            style={{ paddingLeft: `${(item.level - 2) * 12}px` }}
          >
            <a
              href={`#${item.id}`}
              className={`
                block py-1 transition-colors
                ${activeId === item.id
                  ? 'text-primary font-medium'
                  : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                }
              `}
            >
              {item.text}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
