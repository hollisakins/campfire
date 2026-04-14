import { Bug, Github, Lightbulb } from 'lucide-react';

const GITHUB_REPO = 'https://github.com/hollisakins/campfire';

export function Footer() {
  return (
    <footer className="border-t border-border dark:border-slate-700">
      <div className="container mx-auto px-4 py-4 flex flex-col sm:flex-row items-center justify-between gap-2 text-sm text-text-secondary dark:text-slate-400">
        <span>&copy; 2025 Hollis Akins &middot; MIT License</span>
        <div className="flex items-center gap-4">
          <a
            href={GITHUB_REPO}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Github className="w-4 h-4" />
            GitHub
          </a>
          <span className="text-border dark:text-slate-600">|</span>
          <a
            href={`${GITHUB_REPO}/issues/new?template=bug_report.yml`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Bug className="w-4 h-4" />
            Report a Bug
          </a>
          <span className="text-border dark:text-slate-600">|</span>
          <a
            href={`${GITHUB_REPO}/issues/new?template=feature_request.yml`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-primary transition-colors"
          >
            <Lightbulb className="w-4 h-4" />
            Request a Feature
          </a>
        </div>
      </div>
    </footer>
  );
}
