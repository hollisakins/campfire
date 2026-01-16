import { NextRequest, NextResponse } from 'next/server';
import { createClient, createServiceClient } from '@/lib/supabase/server';
import { sendAdminNotification } from '@/lib/email/resend';

/**
 * POST /api/account-requests
 *
 * Submit a new account request (public endpoint)
 * Body: { email: string, full_name: string }
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { email, full_name } = body;

    // Validate required fields
    if (!email || typeof email !== 'string' || !email.includes('@')) {
      return NextResponse.json(
        { error: 'Valid email is required' },
        { status: 400 }
      );
    }

    if (!full_name || typeof full_name !== 'string' || full_name.trim().length < 2) {
      return NextResponse.json(
        { error: 'Full name is required (at least 2 characters)' },
        { status: 400 }
      );
    }

    const normalizedEmail = email.trim().toLowerCase();
    const trimmedName = full_name.trim();

    // Use service client for admin operations
    const serviceClient = createServiceClient();

    // Check if user already exists in Supabase Auth
    const { data: existingUsers } = await serviceClient.auth.admin.listUsers();
    const existingUser = existingUsers?.users?.find(
      u => u.email?.toLowerCase() === normalizedEmail
    );

    if (existingUser) {
      return NextResponse.json(
        { error: 'An account with this email already exists. Please sign in instead.' },
        { status: 409 }
      );
    }

    // Check for existing request
    const supabase = await createClient();
    const { data: existingRequest } = await supabase
      .from('account_requests')
      .select('id, status, created_at')
      .eq('email', normalizedEmail)
      .single();

    if (existingRequest) {
      // Return the current status without creating a duplicate
      return NextResponse.json({
        success: true,
        status: existingRequest.status,
        message: existingRequest.status === 'pending'
          ? 'You already have a pending request. We\'ll notify you once it\'s reviewed.'
          : existingRequest.status === 'approved'
            ? 'Your request was approved! Please check your email for setup instructions.'
            : 'Your previous request was not approved. Please contact the administrator.',
        created_at: existingRequest.created_at,
      });
    }

    // Create new account request
    const { data: newRequest, error: insertError } = await supabase
      .from('account_requests')
      .insert({
        email: normalizedEmail,
        full_name: trimmedName,
        status: 'pending',
      })
      .select()
      .single();

    if (insertError) {
      console.error('Error creating account request:', insertError);
      return NextResponse.json(
        { error: 'Failed to submit request. Please try again.' },
        { status: 500 }
      );
    }

    // Send admin notification email (non-blocking, but log the result)
    sendAdminNotification({
      email: normalizedEmail,
      full_name: trimmedName,
      created_at: newRequest.created_at,
    })
      .then(result => {
        if (result.success) {
          console.log('Admin notification sent for request:', newRequest.id);
        } else {
          console.error('Admin notification failed for request:', newRequest.id, result.error);
        }
      })
      .catch(err => {
        console.error('Admin notification threw error for request:', newRequest.id, err);
      });

    return NextResponse.json({
      success: true,
      status: 'pending',
      message: 'Your request has been submitted! We\'ll review it and send you an email when approved.',
      created_at: newRequest.created_at,
    });

  } catch (error) {
    console.error('Error processing account request:', error);
    return NextResponse.json(
      { error: 'An unexpected error occurred. Please try again.' },
      { status: 500 }
    );
  }
}
