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

// The nav a share-link visitor sees (docs/design-public-mirror.md §7).
//
// Everything that would dead-end is gone: no nav links (every destination
// renders empty for them), no profile, no admin, no sign-in. What remains is
// enough to know where you are and to read the page comfortably -- the
// wordmark, the scope name, and the theme toggle.
//
// The wordmark does what a wordmark always does -- goes home -- but it exits
// the shared view on the way: sign out of the link account, then land on the
// public home page. Without that escape hatch there is no way out of a shared
// view at all, because share-link sessions ride the same cookies as a normal
// login: anyone with a real account who opens a share link (an admin testing
// their own link, say) is silently signed OUT of that account, and this nav
// strips every other route to a sign-in. Navigating home while still signed in
// as the link account would be worse than useless -- home renders empty for
// link accounts -- so the sign-out is what makes the logo do the expected
// thing.
const SharedViewNav: React.FC<{
  scopeLabel: string | null;
  theme: string;
  ThemeIcon: React.ElementType;
  onCycleTheme: () => void;
  onExit: () => void;
}> = ({ scopeLabel, theme, ThemeIcon, onCycleTheme, onExit }) => (
  <nav data-slot="app-header" className="bg-header text-header-foreground shadow-md">
    <div className="container mx-auto px-4 py-4">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center space-x-2 min-w-0">
          <button
            onClick={onExit}
            className="flex items-center space-x-2 hover:opacity-80 transition-opacity"
            title="Leave this shared view"
          >
            <Logo size={32} title="" aria-hidden />
            <span className="text-xl font-bold">CAMPFIRE</span>
          </button>
          {scopeLabel && (
            <span className="hidden sm:inline text-sm text-header-muted truncate border-l border-header-border pl-2 ml-2">
              Shared view · <span className="font-medium">{scopeLabel}</span>
            </span>
          )}
        </div>

        <button
          onClick={onCycleTheme}
          className="flex items-center text-header-muted hover:text-header-foreground transition-colors"
          aria-label={`Current theme: ${theme}. Click to change.`}
          title={`Theme: ${theme}`}
        >
          <ThemeIcon className="w-4 h-4" />
        </button>
      </div>

      {scopeLabel && (
        <div className="sm:hidden mt-2 text-sm text-header-muted truncate">
          Shared view · <span className="font-medium">{scopeLabel}</span>
        </div>
      )}
    </div>
  </nav>
);

export const Navigation: React.FC = () => {
  const pathname = usePathname();
  const router = useRouter();
  const { user, userProfile, signOut, isLinkAccount } = useAuth();
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

  // The dead-link page (/s/inactive) stands alone. By the time someone lands
  // there they have no access to anything, so a full nav would offer a menu of
  // pages that all render empty -- the exact confusion the stripped nav exists
  // to avoid. It carries its own wordmark.
  if (pathname === '/s/inactive') return null;

  // The link account's profile carries the share label as its full_name, so the
  // scope is named without a second fetch.
  if (isLinkAccount) {
    return (
      <SharedViewNav
        scopeLabel={userProfile?.full_name ?? null}
        theme={theme}
        ThemeIcon={ThemeIcon}
        onCycleTheme={cycleTheme}
        onExit={async () => {
          // scope 'local': the link account is shared by everyone holding this
          // link, so a global sign-out would revoke their sessions too.
          await signOut({ scope: 'local' });
          router.push('/');
        }}
      />
    );
  }

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
