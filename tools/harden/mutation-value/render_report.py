"""Render a portable report from measured evidence, with no external assets."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
evidence = {
    name: json.loads((ROOT / "evidence" / f"{name}.json").read_text())
    for name in ("first", "repeat", "ablation")
}
evidence["fault_definitions"] = json.loads((ROOT / "faults.json").read_text())

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Does mutation testing earn its keep?</title>
<style>
:root{color-scheme:light;--bg:#f7f7f5;--panel:#fff;--text:#1b1b18;--muted:#6b6b63;
--border:#e4e2dc;--accent:#b34700;--pass-bg:#e6f4ea;--pass-fg:#1e7a34;
--fail-bg:#fbe9e7;--fail-fg:#b3261e;--code-bg:#f0efe9}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#16151a;--panel:#201f26;
--text:#ecebe6;--muted:#9c9b94;--border:#34333c;--accent:#ff9a5a;
--pass-bg:#133721;--pass-fg:#6fd991;--fail-bg:#3a1a17;--fail-fg:#ff8a80;--code-bg:#29282f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:auto;padding:36px clamp(16px,4vw,48px) 70px}
h1{font-size:clamp(30px,5vw,48px);line-height:1.12;letter-spacing:-.025em;margin:16px 0}
h2{font-size:23px;margin:0 0 14px}h3{font-size:17px;margin:0 0 10px}
p{max-width:78ch}p,h1,h2,h3,td,th,small,code{overflow-wrap:anywhere}
.eyebrow{color:var(--accent);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.12em}
.muted,small{color:var(--muted)}.lead{font-size:19px}.grid{display:grid;
grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.grid>*{min-width:0}
.card,section{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:22px}
section{margin-top:24px}.stat{font-size:36px;line-height:1.2;font-weight:700;color:var(--accent)}
.stat-label{font-size:13px;margin-top:10px}.toolbar{display:flex;flex-wrap:wrap;gap:12px;
justify-content:space-between;align-items:center;margin:20px 0}
button,select{background:var(--panel);color:var(--text);font:inherit;border:1px solid var(--border);
border-radius:6px;padding:8px 12px;max-width:100%;cursor:pointer}
button:hover{border-color:var(--accent)}.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;
padding:12px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-size:12px}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:3px 8px;border-radius:20px;font-size:12px;font-weight:600}
.pass{background:var(--pass-bg);color:var(--pass-fg)}.fail{background:var(--fail-bg);color:var(--fail-fg)}
.bar{height:9px;background:var(--code-bg);border-radius:8px;margin-top:8px;overflow:hidden}
.bar>span{display:block;height:100%;background:var(--accent)}
.notice{border-left:4px solid var(--accent);background:var(--code-bg);padding:14px 18px;border-radius:4px}
.twocol{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.twocol>*{min-width:0}
details{margin-top:14px}summary{cursor:pointer;font-weight:600}
code{font-size:12px}a{color:var(--accent)}ul{padding-left:20px}li{margin:8px 0}
footer{margin-top:28px;color:var(--muted);font-size:12px}
@media(max-width:650px){.grid,.twocol{grid-template-columns:minmax(0,1fr)}section,.card{padding:16px}}
</style></head><body><main>
<header><div class="eyebrow">Quality experiment / 2 projects / repeated measurements</div>
<h1>Mutation testing earned a focused place in review.</h1>
<p class="lead">It pointed to useful regression tests that full coverage missed.
The benefit came from investigating the findings and nearby rules.</p>
<p class="muted">A synthetic demonstration of checkout pricing and access control.
This measures local test sensitivity and runtime, not production ROI.</p></header>
<div class="grid">
<article class="card"><div class="stat">100%</div><div class="stat-label">Line and branch
coverage in every suite, even the weak baselines.</div></article>
<article class="card"><div class="stat">+6 / 16</div><div class="stat-label">More seeded defects
caught by the guided expansion than the equal-sized requirements expansion.</div></article>
<article class="card"><div class="stat">~25x</div><div class="stat-label">Combined guided
mutation runtime versus ordinary tests: 4.92 s versus 0.195 s.</div></article></div>
<section><h2>Same test-count budget, different sensitivity</h2>
<p>Each expansion adds six test functions per project. The requirements arm was
written before viewing mutants; the guided arm followed inspection of the baseline
survivors. Equal output size does not establish equal developer effort.</p>
<div class="toolbar"><label for="project">Explore project
<select id="project"><option value="all">Both projects</option><option value="checkout">Checkout pricing</option>
<option value="access">Access control</option></select></label>
<button id="download">Download measured evidence</button></div>
<div class="scroll"><table><thead><tr><th>Project / suite</th><th>Tests</th>
<th>Generated mutants killed</th><th>Seeded defects caught</th>
<th>Ordinary tests</th><th>Mutation run</th></tr></thead><tbody id="rows"></tbody></table></div>
<p><small>Raw mutation score counts all variants, including duplicates and
out-of-contract wording. Each time is a median: six ordinary runs or two fresh-cache
mutation runs. Defect outcomes and mutant counts repeated exactly.</small></p></section>
<section><h2>A perfect score still missed five defects.</h2>
<div class="twocol"><article><h3>Address just the two access survivors</h3>
<p><span class="pill pass">11 / 11 mutants killed</span></p>
<p>Two added tests reached a 100% score, yet caught only <strong>3 / 8 seeded defects</strong>.
Admin lock bypass, editor expiry bypass, wrong-role owners, viewer access, and
one-second expiry skew remained undetected.</p></article>
<article><h3>Review the surrounding policy</h3>
<p><span class="pill pass">8 / 8 seeded defects caught</span></p>
<p>The six-test guided expansion added role combinations and privileged denial
cases around the findings. It also tested just before expiry. These tests went
beyond directly killing the two survivors.</p></article></div>
<div class="notice">This is a diagnostic ablation with two added tests, not the
matched six-test comparison. It shows why mutation score alone is an inadequate gate.</div></section>
<section><h2>What each suite would have caught</h2>
<p>These separately evaluated faults were authored before mutation inspection.
They are synthetic diagnostic cases, not a blinded independent holdout.</p>
<div class="scroll"><table><thead><tr><th>Project / seeded defect</th><th>Baseline</th>
<th>Requirements</th><th>Guided</th></tr></thead><tbody id="faults"></tbody></table></div></section>
<section><h2>How the findings changed test review</h2><div class="twocol">
<article><h3>Checkout pricing</h3><ul>
<li>Assert the exact discounted total, not merely that a discount happened.</li>
<li>Exercise zero, 99, and exactly 100; test membership asymmetrically.</li>
<li>Check that membership and coupons stack correctly.</li>
</ul></article><article><h3>Access control</h3><ul>
<li>Test exact expiry and the instant just before it.</li>
<li>Split role from ownership with asymmetric cases.</li>
<li>Check denial against a user who otherwise qualifies for access.</li>
</ul></article></div>
<p class="notice">Two checkout survivors only change exception wording. The specification
does not promise that wording. They remain in the raw score: 26 / 28 = 92.9%.
They are observable changes, not equivalent mutants.</p></section>
<section><h2>Where this belongs in our quality workflow</h2>
<p><strong>Adopted workflow:</strong> use a focused mutation pass on changed
decision logic after ordinary tests pass. Treat survivors as concrete review
questions; add regression tests for meaningful contract gaps and inspect nearby
boundary and policy cases. Keep the ordinary test loop fast.</p>
<p>A universal score gate is not supported by these results. A careful
requirements and boundary review could produce the same tests without the tool.
The user adopted this policy on 2026-10-31; dev ship guidance now
selects changed decision-heavy logic and triages survivors against the contract.</p>
<details><summary>What we would measure in a production pilot</summary>
<p>Reviewer minutes per actionable finding, duplicate and out-of-contract findings,
new regression tests, runtime, and detection of independently selected historical
bugs. Compare with equally timed requirements review using separate reviewers
where possible. Avoided-defect value must exceed compute, triage, and maintenance costs.</p>
</details></section>
<section><h2>Evidence and limits</h2>
<p>Two synthetic programs, 24 executable statements, 39 generated mutation variants,
16 seeded faults. The baselines deliberately include plausible weak assertions and
missing boundaries. The same author created all arms and faults, and some faults
overlap mutation operators. These results cannot estimate escaped production bugs
or long-term developer behavior.</p>
<p>All six main arms ran twice from fresh workspaces and caches. No timeout,
suspicious, or untested outcomes were reported. Human effort was not timed.
The tiny pure functions omit integration, I/O, and realistic scale.</p>
<details><summary>Tooling and reproducibility</summary>
<p>Python 3.14.7 on macOS ARM; mutmut 2.4.4, pytest 8.3.3, coverage 7.6.1.
The historical tool version is pinned for this experiment. Full dependency lock,
runner, specs, tests, faults, XML, coverage JSON, survivor diffs, and logs live at
<code>tools/harden/mutation-value/</code>.</p>
<p>The downloadable JSON preserves both main runs and the score-only ablation.
Full reproduction commands and interpretation are in that directory's README.</p></details>
<details><summary>Broader research context</summary>
<p>Google's <a href="https://research.google/pubs/long-term-effects-of-mutation-testing/">2021
long-term study</a> analyzed 15 million mutants and provides observational evidence
of test improvements and relationships to real historical faults.
Its <a href="https://research.google/pubs/practical-mutation-testing-at-scale-a-view-from-google/">scaling
study</a> uses changed-code review and filtering to contain developer and compute cost.
Those findings give context; they do not validate this demonstration's effect size.</p>
</details></section>
<footer>Design follows the repository's E2E report template: warm neutral surfaces,
orange accent, system typography, explicit evidence tables. Standalone; no external assets.</footer>
</main><script id="data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent);
const names={checkout:'Checkout pricing',access:'Access control'};
const suites={baseline:'Baseline',control:'Requirements expansion',guided:'Mutation-guided expansion'};
const median=values=>{const a=[...values].sort((a,b)=>a-b);const m=Math.floor(a.length/2);
return a.length%2?a[m]:(a[m-1]+a[m])/2};
const pill=detected=>`<span class="pill ${detected?'pass':'fail'}">${detected?'Caught':'Missed'}</span>`;
function render(){
const choice=document.getElementById('project').value;
const rows=data.first.results.filter(r=>choice==='all'||r.project===choice)
.sort((a,b)=>a.project.localeCompare(b.project)||['baseline','control','guided'].indexOf(a.suite)
-['baseline','control','guided'].indexOf(b.suite));
document.getElementById('rows').innerHTML=rows.map(r=>{
const repeat=data.repeat.results.find(x=>x.project===r.project&&x.suite===r.suite);
const total=r.mutants.killed+r.mutants.survived+r.mutants.other;
const score=100*r.mutants.killed/total;
const caught=r.faults.filter(f=>f.detected).length;
return `<tr><td><strong>${names[r.project]}</strong><br>${suites[r.suite]}</td>
<td class="num">${r.test_count}</td><td class="num">${r.mutants.killed} / ${total}
(${score.toFixed(1)}%)<div class="bar"><span style="width:${score}%"></span></div></td>
<td class="num">${caught} / ${r.faults.length}<div class="bar">
<span style="width:${100*caught/r.faults.length}%"></span></div></td>
<td class="num">${median([...r.ordinary_seconds,...repeat.ordinary_seconds]).toFixed(3)} s</td>
<td class="num">${median([r.mutation_seconds,repeat.mutation_seconds]).toFixed(3)} s</td></tr>`
}).join('');
document.getElementById('faults').innerHTML=['checkout','access']
.filter(p=>choice==='all'||p===choice).flatMap(p=>data.fault_definitions[p].map(f=>{
const cells=['baseline','control','guided'].map(s=>{
const row=data.first.results.find(r=>r.project===p&&r.suite===s);
return `<td>${pill(row.faults.find(x=>x.id===f.id).detected)}</td>`}).join('');
return `<tr><td><small>${names[p]}</small><br>${f.description}</td>${cells}</tr>`
})).join('');
}
document.getElementById('project').addEventListener('change',render);
document.getElementById('download').addEventListener('click',()=>{
const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
const a=document.createElement('a');a.href=url;a.download='mutation-value-evidence.json';a.click();
setTimeout(()=>URL.revokeObjectURL(url),1000);
});
render();
</script></body></html>
"""

(ROOT / "report.html").write_text(
    HTML.replace("__DATA__", json.dumps(evidence).replace("</", "<\\/"))
)
