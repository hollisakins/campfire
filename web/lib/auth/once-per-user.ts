/**
 * Runs an async load at most once per user id, sharing the in-flight promise
 * with any concurrent caller for the same id.
 *
 * Why: at boot the browser Supabase client emits `SIGNED_IN` from its session
 * recovery step *before* `getSession()` resolves, so `AuthContext` had two
 * triggers — the auth-state subscriber and the `getSession().then` path —
 * both asking for the profile (and then program access) 1 ms apart, on every
 * page load, for every signed-in user (#539). Funnelling both through one
 * per-user gate makes the second call a no-op without either caller needing
 * to know about the other.
 *
 * `force` re-runs for the same id (a `USER_UPDATED` event, an explicit refresh);
 * `reset()` forgets the current id so the next call runs again (a missing
 * profile row that /welcome is about to create). A rejected load also forgets
 * the id, so a transient failure is retried by the next auth event instead of
 * being deduped away.
 */
export type OncePerUser<T> = ReturnType<typeof createOncePerUser<T>>;

export function createOncePerUser<T>(load: (userId: string) => Promise<T>) {
  let current: { userId: string; promise: Promise<T> } | null = null;

  return {
    run(userId: string, { force = false }: { force?: boolean } = {}): Promise<T> {
      if (!force && current && current.userId === userId) return current.promise;
      const entry = { userId, promise: undefined as unknown as Promise<T> };
      entry.promise = load(userId).then(
        (value) => value,
        (error) => {
          if (current === entry) current = null;
          throw error;
        }
      );
      current = entry;
      return entry.promise;
    },
    /** The user id the last run was for, or null. */
    get userId(): string | null {
      return current?.userId ?? null;
    },
    /** Forget the current id so the next `run` for it loads again. */
    reset(userId?: string) {
      if (userId === undefined || current?.userId === userId) current = null;
    },
  };
}
