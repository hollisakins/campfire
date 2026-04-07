/**
 * Generate a username from an email address.
 *
 * Takes the local part (before @), lowercases, replaces non-alphanumeric
 * characters with hyphens, and trims leading/trailing hyphens.
 *
 * Examples:
 *   "hollis.akins@example.com" → "hollis.akins"
 *   "CMCasey@university.edu"   → "cmcasey"
 *   "user+tag@example.com"     → "user-tag"
 */
export function usernameFromEmail(email: string): string {
  const local = email.split('@')[0] ?? email;
  const cleaned = local
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, '-')
    .replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, '')
    .slice(0, 40);
  // Constraint requires min 2 chars (start + end both alphanumeric)
  if (cleaned.length < 2) {
    return cleaned + '0';
  }
  return cleaned;
}
