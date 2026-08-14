'use client';

import React, { createContext, useContext, useEffect, useState } from 'react';
import { User, Session } from '@supabase/supabase-js';
import { createClient } from '@/lib/supabase/client';
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
  // scope 'local' signs out this browser only. It exists for share-link
  // sessions: one link account is shared by every holder of the link, so the
  // default global sign-out would revoke every other visitor's session too.
  signOut: (options?: { scope?: 'global' | 'local' }) => Promise<void>;
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

  const supabase = createClient();

  useEffect(() => {
    // Get initial session
    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session);
      setUser(session?.user ?? null);
      if (session?.user) {
        fetchUserProfile(session.user.id);
      } else {
        setLoading(false);
      }
    });

    // Listen for auth changes
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session);
      setUser(session?.user ?? null);
      if (session?.user) {
        fetchUserProfile(session.user.id);
      } else {
        setUserProfile(null);
        setLoading(false);
      }
    });

    return () => subscription.unsubscribe();
  }, []);

  const fetchUserProfile = async (userId: string) => {
    try {
      const { data, error } = await supabase
        .from('user_profiles')
        .select('*')
        .eq('user_id', userId)
        .single();

      if (error && error.code === 'PGRST116') {
        // Profile doesn't exist - user needs to complete setup via /welcome
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

      // Check program access after fetching profile
      await fetchProgramAccess();
    } catch (error) {
      console.error('Error fetching user profile:', error);
      setUserProfile(null);
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
      await fetchUserProfile(user.id);
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

  const signOut = async (options?: { scope?: 'global' | 'local' }) => {
    await supabase.auth.signOut({ scope: options?.scope ?? 'global' });
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
