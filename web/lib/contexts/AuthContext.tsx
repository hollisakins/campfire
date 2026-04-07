'use client';

import React, { createContext, useContext, useEffect, useState } from 'react';
import { User, Session } from '@supabase/supabase-js';
import { createClient } from '@/lib/supabase/client';
import { UserProfile } from '@/lib/types';
import { usernameFromEmail } from '@/lib/utils/username';

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
  programAccess: ProgramAccessInfo | null;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  signUp: (email: string, password: string, fullName: string) => Promise<{ error: Error | null }>;
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
      // Create auth user
      const { data, error } = await supabase.auth.signUp({
        email,
        password,
      });

      if (error) throw error;

      // Create user profile
      if (data.user) {
        const username = usernameFromEmail(email);
        const { error: profileError } = await supabase
          .from('user_profiles')
          .insert({
            user_id: data.user.id,
            username,
            full_name: fullName,
            is_group_account: false,
            can_comment: true,
            is_admin: false,
          });

        if (profileError) {
          console.error('Error creating user profile:', profileError);
          // Don't throw - auth user was created successfully
        }
      }

      return { error: null };
    } catch (error) {
      return { error: error as Error };
    }
  };

  const signOut = async () => {
    await supabase.auth.signOut();
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
