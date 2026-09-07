'use client';

import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { User, Session } from '@supabase/supabase-js';
import { useQueryClient } from '@tanstack/react-query';
import { createClient } from '@/lib/supabase/client';
import { createOncePerUser, type OncePerUser } from '@/lib/auth/once-per-user';
import { installAuthChannelBfcacheGuard } from '@/lib/supabase/bfcache-auth-channel';
import { UserProfile } from '@/lib/types';
import { generateUniqueUsername } from '@/lib/utils/username';

interface ProgramAccessInfo {
  hasProprietaryAccess: boolean;
  grantedPrograms: number[];
  publicPrograms: number[];
}

interface AuthContextType {
  user: User | null;
  userProfile: UserProfile | null;
  session: Session | null;
  loading: boolean;
  needsProfileSetup: boolean; // True if user is authenticated but has no profile
  needsAccessCode: boolean; // True if user has no proprietary program access
  // True when this session came from a share link. Drives the stripped nav and
  // suppresses account-oriented UI the visitor has no account for.
  isLinkAccount: boolean;
  programAccess: ProgramAccessInfo | null;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  signUp: (
    email: string,
    password: string,
    fullName: string
  ) => Promise<{ error: Error | null; existingAccount?: boolean }>;
  signOut: () => Promise<void>;
  refreshProfile: () => Promise<void>; // Manually refresh profile after setup
  checkProgramAccess: () => Promise<void>; // Refresh program access state
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [userProfile, setUserProfile] = useState<UserProfile | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsProfileSetup, setNeedsProfileSetup] = useState(false);
  const [needsAccessCode, setNeedsAccessCode] = useState(false);
  const [programAccess, setProgramAccess] = useState<ProgramAccessInfo | null>(null);

  // Module singleton (lib/supabase/client.ts) — stable across renders.
  const supabase = createClient();
  const queryClient = useQueryClient();

  // The TanStack cache outlives a sign-out / sign-in on the same tab, and the
  // read queries are keyed on what they fetch, never on the viewer (#506) —
  // so the cache is cleared whenever the signed-in user changes, including to
  // nobody. Not on the initial null → user transition at boot: nothing
  // viewer-specific has been fetched yet, and clearing would cancel the
  // queries the page just started.
  const cacheUserIdRef = useRef<string | null>(null);
  // Synchronous mirror of the user id for the bfcache guard (#540), written
  // before the React state update lands: `undefined` until the initial
  // session load completes, then the id or `null` for signed out.
  const userIdRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const id = user?.id ?? null;
    if (cacheUserIdRef.current !== null && cacheUserIdRef.current !== id) {
      queryClient.clear();
    }
    cacheUserIdRef.current = id;
  }, [user?.id, queryClient]);
  // The profile chain (user_profiles, then program-access in the background)
  // has two triggers at boot: `getSession().then`, and the `SIGNED_IN` event
  // auth-js emits from its session-recovery step *before* `getSession()`
  // resolves (`INITIAL_SESSION` is a third, ignored below). #499 deduped the
  // subscriber by user id but left the `getSession()` path unguarded, so
  // every signed-in page load still ran the chain twice, 1 ms apart (#539).
  // Both triggers now go through one per-user gate: the second call for the
  // same id shares the first's promise, and TOKEN_REFRESHED stays a no-op.
  const profileGateRef = useRef<OncePerUser<void> | null>(null);
  const profileGate = () => (profileGateRef.current ??= createOncePerUser(loadUserProfile));

  useEffect(() => {
    // Get initial session
    supabase.auth.getSession().then(({ data: { session } }) => {
      userIdRef.current = session?.user?.id ?? null;
      setSession(session);
      setUser(session?.user ?? null);
      if (session?.user) {
        void profileGate().run(session.user.id);
      } else {
        setLoading(false);
      }
    });

    // Listen for auth changes
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === 'INITIAL_SESSION') return; // handled by getSession() above
      userIdRef.current = session?.user?.id ?? null;
      setSession(session);
      setUser(session?.user ?? null);
      if (session?.user) {
        // USER_UPDATED re-reads even for the same user; anything else for the
        // user already loaded (SIGNED_IN at boot, TOKEN_REFRESHED) is a no-op.
        void profileGate().run(session.user.id, { force: event === 'USER_UPDATED' });
      } else {
        profileGate().reset();
        setUserProfile(null);
        setLoading(false);
      }
    });

    // Keep this page restorable from bfcache (#540): the auth client's
    // cross-tab channel is closed while the page is parked and reopened on
    // return, and the stored session is re-checked against the user this
    // page was showing when it parked. `undefined` before the boot above has
    // settled tells the guard to leave that pending load to finish instead.
    const uninstallBfcacheGuard = installAuthChannelBfcacheGuard(supabase, {
      getUserId: () => userIdRef.current,
      onIdentityChanged: (current) => {
        // The server-rendered, access-scoped content on screen belongs to the
        // user this page parked with, so the page reloads. Cached reads and
        // the profile gate are dropped first, and a sign-out clears the auth
        // state too, so the tab is not left claiming a session it no longer
        // has if the reload is refused (a dirty-editor `beforeunload` prompt).
        // A switch to another user leaves the state auth-js already moved.
        queryClient.clear();
        profileGate().reset();
        if (!current) {
          userIdRef.current = null;
          setSession(null);
          setUser(null);
          setUserProfile(null);
          setProgramAccess(null);
          setNeedsProfileSetup(false);
          setNeedsAccessCode(false);
          setLoading(false);
        }
        window.location.reload();
      },
    });

    return () => {
      subscription.unsubscribe();
      uninstallBfcacheGuard();
    };
  }, []);

  // Only ever called through `profileGate()`.
  const loadUserProfile = async (userId: string) => {
    try {
      const { data, error } = await supabase
        .from('user_profiles')
        .select('*')
        .eq('user_id', userId)
        .single();

      if (error && error.code === 'PGRST116') {
        // Profile doesn't exist - user needs to complete setup via /welcome.
        // Not a fetched profile: forget the id so the next auth event after
        // /welcome creates the row refetches instead of being deduped.
        profileGate().reset(userId);
        setUserProfile(null);
        setNeedsProfileSetup(true);
        return;
      }

      if (error) throw error;
      setUserProfile(data);
      setNeedsProfileSetup(false);

      // Share links (docs/design-public-mirror.md §7): a link account has no
      // program grants by design -- its scope comes from share_links, not
      // user_program_access -- so the access-code prompt would fire on every
      // shared view and send the visitor to a /profile page they cannot use.
      // Skip the whole check for them.
      if (data?.is_link_account) {
        setProgramAccess(null);
        setNeedsAccessCode(false);
        return;
      }

      // Program access only drives the access-code prompt (needsAccessCode /
      // programAccess); no science query waits on it. Resolve it in the
      // background so `loading` clears — and the list/object queries start —
      // as soon as the session and profile are known (#499).
      void fetchProgramAccess();
    } catch (error) {
      console.error('Error fetching user profile:', error);
      setUserProfile(null);
      // Not fetched after all: let the next auth event for this user (e.g.
      // TOKEN_REFRESHED on tab refocus) retry instead of being deduped away.
      profileGate().reset(userId);
    } finally {
      setLoading(false);
    }
  };

  const fetchProgramAccess = async () => {
    try {
      const response = await fetch('/api/profile/program-access', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });

      if (!response.ok) {
        throw new Error('Failed to fetch program access');
      }

      const data = await response.json();
      setProgramAccess(data);
      setNeedsAccessCode(!data.hasProprietaryAccess);
    } catch (error) {
      console.error('Error fetching program access:', error);
      setProgramAccess(null);
      setNeedsAccessCode(false);
    }
  };

  const checkProgramAccess = async () => {
    if (user) {
      await fetchProgramAccess();
    }
  };

  const refreshProfile = async () => {
    if (user) {
      setLoading(true);
      await profileGate().run(user.id, { force: true });
    }
  };

  const signIn = async (email: string, password: string) => {
    try {
      const { error } = await supabase.auth.signInWithPassword({
        email,
        password,
      });

      if (error) throw error;
      return { error: null };
    } catch (error) {
      return { error: error as Error };
    }
  };

  const signUp = async (email: string, password: string, fullName: string) => {
    try {
      // Suggest a username from the email; the handle_new_user DB trigger uses
      // this (de-duplicating if needed) when it provisions the profile. Email
      // confirmation is required, so there is no session yet — the profile is
      // created server-side by the trigger, not here.
      const username = await generateUniqueUsername(email, async (u) => {
        const { data: existing } = await supabase
          .from('user_profiles')
          .select('user_id')
          .eq('username', u)
          .maybeSingle();
        return !!existing;
      });

      const { data, error } = await supabase.auth.signUp({
        email,
        password,
        options: {
          // self_signup gates the handle_new_user trigger so it only fires for
          // open registrations (not admin invites). full_name/username seed the
          // auto-provisioned profile.
          data: {
            self_signup: 'true',
            full_name: fullName,
            username,
          },
          emailRedirectTo:
            typeof window !== 'undefined'
              ? `${window.location.origin}/login`
              : undefined,
        },
      });

      if (error) throw error;

      // With email confirmation enabled, Supabase obfuscates duplicate-email
      // signups: it returns a fake user with no identities instead of an error.
      // No confirmation email will arrive, so surface it rather than showing a
      // false "check your inbox" success.
      const existingAccount = !!data.user && (data.user.identities?.length ?? 0) === 0;

      return { error: null, existingAccount };
    } catch (error) {
      return { error: error as Error };
    }
  };

  const signOut = async () => {
    await supabase.auth.signOut();
    // Drop every cached read now, before the SIGNED_OUT event lands (the
    // effect above also fires on the user change, harmlessly twice).
    queryClient.clear();
    profileGate().reset();
    setNeedsProfileSetup(false);
    setNeedsAccessCode(false);
    setProgramAccess(null);
  };

  const value = {
    user,
    userProfile,
    session,
    loading,
    needsProfileSetup,
    needsAccessCode,
    isLinkAccount: userProfile?.is_link_account === true,
    programAccess,
    signIn,
    signUp,
    signOut,
    refreshProfile,
    checkProgramAccess,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
