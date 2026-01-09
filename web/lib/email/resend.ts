import { Resend } from 'resend';

interface AccountRequestNotification {
  email: string;
  full_name: string;
  created_at: string;
}

/**
 * Send email notification to admin when a new account request is submitted
 */
export async function sendAdminNotification(request: AccountRequestNotification): Promise<{ success: boolean; error?: string }> {
  const adminEmail = process.env.ADMIN_NOTIFICATION_EMAIL;
  const resendApiKey = process.env.RESEND_API_KEY;

  if (!adminEmail) {
    console.warn('ADMIN_NOTIFICATION_EMAIL not set, skipping notification');
    return { success: true }; // Don't fail the request if email is not configured
  }

  if (!resendApiKey) {
    console.warn('RESEND_API_KEY not set, skipping notification');
    return { success: true }; // Don't fail the request if API key is not configured
  }

  // Initialize Resend client only when needed (avoids build-time errors)
  const resend = new Resend(resendApiKey);

  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000';
  const formattedDate = new Date(request.created_at).toLocaleString('en-US', {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });

  try {
    const { error } = await resend.emails.send({
      from: 'CAMPFIRE <noreply@resend.dev>', // Use Resend's default domain for testing
      to: adminEmail,
      subject: `New Account Request: ${request.full_name}`,
      html: `
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
          <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px 10px 0 0;">
            <h1 style="color: white; margin: 0; font-size: 24px;">New Account Request</h1>
          </div>

          <div style="background: #f9fafb; padding: 30px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 10px 10px;">
            <p style="margin-top: 0;">Someone has requested access to CAMPFIRE:</p>

            <div style="background: white; padding: 20px; border-radius: 8px; border: 1px solid #e5e7eb; margin: 20px 0;">
              <p style="margin: 0 0 10px 0;"><strong>Name:</strong> ${request.full_name}</p>
              <p style="margin: 0 0 10px 0;"><strong>Email:</strong> ${request.email}</p>
              <p style="margin: 0;"><strong>Submitted:</strong> ${formattedDate}</p>
            </div>

            <a href="${appUrl}/admin/requests"
               style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 500; margin-top: 10px;">
              Review in Admin Panel
            </a>

            <p style="color: #6b7280; font-size: 14px; margin-top: 30px; margin-bottom: 0;">
              This email was sent automatically by CAMPFIRE.
            </p>
          </div>
        </body>
        </html>
      `,
      text: `
New Account Request for CAMPFIRE

Name: ${request.full_name}
Email: ${request.email}
Submitted: ${formattedDate}

Review this request in the Admin Panel:
${appUrl}/admin/requests
      `.trim(),
    });

    if (error) {
      console.error('Failed to send admin notification:', error);
      return { success: false, error: error.message };
    }

    return { success: true };
  } catch (err) {
    console.error('Error sending admin notification:', err);
    return {
      success: false,
      error: err instanceof Error ? err.message : 'Unknown error'
    };
  }
}
