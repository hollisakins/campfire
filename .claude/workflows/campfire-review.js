export const meta = {
  name: 'campfire-review',
  description: 'Whole-repo astronomer-led review: role-play discovery, four-lens synthesis, adversarial verification, and dedup against open issues',
  whenToUse: 'Periodic comprehensive health pass over the CAMPFIRE repo, outside individual feature/fix PRs. Invoked by the /campfire-review command.',
  phases: [
    { title: 'Discover', detail: 'role-play + hygiene finders read the codebase' },
    { title: 'Cluster', detail: 'group raw findings into themes' },
    { title: 'Synthesize', detail: 'four lenses per theme' },
    { title: 'Verify', detail: 'adversarial skeptic refutes each theme' },
    { title: 'Dedup', detail: 'cross-reference open issues/PRs' },
  ],
}

// ---------------------------------------------------------------------------
// Shared context. `args` carries the repo stock-take gathered by the command:
//   { version, recent_commits, open_issues, open_prs, focus }
// All of these may be undefined (e.g. GitHub unavailable) — agents degrade.
// ---------------------------------------------------------------------------
const A = args || {}
const focus = A.focus ? `\n\nSCOPE FOR THIS RUN: the user asked to focus on: ${A.focus}. Weight your attention there, but still flag anything serious you stumble across elsewhere.` : ''

const PERSONA = `You are part of a CAMPFIRE codebase review. CAMPFIRE is a JWST spectroscopy
data platform (pipeline + Next.js web portal + Python client/CLI + Supabase).

BEFORE doing anything, read these for grounding and to calibrate "is this a real problem":
  - .claude/review/persona.md   (your astronomer instincts + the G/D/D bar — REQUIRED)
  - AGENTS.md                   (architecture, intentional decisions, conventions)
You are an astronomer first, a software critic second. Judge everything by "does this
help real astronomy science, defensibly and reproducibly?" Respect documented/deliberate
decisions (e.g. deploy/ is deprecated on purpose) — do not report those as defects.

THIS IS A CODE-READING REVIEW. Do NOT start servers, run npm/the app, drive a browser,
or run the pipeline. Reason about behavior from the source, schemas, components, docs,
and tests. Cite concrete file:line evidence for every finding — no hand-waving.`

const STOCKTAKE = `\n\nREPO STOCK-TAKE (gathered by the command that launched you):
  Version: ${A.version || 'unknown'}
  Recent commits:\n${(A.recent_commits || 'unavailable').toString().split('\n').map(l => '    ' + l).join('\n')}
  Open issues: ${A.open_issues ? `${A.open_issues.length} open (titles below)\n` + A.open_issues.map(i => `    #${i.number} ${i.title}`).join('\n') : 'unavailable'}
  Open PRs: ${A.open_prs ? A.open_prs.map(p => `#${p.number} ${p.title}`).join('; ') : 'unavailable'}`

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------
const EVIDENCE = {
  type: 'array',
  items: {
    type: 'object',
    additionalProperties: false,
    properties: {
      file: { type: 'string', description: 'path relative to repo root' },
      line: { type: 'string', description: 'line number or range, or "" if not line-specific' },
      note: { type: 'string', description: 'what this location shows' },
    },
    required: ['file', 'line', 'note'],
  },
}

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          title: { type: 'string', description: 'one concrete line, e.g. "spectra.flux column has no documented units"' },
          description: { type: 'string', description: '2-4 sentences: what is wrong and why it matters scientifically' },
          component: { type: 'string', enum: ['web', 'python', 'pipeline', 'supabase', 'docs', 'scripts', 'cross-cutting'] },
          severity: { type: 'string', enum: ['blocking', 'significant', 'minor'] },
          effort: { type: 'string', enum: ['quick', 'moderate', 'large'] },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          category: { type: 'string', enum: ['bug', 'ux-quirk', 'inconsistency', 'docs-gap', 'dead-code', 'missing-feature', 'tech-debt', 'science-correctness'] },
          evidence: EVIDENCE,
          suggested_fix: { type: 'string', description: 'concrete first move, one sentence' },
        },
        required: ['title', 'description', 'component', 'severity', 'effort', 'confidence', 'category', 'evidence', 'suggested_fix'],
      },
    },
  },
  required: ['findings'],
}

const THEMES_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    themes: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          title: { type: 'string' },
          summary: { type: 'string', description: 'what ties these findings together' },
          finding_ids: { type: 'array', items: { type: 'number' }, description: 'ids of the raw findings in this theme' },
          components: { type: 'array', items: { type: 'string' } },
          severity: { type: 'string', enum: ['blocking', 'significant', 'minor'] },
          is_structural: { type: 'boolean', description: 'true if this is a larger structural pattern vs an isolated issue' },
        },
        required: ['title', 'summary', 'finding_ids', 'components', 'severity', 'is_structural'],
      },
    },
  },
  required: ['themes'],
}

const LENS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    diagnostician: { type: 'string', description: 'root cause; how it connects to other findings or a larger structural issue' },
    field_medic: { type: 'string', description: 'easiest quick fix, and whether it solves the problem for good or just masks it' },
    rehab: { type: 'string', description: 'would a larger architectural change / refactor help, and would it prevent future similar issues?' },
    innovator: { type: 'string', description: 'any wholly new feature/tool that would sidestep this or resolve the underlying pain point; missing functionality worth building' },
  },
  required: ['diagnostician', 'field_medic', 'rehab', 'innovator'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'rejected', 'uncertain'] },
    reasoning: { type: 'string', description: 'why — especially if rejected, what makes it a non-issue or intended behavior' },
    adjusted_severity: { type: 'string', enum: ['blocking', 'significant', 'minor'] },
  },
  required: ['verdict', 'reasoning', 'adjusted_severity'],
}

const DEDUP_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    status: { type: 'string', enum: ['new', 'duplicate', 'related', 'unknown'] },
    issue_refs: { type: 'array', items: { type: 'number' }, description: 'open issue numbers this duplicates or relates to' },
    note: { type: 'string' },
  },
  required: ['status', 'issue_refs', 'note'],
}

// ---------------------------------------------------------------------------
// Phase 1 — Discovery. Role-play astronomers + hygiene/consistency finders.
// Code-reading only. Barrier: we need all findings before clustering.
// ---------------------------------------------------------------------------
phase('Discover')

const ROLES = [
  {
    key: 'basic-user',
    prompt: `${PERSONA}${STOCKTAKE}${focus}

