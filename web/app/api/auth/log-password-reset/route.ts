import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';

export async function POST(request: NextRequest) {
  try {
    const { userId } = await request.json();

    if (!userId) {
      return NextResponse.json(
        { error: 'User ID is required' },
        { status: 400 }
      );
    }

    const supabase = await createClient();

    // Get the authenticated user to verify they're resetting their own password
    const {
      data: { user },
      error: userError,
    } = await supabase.auth.getUser();

    if (userError || !user || user.id !== userId) {
      return NextResponse.json(
        { error: 'Unauthorized' },
        { status: 401 }
      );
    }

    // Get client IP and user agent
    const ip_address = request.headers.get('x-forwarded-for') || request.headers.get('x-real-ip') || 'unknown';
    const user_agent = request.headers.get('user-agent') || 'unknown';

    // Log the password reset
    const { error: insertError } = await supabase
      .from('password_reset_log')
      .insert({
        user_id: userId,
        ip_address,
        user_agent,
      });

    if (insertError) {
      console.error('Error logging password reset:', insertError);
      // Don't fail the request if logging fails
      return NextResponse.json({ success: true });
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error('Password reset logging error:', error);
    // Return success even if logging fails - don't block the reset
    return NextResponse.json({ success: true });
  }
}
