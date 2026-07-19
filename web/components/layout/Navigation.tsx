'use client';

import React, { useState, useRef, useEffect } from 'react';
import Link from 'next/link';
import { SignInLink } from '@/components/auth/SignInLink';
import { usePathname, useRouter } from 'next/navigation';
import { LogOut, User, Shield, Sun, Moon, Monitor, ChevronDown, Github, Menu, X } from 'lucide-react';
import { Logo } from '@/components/brand/Logo';
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
            ? 'text-header-foreground border-primary'
            : 'text-header-muted border-transparent hover:text-header-foreground hover:border-header-border'
          }
        `}
      >
        {link.label}
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-2 w-44 bg-header-elevated rounded-lg shadow-lg border border-header-border py-1 z-[1100]">
          {link.children!.map((child) => (
            <Link
              key={child.href}
              href={child.href}
              onClick={() => setOpen(false)}
              className="block px-4 py-2 text-sm text-header-muted hover:text-header-foreground hover:bg-header-hover transition-colors"
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
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close the mobile menu on navigation.
  useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

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

  const mobileLinkClass = (active: boolean) => `
    block px-3 py-2 rounded-lg text-sm font-medium transition-colors
    ${active
      ? 'text-header-foreground bg-header-hover'
      : 'text-header-muted hover:text-header-foreground hover:bg-header-hover'
    }
  `;

  return (
    <nav data-slot="app-header" className="bg-header text-header-foreground shadow-md">
      <div className="container mx-auto px-4 py-4">
        <div className="flex items-center justify-between">
          {/* Logo — mark is decorative; the CAMPFIRE wordmark names the link */}
          <Link href="/" className="flex items-center space-x-2 hover:opacity-80 transition-opacity">
            <Logo size={32} title="" aria-hidden />
            <span className="text-xl font-bold">CAMPFIRE</span>
          </Link>

          {/* Navigation Links (desktop) */}
          <div className="hidden md:flex items-center space-x-8">
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
                      ? 'text-header-foreground border-primary'
                      : 'text-header-muted border-transparent hover:text-header-foreground hover:border-header-border'
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
              className="flex items-center space-x-1 text-sm text-header-muted hover:text-header-foreground transition-colors"
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
              className="flex items-center text-sm text-header-muted hover:text-header-foreground transition-colors"
              aria-label="View on GitHub"
              title="View on GitHub"
            >
              <Github className="w-4 h-4" />
            </a>

            {/* User Menu */}
            {user ? (
              <div className="flex items-center space-x-4 ml-4 pl-4 border-l border-header-border">
                {userProfile?.is_admin && (
                  <Link
                    href="/admin"
                    className="flex items-center space-x-1 text-sm text-header-muted hover:text-header-foreground transition-colors"
                  >
                    <Shield className="w-4 h-4" />
                    <span>Admin</span>
                  </Link>
                )}
                <Link
                  href="/profile"
                  className="flex items-center space-x-2 text-sm text-header-muted hover:text-header-foreground transition-colors"
                >
                  <User className="w-4 h-4" />
                  <span>{userProfile?.full_name || user.email}</span>
                </Link>
                <button
                  onClick={handleSignOut}
                  className="flex items-center space-x-1 text-sm text-header-muted hover:text-header-foreground transition-colors"
                  aria-label="Sign out"
                >
                  <LogOut className="w-4 h-4" />
                </button>
              </div>
            ) : (
              <SignInLink
                className="text-sm font-medium text-header-muted hover:text-header-foreground transition-colors ml-4 pl-4 border-l border-header-border"
              >
                Sign In
              </SignInLink>
            )}
          </div>

          {/* Mobile controls */}
          <div className="flex md:hidden items-center space-x-3">
            <button
              onClick={cycleTheme}
              className="flex items-center text-header-muted hover:text-header-foreground transition-colors"
              aria-label={`Current theme: ${theme}. Click to change.`}
              title={`Theme: ${theme}`}
            >
              <ThemeIcon className="w-5 h-5" />
            </button>
            <button
              onClick={() => setMobileOpen(!mobileOpen)}
              className="flex items-center text-header-muted hover:text-header-foreground transition-colors"
              aria-label={mobileOpen ? 'Close menu' : 'Open menu'}
              aria-expanded={mobileOpen}
            >
              {mobileOpen ? <X className="w-6 h-6" /> : <Menu className="w-6 h-6" />}
            </button>
          </div>
        </div>

        {/* Mobile menu */}
        {mobileOpen && (
          <div className="md:hidden mt-4 pt-4 border-t border-header-border space-y-1">
            {navLinks.map((link) =>
              link.children ? (
                <div key={link.href}>
                  {link.children.map((child, i) => (
                    <Link
                      key={child.href}
                      href={child.href}
                      className={mobileLinkClass(isActive(link.href) && i === 0)}
                    >
                      {i === 0 ? link.label : `${link.label} · ${child.label}`}
                    </Link>
                  ))}
                </div>
              ) : (
                <Link key={link.href} href={link.href} className={mobileLinkClass(isActive(link.href))}>
                  {link.label}
                </Link>
              )
            )}

            <div className="pt-2 mt-2 border-t border-header-border space-y-1">
              {user ? (
                <>
                  {userProfile?.is_admin && (
                    <Link href="/admin" className={mobileLinkClass(isActive('/admin'))}>
                      <span className="flex items-center gap-2"><Shield className="w-4 h-4" /> Admin</span>
                    </Link>
                  )}
                  <Link href="/profile" className={mobileLinkClass(isActive('/profile'))}>
                    <span className="flex items-center gap-2">
                      <User className="w-4 h-4" /> {userProfile?.full_name || user.email}
                    </span>
                  </Link>
                  <button onClick={handleSignOut} className={`w-full text-left ${mobileLinkClass(false)}`}>
                    <span className="flex items-center gap-2"><LogOut className="w-4 h-4" /> Sign out</span>
                  </button>
                </>
              ) : (
                <SignInLink className={mobileLinkClass(false)}>Sign In</SignInLink>
              )}
              <a
                href="https://github.com/hollisakins/campfire"
                target="_blank"
                rel="noopener noreferrer"
                className={mobileLinkClass(false)}
              >
                <span className="flex items-center gap-2"><Github className="w-4 h-4" /> GitHub</span>
              </a>
            </div>
          </div>
        )}
      </div>
    </nav>
  );
};