ROLE: You are a GRAD STUDENT using CAMPFIRE for the first few times. You need a few
specific spectra for ONE science case (e.g. "find PRISM spectra of z>7 candidates in
COSMOS-Web and look at the Lyman break"). You are NOT a developer.

Enumerate 3-5 CONCRETE things you'd try, then trace each through the actual code:
  - Landing / search / filtering: read web/ (pages, components/spectra, lib/actions,
    get_filtered_target_ids RPC). Can you find what you want? Are filters discoverable?
    Are units/labels/defaults sane? Are there confusing or inconsistent UI behaviors?
  - Detail pages: what does an object/spectrum detail page show? Is provenance, units,
    redshift, quality/flags legible to a non-expert?
  - Downloading a file: trace the download path end to end. What do you get? Is the FITS
    self-describing (units, wavelength frame, pipeline version in the header)?
Focus on friction, confusion, and anything that would silently mislead a newcomer.
Report ONLY things grounded in the code with file:line evidence.`,
  },
  {
    key: 'power-user',
    prompt: `${PERSONA}${STOCKTAKE}${focus}

ROLE: You are a POSTDOC doing population-level work and trying to use CAMPFIRE to its
full extent — inspecting many objects and driving everything from the PYTHON CLIENT.

Enumerate 3-5 CONCRETE workflows, then trace each through the code (python/campfire/:
client, models, CLI, data download; and the web actions/RPCs they hit):
  - Batch access: pull N spectra matching a query, with uncertainties + masks + flags,
    into a usable in-memory form. Are non-detections/upper-limits first-class?
  - Provenance/reproducibility: can you trace every flux back to a pipeline version +
    CRDS context? Can a co-author reproduce your selection exactly?
  - A collaboration scenario: handing a sub-sample to a co-author or reproducing a figure.
  - Doing something the docs imply is possible but is awkward/missing in practice.
Look for API/data-model leaks, inconsistencies between web and client, dropped errors,
missing batch ergonomics, and docs that overpromise. file:line evidence required.`,
  },
  {
    key: 'admin',
    prompt: `${PERSONA}${STOCKTAKE}${focus}

ROLE: You are the SURVEY PI / data reducer. You reduce JWST data with the pipeline and
deploy with the CLI. You CANNOT actually run the pipeline here (no JWST data / CRDS /
conda env) — so review by reading code, configs, --dry-run paths, and docs.

Trace these concretely (pipeline/ and python/campfire/deploy/):
  - Reduction: cfpipe nirspec/nircam run — config resolution, stage parametrization,
    overwrite/processes handling, error reporting, output schema/file-naming stability.
  - Versioning & provenance: setuptools-scm version flow, CMPFRVER/cfpipe_version,
    CRDS_CONTEXT, the changelog/release policy in AGENTS.md. Is provenance airtight
    from reduction to deployed spectra?
  - Deploy: campfire deploy (full, --dry-run, rgb, pointings, tiles, sync-programs).
    Guardrails, idempotency, failure recovery, credential handling.
Flag operational footguns, provenance gaps, and places the docs and code disagree.
file:line evidence required.`,
  },
  {
    key: 'hygiene',
    prompt: `${PERSONA}${STOCKTAKE}${focus}

ROLE: Code & docs HYGIENE sweep across the whole repo. You are looking specifically for
the low-glamour rot the user wants cleaned up:
  - Dead code, commented-out blocks left behind, unused files/scripts (note scripts/ has
    a deploy_old.py etc. — check what's genuinely orphaned vs intentionally kept).
  - Stale, missing, or wrong docstrings; README/docs that no longer match the code.
  - Comments that describe a previously-fixed bug or a PAST behavior instead of the CURRENT
    state of the code. These get written in the moment of a fix and lose all context once
    the PR that explains them is merged — to the next reader they're confusing at best and
    actively misleading at worst. Examples: "# don't call X here, it double-counts" sitting
    next to code that no longer double-counts; "// returns None on failure" above a function
    that now raises; "# temporary workaround for the off-by-one" after the off-by-one was
    fixed for good. Flag any comment whose claim contradicts what the adjacent code actually
    does now, or that narrates a change/fix rather than explaining present behavior. Cite the
    comment and the code it disagrees with.
  - TODO/FIXME/HACK markers and what they imply.
  - Obvious inconsistencies in naming/conventions across the codebase.
Be concrete and cite file:line. Do NOT flag the deprecated deploy/ dir as dead — it is
documented as deprecated. Prefer a tight, real list over an exhaustive padded one.`,
  },
  {
    key: 'consistency',
    prompt: `${PERSONA}${STOCKTAKE}${focus}

ROLE: CROSS-CUTTING consistency & correctness auditor. CAMPFIRE is multi-component and
the dangerous bugs live at the seams. Trace shared concepts across ALL surfaces:
  - Units / flux conventions / wavelength frames: are they consistent and explicit across
    Supabase schema (supabase/schemas/), web display, Python models, and FITS headers?
  - The data model: does a column added to spectra/targets propagate consistently to the
    Python model, download, CLI, and docs? Any field that exists in one layer but is
    silently dropped/renamed in another?
  - Flags/quality/null/upper-limit handling consistent across web flags.ts, RPCs, client?
  - Error handling and auth/RLS consistency.
These are the highest-severity findings if real. Demand strong file:line evidence and be
honest about confidence.`,
  },
]

const discovered = (await parallel(
  ROLES.map(r => () => agent(r.prompt, { label: `discover:${r.key}`, phase: 'Discover', schema: FINDINGS_SCHEMA }))
)).filter(Boolean)

// Flatten + assign stable ids, tagging each with the role that found it.
let nextId = 0
const rawFindings = []
discovered.forEach((res, i) => {
  const role = ROLES[i] ? ROLES[i].key : 'unknown'
  for (const f of (res.findings || [])) {
    rawFindings.push({ id: nextId++, role, ...f })
  }
})
log(`Discovery: ${rawFindings.length} raw findings from ${discovered.length} agents`)

if (rawFindings.length === 0) {
  return { generated_for_version: A.version || 'unknown', themes: [], note: 'No findings surfaced.' }
}

// ---------------------------------------------------------------------------
// Phase 2 — Cluster into themes. Barrier: needs ALL findings at once.
// ---------------------------------------------------------------------------
phase('Cluster')

const clusterPrompt = `${PERSONA}

You are the SYNTHESIS lead. Below are raw findings (with ids) from five reviewers. Group
them into coherent THEMES. A theme may contain one finding (if truly isolated) or many.
Prefer grouping findings that share a root cause or structural pattern — the point is to
turn a wall of micro-findings into a handful of actionable work-streams, each of which
could become one GitHub issue. Set is_structural=true when a theme reflects a systemic
pattern rather than a one-off. Do not drop findings: every id must appear in exactly one
theme. Order themes roughly by severity.

RAW FINDINGS (JSON):
${JSON.stringify(rawFindings, null, 1)}`

const clustered = await agent(clusterPrompt, { label: 'cluster', phase: 'Cluster', schema: THEMES_SCHEMA })
const findingById = new Map(rawFindings.map(f => [f.id, f]))
let themes = (clustered.themes || []).map((t, i) => ({
  theme_id: i,
  ...t,
  findings: (t.finding_ids || []).map(id => findingById.get(id)).filter(Boolean),
}))
log(`Clustered into ${themes.length} themes`)

// ---------------------------------------------------------------------------
// Phases 3-5 — per theme: four-lens synthesis -> adversarial verify -> dedup.
// Pipeline: no barrier between stages, each theme flows independently.
// ---------------------------------------------------------------------------
phase('Synthesize')

const haveIssues = Array.isArray(A.open_issues)

const processed = await pipeline(
  themes,
  // Stage 1: four-lens synthesis
  (theme) => agent(
    `${PERSONA}

Apply the FOUR LENSES to this theme. Be concrete and astronomer-minded; reference the
specific findings and their file:line evidence. Each lens is a distinct mode of thought:
  - diagnostician: root cause; is it connected to other findings or a larger structural issue?
  - field_medic: the easiest quick fix — and be honest whether it actually solves it or just masks it.
  - rehab: would a larger architectural change / refactor help, and prevent future similar issues?
  - innovator: any wholly new feature/tool that would sidestep this or resolve the underlying
    pain point for astronomers? Missing functionality worth building?

THEME: ${theme.title}
SUMMARY: ${theme.summary}
FINDINGS (JSON):
${JSON.stringify(theme.findings, null, 1)}`,
    { label: `lens:${theme.theme_id}`, phase: 'Synthesize', schema: LENS_SCHEMA }
  ).then(lenses => ({ ...theme, lenses })),

  // Stage 2: adversarial verification — try to REFUTE the theme.
  (theme) => agent(
    `${PERSONA}

You are a SKEPTIC. Your job is to REFUTE the theme below, not to agree. Read the cited
code yourself. Default toward 'rejected' or 'uncertain' unless the evidence clearly holds.
Reject if: it's intended/documented behavior, the evidence doesn't show what's claimed,
it's already handled elsewhere, or it's a stylistic nitpick dressed up as a defect.
Confirm only if a real astronomer would genuinely be hurt by this. Set adjusted_severity
to what the evidence actually supports (you may downgrade).

THEME: ${theme.title}
SUMMARY: ${theme.summary}
FINDINGS (JSON):
${JSON.stringify(theme.findings, null, 1)}`,
    { label: `verify:${theme.theme_id}`, phase: 'Verify', schema: VERDICT_SCHEMA }
  ).then(verdict => ({ ...theme, verdict, severity: verdict.adjusted_severity || theme.severity })),

  // Stage 3: dedup against open issues/PRs (degrades if GitHub unavailable).
  (theme) => {
    if (theme.verdict && theme.verdict.verdict === 'rejected') {
      return { ...theme, dedup: { status: 'unknown', issue_refs: [], note: 'skipped — refuted in verification' } }
    }
    const issuesBlock = haveIssues
      ? `Open issues (number — title):\n${A.open_issues.map(i => `#${i.number} ${i.title}`).join('\n')}\n\nIf you need issue bodies to decide, search GitHub via available mcp__github__ tools (load with ToolSearch). `
      : `No open-issue list was provided and GitHub may be unavailable. Try mcp__github__ search tools (load via ToolSearch) for repo hollisakins/campfire; if they are unavailable, return status "unknown" and say dedup was skipped. `
    return agent(
      `You are deduplicating a review finding against the existing issue tracker for the
repo hollisakins/campfire. Decide whether this theme is already tracked.
${issuesBlock}
THEME: ${theme.title}
SUMMARY: ${theme.summary}
KEY EVIDENCE: ${JSON.stringify((theme.findings || []).flatMap(f => f.evidence || []).slice(0, 6))}`,
      { label: `dedup:${theme.theme_id}`, phase: 'Dedup', schema: DEDUP_SCHEMA }
    ).then(dedup => ({ ...theme, dedup }))
  }
)

const out = processed.filter(Boolean)

// Surface refuted themes separately rather than silently dropping them — the user
// may disagree with the skeptic during triage.
const confirmed = out.filter(t => !t.verdict || t.verdict.verdict !== 'rejected')
const refuted = out.filter(t => t.verdict && t.verdict.verdict === 'rejected')

const SEV = { blocking: 0, significant: 1, minor: 2 }
confirmed.sort((a, b) => (SEV[a.severity] ?? 3) - (SEV[b.severity] ?? 3))

log(`Done: ${confirmed.length} themes for triage, ${refuted.length} refuted, dedup ${haveIssues ? 'on' : 'best-effort'}`)

return {
  generated_for_version: A.version || 'unknown',
  focus: A.focus || null,
  dedup_mode: haveIssues ? 'against-open-issues' : 'best-effort-or-skipped',
  counts: {
    raw_findings: rawFindings.length,
    themes_total: themes.length,
    confirmed: confirmed.length,
    refuted: refuted.length,
  },
  themes: confirmed,
  refuted,
}
