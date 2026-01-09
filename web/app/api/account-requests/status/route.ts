import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

/**
 * GET /api/account-requests/status?email=user@example.com
 *
 * Check the status of an account request (public endpoint)
 * Returns: { status: 'pending' | 'approved' | 'rejected' | 'not_found', created_at?: string }
 */
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const email = searchParams.get('email');

  // Validate email
  if (!email || typeof email !== 'string' || !email.includes('@')) {
    return NextResponse.json(
      { error: 'Valid email is required' },
      { status: 400 }
    );
  }

  const normalizedEmail = email.trim().toLowerCase();

  try {
    const supabase = await createClient();

    // Look up the request by email
    const { data: accountRequest, error } = await supabase
      .from('account_requests')
      .select('status, created_at, reviewed_at')
      .eq('email', normalizedEmail)
      .single();

    if (error || !accountRequest) {
      return NextResponse.json({
        status: 'not_found',
        message: 'No request found for this email address.',
      });
    }

    // Return appropriate message based on status
    let message: string;
    switch (accountRequest.status) {
      case 'pending':
        message = 'Your request is pending review. We\'ll send you an email when it\'s approved.';
        break;
      case 'approved':
        message = 'Your request was approved! Please check your email for instructions to complete your account setup.';
        break;
      case 'rejected':
        message = 'Your request was not approved. Please contact the administrator for more information.';
        break;
      default:
        message = 'Unknown status.';
    }

    return NextResponse.json({
      status: accountRequest.status,
      message,
      created_at: accountRequest.created_at,
      reviewed_at: accountRequest.reviewed_at,
    });

  } catch (error) {
    console.error('Error checking request status:', error);
    return NextResponse.json(
      { error: 'An unexpected error occurred. Please try again.' },
      { status: 500 }
    );
  }
}
