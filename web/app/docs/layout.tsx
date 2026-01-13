'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { BookOpen, ChevronDown, ChevronRight, Menu, X } from 'lucide-react';
import { docsNav, type DocPage } from '@/lib/docs/config';

function NavItem({ item, level = 0 }: { item: DocPage; level?: number }) {
  const pathname = usePathname();
  const currentSlug = pathname.replace('/docs/', '').replace('/docs', '') || 'overview';
  const isActive = currentSlug === item.slug;
  const hasChildren = item.children && item.children.length > 0;
  const isParentOfActive = hasChildren && item.children?.some(child => child.slug === currentSlug);
  const [isOpen, setIsOpen] = useState(isParentOfActive || isActive);

  const Icon = item.icon;

  if (hasChildren) {
    return (
      <div>
        <button
          onClick={() => setIsOpen(!isOpen)}
          className={`
            w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg text-left transition-colors
            ${isActive || isParentOfActive
              ? 'bg-primary/10 text-primary dark:bg-primary/20'
              : 'text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-800 hover:text-text-primary dark:hover:text-slate-200'
            }
          `}
        >
          <span className="flex items-center gap-2">
            {Icon && <Icon className="w-4 h-4" />}
            <span className="font-medium">{item.title}</span>
          </span>
          {isOpen ? (
            <ChevronDown className="w-4 h-4" />
          ) : (
            <ChevronRight className="w-4 h-4" />
          )}
        </button>
        {isOpen && (
          <div className="ml-4 mt-1 space-y-1 border-l border-border dark:border-slate-700 pl-3">
            {item.children?.map((child) => (
              <NavItem key={child.slug} item={child} level={level + 1} />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <Link
      href={`/docs/${item.slug}`}
      className={`
        flex items-center gap-2 px-3 py-2 rounded-lg transition-colors
        ${isActive
          ? 'bg-primary text-white'
          : 'text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-800 hover:text-text-primary dark:hover:text-slate-200'
        }
      `}
    >
      {Icon && <Icon className="w-4 h-4" />}
      <span className={level === 0 ? 'font-medium' : ''}>{item.title}</span>
    </Link>
  );
}

export default function DocsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      {/* Mobile sidebar toggle */}
      <button
        onClick={() => setSidebarOpen(!sidebarOpen)}
        className="lg:hidden fixed bottom-4 right-4 z-50 p-3 bg-primary text-white rounded-full shadow-lg hover:bg-primary-hover transition-colors"
        aria-label="Toggle navigation"
      >
        {sidebarOpen ? <X className="w-6 h-6" /> : <Menu className="w-6 h-6" />}
      </button>

      <div className="flex gap-8">
        {/* Sidebar */}
        <aside
          className={`
            fixed inset-0 z-40
            lg:sticky lg:top-24 lg:self-start lg:z-0
            lg:max-h-[calc(100vh-8rem)] lg:overflow-y-auto
            w-64 lg:w-56 flex-shrink-0
            bg-background dark:bg-slate-900 lg:bg-transparent
            transform transition-transform duration-200 ease-in-out
            ${sidebarOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0'}
            overflow-y-auto
            p-4 lg:p-0
          `}
        >
          {/* Mobile close button area */}
          <div className="lg:hidden flex justify-end mb-4">
            <button
              onClick={() => setSidebarOpen(false)}
              className="p-2 text-text-secondary hover:text-text-primary"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="flex items-center gap-2 mb-6">
            <BookOpen className="w-6 h-6 text-primary" />
            <h1 className="text-xl font-semibold text-text-primary dark:text-slate-100">
              Documentation
            </h1>
          </div>

          <nav className="space-y-1">
            {docsNav.map((item) => (
              <NavItem key={item.slug} item={item} />
            ))}
          </nav>
        </aside>

        {/* Backdrop for mobile */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 bg-black/50 z-30 lg:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Main Content */}
        <main className="flex-1 min-w-0 max-w-4xl">
          {children}
        </main>
      </div>
    </div>
  );
}
