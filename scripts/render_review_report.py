#!/usr/bin/env python3
"""Render a campfire-review findings JSON file into a self-contained triage HTML page.

Usage:
    python scripts/render_review_report.py findings.json -o report.html

The findings JSON is the object returned by the `campfire-review` workflow. The
generated HTML embeds the data inline (so it works over file://), lets you mark
each theme valid/invalid and add notes (persisted in localStorage, keyed by the
report's version+generation), and exports your triage decisions as JSON for the
`/campfire-review-file-issues` command to turn into GitHub issues.
"""
import argparse
import datetime
import html
import json
import sys

SEV_META = {
    "blocking": ("\U0001F534", "Blocking"),
    "significant": ("\U0001F7E1", "Significant"),
    "minor": ("\U0001F7E2", "Minor"),
}


def esc(x):
    return html.escape(str(x if x is not None else ""))


def render(data, source_name):
    themes = data.get("themes", []) or []
    refuted = data.get("refuted", []) or []
    counts = data.get("counts", {}) or {}
    version = data.get("generated_for_version", "unknown")
    focus = data.get("focus")
    dedup_mode = data.get("dedup_mode", "unknown")
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    # Stable key so triage state survives reloads but resets for a new run.
    report_key = f"campfire-review::{version}::{counts.get('raw_findings', 0)}::{len(themes)}"

    payload = {
        "report_key": report_key,
        "version": version,
        "themes": themes,
        "refuted": refuted,
    }

    cards = []
    for t in themes:
        cards.append(theme_card(t, refuted=False))
    for t in refuted:
        cards.append(theme_card(t, refuted=True))

    focus_line = f'<span class="meta-pill">focus: {esc(focus)}</span>' if focus else ""

    return TEMPLATE.format(
        version=esc(version),
        generated=esc(generated),
        source=esc(source_name),
        dedup_mode=esc(dedup_mode),
        focus_line=focus_line,
        n_themes=len(themes),
        n_refuted=len(refuted),
        n_raw=counts.get("raw_findings", "?"),
        cards="\n".join(cards),
        payload=json.dumps(payload),
    )


def lens_block(lenses):
    if not lenses:
        return ""
    order = [
        ("diagnostician", "\U0001F50D Diagnostician", "root cause & connections"),
        ("field_medic", "\U0001FA79 Field medic", "easiest quick fix"),
        ("rehab", "\U0001F3D7️ Rehab", "architectural change"),
        ("innovator", "\U0001F4A1 Innovator", "new feature / future-proofing"),
    ]
    rows = []
    for key, label, sub in order:
        val = lenses.get(key)
        if not val:
            continue
        rows.append(
            f'<div class="lens"><div class="lens-h">{label} '
            f'<span class="lens-sub">{esc(sub)}</span></div>'
            f'<div class="lens-b">{esc(val)}</div></div>'
        )
    return f'<div class="lenses">{"".join(rows)}</div>'


def evidence_block(findings):
    rows = []
    for f in findings or []:
        sev, _ = SEV_META.get(f.get("severity", "minor"), ("", ""))
        ev = "".join(
            f'<li><code>{esc(e.get("file"))}'
            + (f':{esc(e.get("line"))}' if e.get("line") else "")
            + f'</code> — {esc(e.get("note"))}</li>'
            for e in (f.get("evidence") or [])
        )
        rows.append(
            f'<div class="finding">'
            f'<div class="finding-h">{sev} <b>{esc(f.get("title"))}</b>'
            f'<span class="tag">{esc(f.get("category"))}</span>'
            f'<span class="tag">{esc(f.get("component"))}</span>'
            f'<span class="tag">effort: {esc(f.get("effort"))}</span>'
            f'<span class="tag">conf: {esc(f.get("confidence"))}</span>'
            f'<span class="tag">via {esc(f.get("role"))}</span></div>'
            f'<div class="finding-d">{esc(f.get("description"))}</div>'
            + (f'<div class="finding-fix">→ {esc(f.get("suggested_fix"))}</div>' if f.get("suggested_fix") else "")
            + (f'<ul class="ev">{ev}</ul>' if ev else "")
            + "</div>"
        )
    return "".join(rows)


