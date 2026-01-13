'use client';

import React, { useEffect, useState, useMemo } from 'react';
import type { TOCItem } from './MarkdownRenderer';

interface TableOfContentsProps {
  items: TOCItem[];
}

interface TOCGroup {
  parent: TOCItem;
  children: TOCItem[];
}

// Group items by their parent H2
function groupItemsByH2(items: TOCItem[]): TOCGroup[] {
  const groups: TOCGroup[] = [];
  let currentGroup: TOCGroup | null = null;

  for (const item of items) {
    if (item.level === 2) {
      // Start a new group for each H2
      currentGroup = { parent: item, children: [] };
      groups.push(currentGroup);
    } else if (currentGroup) {
      // Add H3/H4 to current group
      currentGroup.children.push(item);
    }
  }

  return groups;
}

// Find which H2 section contains a given heading ID
function findParentH2Id(activeId: string, groups: TOCGroup[]): string | null {
  for (const group of groups) {
    if (group.parent.id === activeId) {
      return group.parent.id;
    }
    if (group.children.some(child => child.id === activeId)) {
      return group.parent.id;
    }
  }
  return null;
}

// Format TOC item text - render code in monospace without backticks
function formatTocText(text: string): React.ReactNode {
  // Check if the entire text is wrapped in backticks
  if (text.startsWith('`') && text.endsWith('`')) {
    return <code className="font-mono">{text.slice(1, -1)}</code>;
  }

  // Check for inline code segments within the text
  const parts = text.split(/(`[^`]+`)/g);
  if (parts.length === 1) {
    return text;
  }

  return parts.map((part, i) => {
    if (part.startsWith('`') && part.endsWith('`')) {
      return <code key={i} className="font-mono">{part.slice(1, -1)}</code>;
    }
    return part;
  });
}

export default function TableOfContents({ items }: TableOfContentsProps) {
  const [activeId, setActiveId] = useState<string>('');

  const groups = useMemo(() => groupItemsByH2(items), [items]);
  const activeH2Id = useMemo(() => findParentH2Id(activeId, groups), [activeId, groups]);

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
    <nav className="hidden xl:block sticky top-24 self-start w-56 flex-shrink-0 pl-8 max-h-[calc(100vh-8rem)] overflow-y-auto">
      <h4 className="text-sm font-semibold text-text-primary dark:text-slate-200 mb-2">
        On this page
      </h4>
      <ul className="space-y-0.5 text-sm">
        {groups.map((group) => (
          <li key={group.parent.id}>
            {/* H2 heading - always visible */}
            <a
              href={`#${group.parent.id}`}
              className={`
                block py-0.5 transition-colors
                ${activeId === group.parent.id
                  ? 'text-primary font-medium'
                  : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                }
              `}
            >
              {formatTocText(group.parent.text)}
            </a>

            {/* Subsections - only visible when this section is active */}
            {activeH2Id === group.parent.id && group.children.length > 0 && (
              <ul className="ml-3 mt-0.5 space-y-0.5 border-l border-border dark:border-slate-700 pl-2">
                {group.children.map((child) => (
                  <li key={child.id}>
                    <a
                      href={`#${child.id}`}
                      className={`
                        block py-0.5 transition-colors text-[13px]
                        ${activeId === child.id
                          ? 'text-primary font-medium'
                          : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                        }
                      `}
                    >
                      {formatTocText(child.text)}
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
