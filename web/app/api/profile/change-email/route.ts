import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(request: NextRequest) {
  try {
    const { newEmail } = await request.json();

    if (!newEmail) {
      return NextResponse.json(
        { error: 'New email address is required' },
        { status: 400 }
      );
    }

    // Validate email format
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(newEmail)) {
      return NextResponse.json(
        { error: 'Invalid email address format' },
        { status: 400 }
      );
    }

    const supabase = await createClient();

    // Get the authenticated user
    const {
      data: { user },
      error: userError,
    } = await supabase.auth.getUser();

    if (userError || !user) {
      return NextResponse.json(
        { error: 'Unauthorized' },
        { status: 401 }
      );
    }

    const currentEmail = user.email;

    if (!currentEmail) {
      return NextResponse.json(
        { error: 'Current email not found' },
        { status: 400 }
      );
    }

    // Check if new email is the same as current
    if (newEmail.toLowerCase() === currentEmail.toLowerCase()) {
      return NextResponse.json(
        { error: 'This is already your current email address' },
        { status: 400 }
      );
    }

    // Initiate email change via Supabase
    // This will send confirmation emails to both old and new addresses
    const { error: updateError } = await supabase.auth.updateUser({
      email: newEmail.toLowerCase(),
    });

    if (updateError) {
      console.error('Email update error:', updateError);
      return NextResponse.json(
        { error: updateError.message },
        { status: 400 }
      );
    }

    // Log the email change attempt
    const ip_address = request.headers.get('x-forwarded-for') || request.headers.get('x-real-ip') || 'unknown';
    const user_agent = request.headers.get('user-agent') || 'unknown';

    const { error: logError } = await supabase
      .from('email_change_log')
      .insert({
        user_id: user.id,
        old_email: currentEmail,
        new_email: newEmail.toLowerCase(),
        ip_address,
        user_agent,
      });

    if (logError) {
      console.error('Error logging email change:', logError);
      // Don't fail the request if logging fails
    }

    return NextResponse.json({
      success: true,
      message: 'Verification emails sent. Please check both your current and new email addresses.',
    });
  } catch (error) {
    console.error('Email change error:', error);
    return NextResponse.json(
      { error: 'An unexpected error occurred' },
      { status: 500 }
    );
  }
}
