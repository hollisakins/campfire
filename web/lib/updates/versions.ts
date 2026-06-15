// Current component versions shown in the header of the /updates page.
//
// HARD-CODED FOR NOW. The `campfire` package/CLI does not yet have
// setuptools-scm versioning, and the pipeline has no release tags, so these
// values are maintained by hand. Once proper versioning lands, wire these to
// the real package versions (e.g. a generated file or build-time injection).
// Tracking issue: see repo issues ("Add setuptools-scm versioning to the
// campfire package/CLI and surface it on the Updates page").

export interface VersionInfo {
  /** campfire-pipeline release (git tag pipeline-vX.Y.Z), or a dev label. */
  pipeline: string;
  /** campfire python package / CLI version. */
  client: string;
  /** Human label for the current frozen database "data release", if any. */
  dataRelease: string;
}

export const CURRENT_VERSIONS: VersionInfo = {
  pipeline: '0.1.0 (unreleased)',
  client: '0.4.0',
  dataRelease: 'Rolling — no frozen release yet',
};
