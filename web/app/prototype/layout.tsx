'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useState, useEffect } from 'react';
import { Sun, Moon, ArrowLeft } from 'lucide-react';

/**
 * Prototype layout - simplified version without auth
 * Provides navigation between prototype pages and theme toggle
 */
export default function PrototypeLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const [isDark, setIsDark] = useState(false);

  // Check initial theme
  useEffect(() => {
    setIsDark(document.documentElement.classList.contains('dark'));
  }, []);

  const toggleTheme = () => {
    document.documentElement.classList.toggle('dark');
    setIsDark(!isDark);
  };

  const navItems = [
    { href: '/prototype', label: 'Overview' },
    { href: '/prototype/overflow-panel', label: 'Live Demo' },
  ];

  return (
    <div className="min-h-screen bg-background dark:bg-slate-900">
      {/* Header */}
      <header className="border-b border-border dark:border-slate-700 bg-card dark:bg-slate-800">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-14">
            {/* Left: Back to main app */}
            <div className="flex items-center gap-4">
              <Link
                href="/nirspec"
                className="flex items-center gap-2 text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
              >
                <ArrowLeft className="w-4 h-4" />
                <span className="text-sm">Back to CAMPFIRE</span>
              </Link>
              <div className="h-6 w-px bg-border dark:bg-slate-700" />
              <span className="text-lg font-semibold text-text-primary dark:text-slate-100">
                Filter UI Prototype
              </span>
            </div>

            {/* Right: Theme toggle */}
            <button
              onClick={toggleTheme}
              className="p-2 rounded-lg text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
              title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {isDark ? <Sun className="w-5 h-5" /> : <Moon className="w-5 h-5" />}
            </button>
          </div>
        </div>
      </header>

      {/* Navigation tabs */}
      <nav className="border-b border-border dark:border-slate-700 bg-card/50 dark:bg-slate-800/50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex gap-1 overflow-x-auto py-2">
            {navItems.map((item) => {
              const isActive = pathname === item.href;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`
                    px-4 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors
                    ${isActive
                      ? 'bg-primary/10 text-primary dark:bg-primary/20'
                      : 'text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-200'
                    }
                  `}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>
        </div>
      </nav>

      {/* Main content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {children}
      </main>
    </div>
  );
}
