import { ThemeToggle } from 'campfire-web';

// Renders the segmented Light / System / Dark control. With no ThemeProvider on
// the page, useTheme() falls back to its 'system' default, so the System
// segment shows active (ember fill).
export function Default() {
  return <ThemeToggle />;
}
