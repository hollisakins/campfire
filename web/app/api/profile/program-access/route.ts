import { NextRequest, NextResponse } from 'next/server';
import { getRequestIdentity } from '@/lib/auth/identity';
import { checkUserProgramAccess } from '@/lib/api-helpers';

/**
 * POST /api/profile/program-access
 *
 * Check user's program access (proprietary + public)
 * Returns: { hasProprietaryAccess, grantedPrograms, publicPrograms }
 */
export async function POST(request: NextRequest) {
  const { user } = await getRequestIdentity();

  if (!user) {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }

  try {
    const programAccess = await checkUserProgramAccess(user.id);
    return NextResponse.json(programAccess);
  } catch (error) {
    console.error('Error checking program access:', error);
    return NextResponse.json({ error: 'Failed to check program access' }, { status: 500 });
  }
}
