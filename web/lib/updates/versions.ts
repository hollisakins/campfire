// Config for the version/status badges in the header of the /updates page.
//
// The pipeline and client versions are pulled LIVE from GitHub via shields.io
// badges (rendered client-side), so they never need editing here:
//   • pipeline → latest `pipeline-vX.Y.Z` git tag (setuptools-scm's source of truth)
//   • client   → `version` in python/pyproject.toml on `main`
// As these actively-developed packages move, the header follows GitHub with no
// redeploy. Only the data-release label — a human-declared database freeze — is
// manual, so it stays hand-set below.

const REPO = 'hollisakins/campfire';

/** Canonical repo URL, reused for badge links and the "View on GitHub" action. */
export const GITHUB_REPO_URL = `https://github.com/${REPO}`;

/** raw.githubusercontent URL for a repo file on the default branch. */
const rawMain = (p: string) => `https://raw.githubusercontent.com/${REPO}/main/${p}`;

export interface StatusBadge {
  /** Stable key for React lists. */
  key: string;
  /** shields.io image URL — rendered client-side, reads live from GitHub. */
  src: string;
  /** GitHub page this badge links to. */
  href: string;
  /** Accessible alt / title text. */
  alt: string;
}

/** Live version/status badges, in display order. Tweak shields params here. */
export const STATUS_BADGES: StatusBadge[] = [
  {
    key: 'pipeline',
    // Highest semver `pipeline-v*` tag. Empty label, so the tag name (which
    // already reads "pipeline-vX.Y.Z") is the whole badge.
    src: `https://img.shields.io/github/v/tag/${REPO}?filter=pipeline-v*&sort=semver&label=&color=2563eb`,
    href: `${GITHUB_REPO_URL}/tags`,
    alt: 'Latest campfire-pipeline release tag',
  },
  {
    key: 'client',
    // `campfire` Python package / CLI version, read from pyproject.toml on main.
    // (Hardcoded there for now; moves to git-tag semver once that issue lands —
    // this badge will keep tracking pyproject either way.)
    src: `https://img.shields.io/badge/dynamic/toml?url=${encodeURIComponent(
      rawMain('python/pyproject.toml'),
    )}&query=%24.project.version&prefix=v&label=client&color=16a34a`,
    href: `${GITHUB_REPO_URL}/tree/main/python`,
    alt: 'campfire Python client / CLI version',
  },
  {
    key: 'last-commit',
    // Active-development signal — links to the commit history.
    src: `https://img.shields.io/github/last-commit/${REPO}?label=last%20commit&color=64748b`,
    href: `${GITHUB_REPO_URL}/commits`,
    alt: 'Date of the most recent commit',
  },
];

/** Human label for the current frozen database "data release", if any.
 *  Manual: set this when you cut a data release, otherwise leave empty. */
export const DATA_RELEASE = '';
