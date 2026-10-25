import { Activity, Bug, Github, Lightbulb } from 'lucide-react';

const GITHUB_REPO = 'https://github.com/hollisakins/campfire';
const STATUS_PAGE = 'https://stats.uptimerobot.com/ajNDnzKMeM';

export function Footer() {
  return (
    <footer className="border-t border-border">
      <div className="container mx-auto px-4 py-4 flex flex-col sm:flex-row items-center justify-between gap-2 text-sm text-text-secondary">
        <span>&copy; {new Date().getFullYear()} Hollis Akins &middot; MIT License</span>
        <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1">
          <a
            href={GITHUB_REPO}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Github className="w-4 h-4" />
            GitHub
          </a>
          <span className="hidden sm:inline text-border dark:text-border-strong">|</span>
          <a
            href={`${GITHUB_REPO}/issues/new?template=bug_report.yml`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Bug className="w-4 h-4" />
            Report a Bug
          </a>
          <span className="hidden sm:inline text-border dark:text-border-strong">|</span>
          <a
            href={`${GITHUB_REPO}/issues/new?template=feature_request.yml`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Lightbulb className="w-4 h-4" />
            Request a Feature
          </a>
          <span className="hidden sm:inline text-border dark:text-border-strong">|</span>
          <a
            href={STATUS_PAGE}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Activity className="w-4 h-4" />
            Status
          </a>
        </div>
      </div>
    </footer>
  );
}
