import { Resend } from 'resend';

interface InspectionRequestNotification {
  full_name: string;
  username: string;
  email: string;
  message?: string | null;
  created_at: string;
}

/**
 * Notify admins when a user requests inspection privileges. Best-effort: never
 * fails the underlying request if email isn't configured.
 */
export async function sendInspectionRequestNotification(
  request: InspectionRequestNotification
): Promise<{ success: boolean; error?: string }> {
  const adminEmail = process.env.ADMIN_NOTIFICATION_EMAIL;
  const resendApiKey = process.env.RESEND_API_KEY;

  if (!adminEmail) {
    console.warn('ADMIN_NOTIFICATION_EMAIL not set, skipping notification');
    return { success: true };
  }
  if (!resendApiKey) {
    console.warn('RESEND_API_KEY not set, skipping notification');
    return { success: true };
  }

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
    const { data, error } = await resend.emails.send({
      from: 'CAMPFIRE <team@campfire.hollisakins.com>',
      to: adminEmail,
      subject: `Inspection access request: ${request.full_name}`,
      html: `
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
          <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px 10px 0 0;">
            <h1 style="color: white; margin: 0; font-size: 24px;">Inspection Access Request</h1>
          </div>

          <div style="background: #f9fafb; padding: 30px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 10px 10px;">
            <p style="margin-top: 0;">A user has requested permission to submit inspections:</p>

            <div style="background: white; padding: 20px; border-radius: 8px; border: 1px solid #e5e7eb; margin: 20px 0;">
              <p style="margin: 0 0 10px 0;"><strong>Name:</strong> ${request.full_name}</p>
              <p style="margin: 0 0 10px 0;"><strong>Username:</strong> @${request.username}</p>
              <p style="margin: 0 0 10px 0;"><strong>Email:</strong> ${request.email}</p>
              ${request.message ? `<p style="margin: 0 0 10px 0;"><strong>Message:</strong> ${request.message}</p>` : ''}
              <p style="margin: 0;"><strong>Submitted:</strong> ${formattedDate}</p>
            </div>

            <a href="${appUrl}/admin/inspection-requests"
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
Inspection Access Request for CAMPFIRE

Name: ${request.full_name}
Username: @${request.username}
Email: ${request.email}
${request.message ? `Message: ${request.message}\n` : ''}Submitted: ${formattedDate}

Review this request in the Admin Panel:
${appUrl}/admin/inspection-requests
      `.trim(),
    });

    if (error) {
      console.error('Failed to send inspection request notification:', error);
      return { success: false, error: error.message };
    }

    console.log('Inspection request notification sent:', { id: data?.id, to: adminEmail });
    return { success: true };
  } catch (err) {
    console.error('Error sending inspection request notification:', err);
    return { success: false, error: err instanceof Error ? err.message : 'Unknown error' };
  }
}

interface InspectionDecisionNotification {
  email: string;
  full_name: string;
  approved: boolean;
}

/**
 * Notify a user that their inspection-access request was reviewed. Best-effort:
 * never fails the review action if email isn't configured. (The profile page
 * promises users an email once an admin has reviewed their request.)
 */
export async function sendInspectionDecisionNotification(
  decision: InspectionDecisionNotification
): Promise<{ success: boolean; error?: string }> {
  const resendApiKey = process.env.RESEND_API_KEY;

  if (!resendApiKey) {
    console.warn('RESEND_API_KEY not set, skipping decision notification');
    return { success: true };
  }

  const resend = new Resend(resendApiKey);
  const appUrl = process.env.NEXT_PUBLIC_APP_URL || 'http://localhost:3000';
  const { email, full_name, approved } = decision;

  const bodyText = approved
    ? 'Your request for inspection access has been approved. You can now submit redshift assessments, quality ratings, and flags from any spectrum page.'
    : 'Your request for inspection access was not approved at this time. If you think this is a mistake, reply to this email or reach out to the CAMPFIRE team.';
  const ctaLabel = approved ? 'Start Inspecting' : 'Open CAMPFIRE';
  const ctaUrl = approved ? `${appUrl}/nirspec` : appUrl;

  try {
    const { data, error } = await resend.emails.send({
      from: 'CAMPFIRE <team@campfire.hollisakins.com>',
      to: email,
      subject: approved
        ? 'Your CAMPFIRE inspection access request was approved'
        : 'Update on your CAMPFIRE inspection access request',
      html: `
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
          <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px 10px 0 0;">
            <h1 style="color: white; margin: 0; font-size: 24px;">Inspection Access ${approved ? 'Approved' : 'Request Reviewed'}</h1>
          </div>

          <div style="background: #f9fafb; padding: 30px; border: 1px solid #e5e7eb; border-top: none; border-radius: 0 0 10px 10px;">
            <p style="margin-top: 0;">Hi ${full_name},</p>
            <p>${bodyText}</p>

            <a href="${ctaUrl}"
               style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 500; margin-top: 10px;">
              ${ctaLabel}
            </a>

            <p style="color: #6b7280; font-size: 14px; margin-top: 30px; margin-bottom: 0;">
              This email was sent automatically by CAMPFIRE.
            </p>
          </div>
        </body>
        </html>
      `,
      text: `
Hi ${full_name},

${bodyText}

${ctaUrl}
      `.trim(),
    });

    if (error) {
      console.error('Failed to send inspection decision notification:', error);
      return { success: false, error: error.message };
    }

    console.log('Inspection decision notification sent:', { id: data?.id, to: email });
    return { success: true };
  } catch (err) {
    console.error('Error sending inspection decision notification:', err);
    return { success: false, error: err instanceof Error ? err.message : 'Unknown error' };
  }
}
