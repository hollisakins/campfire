export const USERNAME_REGEX = /^[a-z0-9][a-z0-9._-]{0,38}[a-z0-9]$/;

/**
 * Find a unique username by appending -2, -3, etc. until a free slot is found.
 * @param email - source email address
 * @param checkExists - async predicate, returns true if the username is already taken
 */
export async function generateUniqueUsername(
  email: string,
  checkExists: (username: string) => Promise<boolean>,
): Promise<string> {
  const base = usernameFromEmail(email);
  if (!(await checkExists(base))) return base;
  let suffix = 2;
  while (true) {
    const candidate = `${base}-${suffix}`;
    if (!(await checkExists(candidate))) return candidate;
    suffix++;
  }
}

export function usernameFromEmail(email: string): string {
  const local = email.split('@')[0] ?? email;
  const cleaned = local
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, '-')
    .replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, '')
    .slice(0, 40);
  // Constraint requires min 2 chars (start + end both alphanumeric)
  if (cleaned.length < 2) {
    return cleaned.padEnd(2, '0');
  }
  return cleaned;
}
