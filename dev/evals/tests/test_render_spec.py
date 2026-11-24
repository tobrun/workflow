"""Unit tests for render-spec.py: the review page is a pure function of spec.md.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_render_spec.py' -t .
Each test writes a spec to a scratch plan directory and runs the script as a
subprocess, so it proves the CLI contract the scope skill relies on.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dev.evals.tests.spec_fixtures import verification

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "skills" / "scope" / "scripts" / "render-spec.py"

SPEC = """# Offline tile cache

2026-11-24

Serve tiles already seen from a local cache so a revisited area renders offline.

Risk: medium - adds a disk cache under the map view

## Research

D-cache-store: Where are cached tiles stored? [impact: high]
  ✓ SQLite file per region - cheap eviction by region (auto-applied at Confidence: 72%) ⚠ a corrupt file loses the region
  ✗ one file per tile - inode pressure ? verify: inode limits on target devices

D-eviction: How is the cache bounded? [impact: medium]
  ✓ 200 MB cap - the product's storage budget (user, 2026-11-20)
  ✗ time-to-live of 7 days - evicts regions a commuter still uses

D-test-layer: Where is the offline path proven? [impact: medium]
  ✓ integration test - the fallback crosses components (auto-applied at Confidence: 45%)
  ✗ unit tests with a mocked cache - pass even if the schema is wrong

## Scope

Inputs: tile requests. D-cache-store (✓ SQLite)

⊘ Prefetching neighbouring tiles - no evidence; reopen with analytics.

### Validation

- `npm test`
- `npm run e2e` (end of build)

{VERIFICATION}## Change plan

1. Tile cache store
   a. `src/cache/TileCache.ts` - SQLite store - decisions: D-cache-store (✓ SQLite)
   b. `src/cache/eviction.ts` - 200 MB cap - decisions: D-eviction (✓ 200 MB)
   tests: [unit] put then get returns identical bytes; [unit] cap exceeded -> oldest region evicted; [integration] real file survives reopen
2. Map view reads through the cache
   a. `src/MapView.ts` - ask the cache first - decisions: D-cache-store (✓ SQLite)
   b. `src/cache/TileCache.ts` - expose has()
   tests: [e2e] airplane mode after visiting -> map renders
3. Settings copy
   a. `src/settings/strings.json` - label for the cache size
   tests: none - copy only
"""
SPEC = SPEC.replace("{VERIFICATION}", verification("unit", "functional", "acceptance"))

OPEN = """D-staleness: When does a cached tile get refreshed? [impact: high] [open]
  ? refresh after 24h - fresh enough for road changes
  ? never refresh - closed roads stay on the map
  ⚑ ask: do style updates invalidate tiles?

"""

FLOW = """### Flow

```mermaid
sequenceDiagram
  MapView->>TileCache: getTiles
```