def theme_card(t, refuted):
    tid = t.get("theme_id")
    sev = t.get("severity", "minor")
    sev_emoji, sev_label = SEV_META.get(sev, ("", sev))
    verdict = (t.get("verdict") or {})
    dedup = (t.get("dedup") or {})
    comps = ", ".join(t.get("components", []) or [])
    structural = '<span class="badge struct">structural</span>' if t.get("is_structural") else ""

    dedup_status = dedup.get("status", "unknown")
    dedup_refs = dedup.get("issue_refs", []) or []
    dedup_badge = ""
    if dedup_status == "duplicate":
        dedup_badge = f'<span class="badge dup">duplicate of {", ".join("#"+str(r) for r in dedup_refs)}</span>'
    elif dedup_status == "related":
        dedup_badge = f'<span class="badge rel">related to {", ".join("#"+str(r) for r in dedup_refs)}</span>'
    elif dedup_status == "new":
        dedup_badge = '<span class="badge new">new</span>'

    verdict_badge = ""
    if refuted:
        verdict_badge = '<span class="badge refuted">refuted by skeptic</span>'
    elif verdict.get("verdict") == "uncertain":
        verdict_badge = '<span class="badge uncertain">uncertain</span>'

    refuted_attr = "true" if refuted else "false"
    verdict_note = (
        f'<div class="verdict-note"><b>Skeptic:</b> {esc(verdict.get("reasoning"))}</div>'
        if verdict.get("reasoning") else ""
    )
    dedup_note = (
        f'<div class="dedup-note"><b>Dedup:</b> {esc(dedup.get("note"))}</div>'
        if dedup.get("note") else ""
    )

    return f'''
<div class="card sev-{esc(sev)}" data-id="{esc(tid)}" data-sev="{esc(sev)}"
     data-comp="{esc(comps)}" data-refuted="{refuted_attr}" data-dedup="{esc(dedup_status)}">
  <div class="card-top">
    <div class="card-title">{sev_emoji} {esc(t.get("title"))}</div>
    <div class="badges">{structural}{dedup_badge}{verdict_badge}
      <span class="badge sevb sev-{esc(sev)}">{esc(sev_label)}</span></div>
  </div>
  <div class="card-meta">{esc(comps)} &middot; {len(t.get("findings", []) or [])} finding(s)</div>
  <div class="summary">{esc(t.get("summary"))}</div>
  {lens_block(t.get("lenses"))}
  <details class="ev-wrap"><summary>Findings &amp; evidence ({len(t.get("findings", []) or [])})</summary>
    {evidence_block(t.get("findings"))}
  </details>
  {verdict_note}
  {dedup_note}
  <div class="triage">
    <div class="triage-btns">
      <button class="t-valid" onclick="setVerdict({esc(tid)},'valid')">✓ Valid</button>
      <button class="t-invalid" onclick="setVerdict({esc(tid)},'invalid')">✗ Invalid</button>
      <span class="t-state" id="state-{esc(tid)}"></span>
    </div>
    <textarea class="t-notes" id="notes-{esc(tid)}" placeholder="Notes (appended to the issue body)..."
      oninput="saveNotes({esc(tid)})"></textarea>
  </div>
</div>'''


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CAMPFIRE review — {version}</title>
<style>
  :root {{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --ink:#e7e9ee; --dim:#9aa3b2;
    --line:#2a2f3a; --blocking:#ff5c5c; --significant:#ffb23e; --minor:#46c46e; --acc:#6ea8fe;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ position:sticky; top:0; z-index:10; background:var(--panel); border-bottom:1px solid var(--line);
    padding:14px 20px; }}
  h1 {{ margin:0 0 4px; font-size:18px; }}
  .sub {{ color:var(--dim); font-size:12px; }}
  .meta-pill {{ background:var(--panel2); border:1px solid var(--line); border-radius:10px;
    padding:1px 8px; margin-right:6px; }}
  .controls {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }}
  .controls select, .controls input {{ background:var(--panel2); color:var(--ink);
    border:1px solid var(--line); border-radius:6px; padding:5px 8px; }}
  .controls button {{ background:var(--acc); color:#06101f; border:0; border-radius:6px;
    padding:6px 12px; font-weight:600; cursor:pointer; }}
  .controls .ghost {{ background:var(--panel2); color:var(--ink); border:1px solid var(--line); }}
  .wrap {{ max-width:980px; margin:0 auto; padding:20px; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--line);
    border-radius:10px; padding:16px; margin-bottom:16px; }}
  .card.sev-blocking {{ border-left-color:var(--blocking); }}
  .card.sev-significant {{ border-left-color:var(--significant); }}
  .card.sev-minor {{ border-left-color:var(--minor); }}
  .card.v-valid {{ box-shadow:0 0 0 1px var(--minor) inset; }}
  .card.v-invalid {{ opacity:.5; }}
  .card-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
  .card-title {{ font-size:16px; font-weight:650; }}
  .card-meta {{ color:var(--dim); font-size:12px; margin:2px 0 8px; }}
  .badges {{ display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }}
  .badge {{ font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--line);
    background:var(--panel2); white-space:nowrap; }}
  .badge.struct {{ color:#c9a3ff; border-color:#5b3f8a; }}
  .badge.dup {{ color:#ff9; border-color:#7a6; }}
  .badge.rel {{ color:#9cf; }}
  .badge.new {{ color:var(--minor); border-color:#2c5; }}
  .badge.refuted {{ color:var(--blocking); border-color:#a33; }}
  .badge.uncertain {{ color:var(--significant); }}
  .badge.sevb.sev-blocking {{ color:var(--blocking); }}
  .badge.sevb.sev-significant {{ color:var(--significant); }}
  .badge.sevb.sev-minor {{ color:var(--minor); }}
  .summary {{ margin:6px 0 12px; }}
  .lenses {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:10px 0; }}
  .lens {{ background:var(--panel2); border:1px solid var(--line); border-radius:8px; padding:10px; }}
  .lens-h {{ font-weight:650; font-size:12px; margin-bottom:4px; }}
  .lens-sub {{ color:var(--dim); font-weight:400; }}
  .lens-b {{ font-size:13px; color:#d4d8e0; }}
  details.ev-wrap {{ margin:6px 0; }}
  details.ev-wrap > summary {{ cursor:pointer; color:var(--acc); font-size:13px; }}
  .finding {{ border-top:1px solid var(--line); padding:8px 0; }}
  .finding-h {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
  .finding-d {{ color:#cfd4dd; margin:3px 0; }}
  .finding-fix {{ color:var(--minor); font-size:13px; }}
  .tag {{ font-size:10px; color:var(--dim); background:var(--bg); border:1px solid var(--line);
    border-radius:8px; padding:1px 6px; }}
  ul.ev {{ margin:6px 0 0; padding-left:18px; color:var(--dim); font-size:12px; }}
  code {{ background:var(--bg); padding:1px 5px; border-radius:4px; color:#bcd; }}
  .verdict-note, .dedup-note {{ font-size:12px; color:var(--dim); margin-top:8px;
    border-left:2px solid var(--line); padding-left:8px; }}
  .triage {{ margin-top:12px; border-top:1px dashed var(--line); padding-top:10px; }}
  .triage-btns {{ display:flex; gap:8px; align-items:center; }}
  .triage button {{ cursor:pointer; border:1px solid var(--line); background:var(--panel2);
    color:var(--ink); border-radius:6px; padding:5px 12px; }}
  .triage button.on-valid {{ background:var(--minor); color:#04210f; border-color:var(--minor); }}
  .triage button.on-invalid {{ background:var(--blocking); color:#2a0606; border-color:var(--blocking); }}
  .t-state {{ font-size:12px; color:var(--dim); }}
  textarea.t-notes {{ width:100%; margin-top:8px; min-height:48px; background:var(--bg);
    color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:8px; resize:vertical; }}
  .section-h {{ color:var(--dim); text-transform:uppercase; letter-spacing:.08em; font-size:11px;
    margin:24px 0 8px; }}
  .hidden {{ display:none !important; }}
</style>
</head>
<body>
<header>
  <h1>🔥 CAMPFIRE review report</h1>
  <div class="sub">
    <span class="meta-pill">version: {version}</span>
    <span class="meta-pill">generated: {generated}</span>
    <span class="meta-pill">{n_themes} themes / {n_raw} raw findings</span>
    <span class="meta-pill">dedup: {dedup_mode}</span>
    {focus_line}
  </div>
  <div class="controls">
    <input id="search" type="text" placeholder="filter text..." oninput="applyFilters()">
    <select id="fsev" onchange="applyFilters()">
      <option value="">all severities</option>
      <option value="blocking">blocking</option>
      <option value="significant">significant</option>
      <option value="minor">minor</option>
    </select>
    <select id="fstate" onchange="applyFilters()">
      <option value="">all triage states</option>
      <option value="untriaged">untriaged</option>
      <option value="valid">valid</option>
      <option value="invalid">invalid</option>
    </select>
    <select id="fdedup" onchange="applyFilters()">
      <option value="">all dedup</option>
      <option value="new">new only</option>
      <option value="duplicate">duplicate</option>
      <option value="related">related</option>
    </select>
    <label class="sub"><input type="checkbox" id="fhref" onchange="applyFilters()"> hide refuted</label>
    <button onclick="exportTriage()">⬇ Export triage JSON</button>
    <button class="ghost" onclick="resetTriage()">reset triage</button>
    <span class="t-state" id="progress"></span>
  </div>
</header>
<div class="wrap">
  <div class="section-h">Themes for triage ({n_themes})</div>
  {cards}
</div>
<script id="payload" type="application/json">{payload}</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const KEY = DATA.report_key;
function store() {{ try {{ return JSON.parse(localStorage.getItem(KEY) || '{{}}'); }} catch(e) {{ return {{}}; }} }}
function persist(s) {{ localStorage.setItem(KEY, JSON.stringify(s)); }}

function setVerdict(id, v) {{
  const s = store(); s[id] = s[id] || {{}};
  s[id].verdict = (s[id].verdict === v) ? null : v;
  persist(s); paint(id); updateProgress();
}}
function saveNotes(id) {{
  const s = store(); s[id] = s[id] || {{}};
  s[id].notes = document.getElementById('notes-'+id).value; persist(s);
}}
function paint(id) {{
  const s = store()[id] || {{}};
  const card = document.querySelector('.card[data-id="'+id+'"]');
  const vb = card.querySelector('.t-valid'), ib = card.querySelector('.t-invalid');
  const st = document.getElementById('state-'+id);
  card.classList.remove('v-valid','v-invalid'); vb.classList.remove('on-valid'); ib.classList.remove('on-invalid');
  if (s.verdict === 'valid') {{ card.classList.add('v-valid'); vb.classList.add('on-valid'); st.textContent='marked valid'; }}
  else if (s.verdict === 'invalid') {{ card.classList.add('v-invalid'); ib.classList.add('on-invalid'); st.textContent='marked invalid'; }}
  else st.textContent='';
  const ta = document.getElementById('notes-'+id); if (s.notes != null) ta.value = s.notes;
}}
function updateProgress() {{
  const s = store(); let v=0, iv=0;
  Object.values(s).forEach(x => {{ if (x.verdict==='valid') v++; else if (x.verdict==='invalid') iv++; }});
  document.getElementById('progress').textContent = v+' valid · '+iv+' invalid';
}}
function applyFilters() {{
  const q = document.getElementById('search').value.toLowerCase();
  const sev = document.getElementById('fsev').value;
  const state = document.getElementById('fstate').value;
  const dd = document.getElementById('fdedup').value;
  const hideRef = document.getElementById('fhref').checked;
  const s = store();
  document.querySelectorAll('.card').forEach(c => {{
    const id = c.getAttribute('data-id');
    const v = (s[id]||{{}}).verdict || 'untriaged';
    let show = true;
    if (q && !c.textContent.toLowerCase().includes(q)) show = false;
    if (sev && c.getAttribute('data-sev') !== sev) show = false;
    if (state && v !== state) show = false;
    if (dd && c.getAttribute('data-dedup') !== dd) show = false;
    if (hideRef && c.getAttribute('data-refuted') === 'true') show = false;
    c.classList.toggle('hidden', !show);
  }});
}}
function exportTriage() {{
  const s = store();
  const byId = {{}};
  (DATA.themes||[]).concat(DATA.refuted||[]).forEach(t => byId[t.theme_id] = t);
  const out = [];
  Object.keys(s).forEach(id => {{
    const e = s[id]; if (!e || !e.verdict) return;
    const t = byId[id] || {{}};
    out.push({{
      theme_id: Number(id), verdict: e.verdict, notes: e.notes || '',
      title: t.title, summary: t.summary, severity: t.severity,
      components: t.components, is_structural: t.is_structural,
      lenses: t.lenses, findings: t.findings, dedup: t.dedup,
    }});
  }});
  const blob = new Blob([JSON.stringify({{report_key: KEY, version: DATA.version, triaged: out}}, null, 2)],
    {{type:'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'triage.json'; a.click();
}}
function resetTriage() {{
  if (!confirm('Clear all triage marks and notes for this report?')) return;
  localStorage.removeItem(KEY);
  document.querySelectorAll('.t-notes').forEach(t => t.value='');
  document.querySelectorAll('.card').forEach(c => paint(c.getAttribute('data-id')));
  updateProgress();
}}
document.querySelectorAll('.card').forEach(c => paint(c.getAttribute('data-id')));
updateProgress();
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("findings", help="path to findings JSON from the campfire-review workflow")
    ap.add_argument("-o", "--output", default="report.html", help="output HTML path")
    args = ap.parse_args()

    with open(args.findings) as fh:
        data = json.load(fh)

    html_out = render(data, source_name=args.findings)
    with open(args.output, "w") as fh:
        fh.write(html_out)
    print(f"Wrote {args.output} ({len(data.get('themes', []))} themes for triage).")


if __name__ == "__main__":
    sys.exit(main())
