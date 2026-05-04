'use client';

import React, { useState, useRef, useEffect } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Flame, LogOut, User, Shield, Sun, Moon, Monitor, ChevronDown, Github } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import { useTheme } from '@/lib/contexts/ThemeContext';

type NavLink = { href: string; label: string; children?: { href: string; label: string }[] };

function NavDropdown({ link, isActive }: { link: NavLink; isActive: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={`
          flex items-center gap-1 text-sm font-medium transition-colors pb-1 border-b-2
          ${isActive
            ? 'text-white border-primary'
            : 'text-gray-300 border-transparent hover:text-white hover:border-gray-400'
          }
        `}
      >
        {link.label}
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-2 w-44 bg-header dark:bg-slate-800 rounded-lg shadow-lg border border-gray-700 dark:border-slate-700 py-1 z-[1100]">
          {link.children!.map((child) => (
            <Link
              key={child.href}
              href={child.href}
              onClick={() => setOpen(false)}
              className="block px-4 py-2 text-sm text-gray-300 hover:text-white hover:bg-white/10 transition-colors"
            >
              {child.label}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export const Navigation: React.FC = () => {
  const pathname = usePathname();
  const router = useRouter();
  const { user, userProfile, signOut } = useAuth();
  const { theme, setTheme } = useTheme();

  const cycleTheme = () => {
    const themes: Array<'light' | 'dark' | 'system'> = ['light', 'system', 'dark'];
    const currentIndex = themes.indexOf(theme);
    const nextIndex = (currentIndex + 1) % themes.length;
    setTheme(themes[nextIndex]);
  };

  const ThemeIcon = theme === 'dark' ? Moon : theme === 'light' ? Sun : Monitor;

  const isActive = (path: string) => {
    if (path === '/') return pathname === '/';
    return pathname.startsWith(path);
  };

  const navLinks: NavLink[] = [
    { href: '/', label: 'Home' },
    { href: '/nircam', label: 'NIRCam' },
    {
      href: '/nirspec', label: 'NIRSpec', children: [
        { href: '/nirspec', label: 'Catalog' },
        { href: '/nirspec/tags', label: 'Tags' },
        { href: '/nirspec/metadata', label: 'Metadata' },
      ],
    },
    { href: '/map', label: 'Map' },
    { href: '/docs', label: 'Docs' },
  ];

  const handleSignOut = async () => {
    await signOut();
    router.push('/login');
  };

  return (
    <nav className="bg-header dark:bg-slate-900 text-white shadow-md">
      <div className="container mx-auto px-4 py-4">
        <div className="flex items-center justify-between">
          {/* Logo */}
          <Link href="/" className="flex items-center space-x-2 hover:opacity-80 transition-opacity">
            <Flame className="w-8 h-8 text-primary" />
            <span className="text-xl font-bold">CAMPFIRE</span>
          </Link>

          {/* Navigation Links */}
          <div className="flex items-center space-x-8">
            {navLinks.map((link) =>
              link.children ? (
                <NavDropdown key={link.href} link={link} isActive={isActive(link.href)} />
              ) : (
                <Link
                  key={link.href}
                  href={link.href}
                  className={`
                    text-sm font-medium transition-colors pb-1 border-b-2
                    ${isActive(link.href)
                      ? 'text-white border-primary'
                      : 'text-gray-300 border-transparent hover:text-white hover:border-gray-400'
                    }
                  `}
                >
                  {link.label}
                </Link>
              )
            )}

            {/* Theme Toggle */}
            <button
              onClick={cycleTheme}
              className="flex items-center space-x-1 text-sm text-gray-300 hover:text-white transition-colors"
              aria-label={`Current theme: ${theme}. Click to change.`}
              title={`Theme: ${theme}`}
            >
              <ThemeIcon className="w-4 h-4" />
            </button>

            {/* GitHub */}
            <a
              href="https://github.com/hollisakins/campfire"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center text-sm text-gray-300 hover:text-white transition-colors"
              aria-label="View on GitHub"
              title="View on GitHub"
            >
              <Github className="w-4 h-4" />
            </a>

            {/* User Menu */}
            {user ? (
              <div className="flex items-center space-x-4 ml-4 pl-4 border-l border-gray-600 dark:border-slate-700">
                {userProfile?.is_admin && (
                  <Link
                    href="/admin"
                    className="flex items-center space-x-1 text-sm text-gray-300 hover:text-white transition-colors"
                  >
                    <Shield className="w-4 h-4" />
                    <span>Admin</span>
                  </Link>
                )}
                <Link
                  href="/profile"
                  className="flex items-center space-x-2 text-sm text-gray-300 hover:text-white transition-colors"
                >
                  <User className="w-4 h-4" />
                  <span>{userProfile?.full_name || user.email}</span>
                </Link>
                <button
                  onClick={handleSignOut}
                  className="flex items-center space-x-1 text-sm text-gray-300 hover:text-white transition-colors"
                  aria-label="Sign out"
                >
                  <LogOut className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <Link
                href="/login"
                className="text-sm font-medium text-gray-300 hover:text-white transition-colors ml-4 pl-4 border-l border-gray-600 dark:border-slate-700"
              >
                Sign In
              </Link>
            )}
          </div>
        </div>
      </div>
    </nav>
  );
};