"""


class RenderSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name) / ".dev" / "offline-cache"
        self.plan.mkdir(parents=True)

    def render(self, text: str = SPEC) -> tuple[subprocess.CompletedProcess, str]:
        spec = self.plan / "spec.md"
        spec.write_text(text, encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), str(spec)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, (self.plan / "spec.html").read_text(encoding="utf-8")

    def card(self, page: str, slug: str) -> str:
        start = page.index(f'<article class="card decision" id="{slug}">')
        return page[start:page.index("</article>", start)]

    def test_page_is_written_beside_the_spec_and_its_path_printed(self) -> None:
        result, _ = self.render()
        self.assertEqual(result.stdout.strip(), str(self.plan / "spec.html"))

    def test_clean_spec_with_nothing_pending_is_ready_to_build(self) -> None:
        _, page = self.render()
        self.assertIn("✓ Ready to build", page)
        self.assertIn("Medium risk", page)

    def test_reach_counts_the_change_sets_and_files_that_cite_a_decision(self) -> None:
        _, page = self.render()
        self.assertIn("2 change sets · 2 files", self.card(page, "D-cache-store"))
        self.assertIn("1 change set · 1 file", self.card(page, "D-eviction"))
        self.assertIn("reach 0", self.card(page, "D-test-layer"))

    def test_cards_carry_impact_confidence_and_who_settled_it(self) -> None:
        _, page = self.render()
        store = self.card(page, "D-cache-store")
        self.assertIn("impact-high", store)
        self.assertIn("72%", store)
        self.assertIn("Auto-applied", store)
        self.assertIn("⚠ 1", store)
        self.assertIn("? verify 1", store)
        self.assertIn("Settled by you", self.card(page, "D-eviction"))

    def test_confidence_below_the_ask_line_is_flagged(self) -> None:
        _, page = self.render()
        self.assertIn('class="meter low"', self.card(page, "D-test-layer"))
        self.assertNotIn('class="meter low"', self.card(page, "D-cache-store"))

    def test_applied_decisions_are_listed_least_confident_first_with_overturn_replies(self) -> None:
        _, page = self.render()
        applied = page[page.index('<table class="applied">'):]
        applied = applied[:applied.index("</table>")]
        self.assertLess(applied.index("D-test-layer"), applied.index("D-cache-store"))
        self.assertNotIn("D-eviction", applied, "a decision the user settled is not offered for overturn")
        self.assertIn('data-reply="D-cache-store: take &quot;one file per tile&quot; instead of '
                      '&quot;SQLite file per region&quot;"', applied)

    def test_open_decision_waits_on_the_user_with_choose_replies(self) -> None:
        _, page = self.render(SPEC.replace("D-eviction:", OPEN + "D-eviction:"))
        self.assertIn("⚑ 1 waiting on you", page)
        self.assertIn('data-reply="D-staleness: choose &quot;refresh after 24h&quot;"', page)
        self.assertIn("ask: do style updates invalidate tiles?", page)

    def test_heatmap_counts_scenarios_per_layer(self) -> None:
        _, page = self.render()
        row = re.search(r"1\. Tile cache store</a></th>(.*?)</tr>", page).group(1)
        self.assertEqual(re.findall(r'class="heat h\d"[^>]*>([^<]+)<', row), ["2", "1", "-"])
        self.assertIn("tests: none - copy only", page)

    def test_build_order_comes_from_lint_spec_waves(self) -> None:
        _, page = self.render()
        self.assertIn("Wave 2", page)
        self.assertIn("2 waits on 1: <code>src/cache/TileCache.ts</code>", page)

    def test_non_goals_and_validation_are_listed(self) -> None:
        _, page = self.render()
        self.assertIn("⊘ Prefetching neighbouring tiles", page)
        self.assertIn("<code>npm run e2e</code> <span class=muted>end of build</span>", page)

    def test_flow_sketch_loads_mermaid_only_when_the_spec_has_one(self) -> None:
        _, page = self.render()
        self.assertNotIn("cdn.jsdelivr.net", page, "no flow, so nothing remote loads")
        self.assertNotIn('<pre class="mermaid">', page)
        _, page = self.render(SPEC.replace("### Validation", FLOW + "### Validation"))
        self.assertIn('<pre class="mermaid">sequenceDiagram\n  MapView-&gt;&gt;TileCache: getTiles</pre>', page)
        self.assertIn("design sketch from the spec, not derived from code", page)
        self.assertIn("cdn.jsdelivr.net/npm/mermaid@11", page)

    def test_lint_problems_render_the_page_as_a_draft(self) -> None:
        result, page = self.render(SPEC.replace("Risk: medium - adds a disk cache under the map view\n", ""))
        self.assertIn("⚑ Draft - lint fails", page)
        self.assertIn("no &#x27;Risk: low|medium|high - why&#x27; line", page)
        self.assertIn("rendered as a draft", result.stdout)

    def test_the_verification_plan_shows_every_kind_with_its_reason(self) -> None:
        _, page = self.render()
        self.assertIn("3 of 20 kinds apply", page)
        self.assertIn('<div class="vk yes"><b>✓ unit</b>', page)
        self.assertIn('<div class="vk no"><b>- fuzz</b><span>not touched by this change</span>', page)

    def test_a_kind_missing_from_the_plan_is_shown_as_a_gap(self) -> None:
        _, page = self.render(SPEC.replace("- penetration: no - not touched by this change\n", ""))
        self.assertIn('<div class="vk missing"><b>? penetration</b>', page)
        self.assertIn("⚑ Draft - lint fails", page)

    def test_the_heatmap_has_a_column_per_tag_the_spec_uses(self) -> None:
        text = SPEC.replace("[unit] cap exceeded -> oldest region evicted", "[property] any insert order -> cap holds").replace(
            "- property: no - not touched by this change", "- property: yes - the cap invariant")
        _, page = self.render(text)
        head = re.search(r'<table class="heatmap"><thead><tr>(.*?)</tr>', page).group(1)
        self.assertEqual(re.findall(r"<th>([^<]+)</th>", head), ["Change set", "Unit", "Integration", "Property", "E2E", "Total"])

    def test_spec_text_is_escaped_not_executed(self) -> None:
        _, page = self.render(SPEC.replace("How is the cache bounded?", "How is <script>alert(1)</script> bounded?"))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)


if __name__ == "__main__":
    unittest.main()
