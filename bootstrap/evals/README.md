# Bootstrap evals

Two levels of verification:

1. **Unit tests, free**: `python3 -m unittest discover -s bootstrap/evals/tests -t .`, run on every `scripts/validate.sh` invocation as check B01.
   `test_check_agents_md.py` proves the checker's CLI contract against scratch repositories.
   `test_concept_sources.py` keeps the concept map, the template, and the checker agreeing, and fails when a dev source file the map was distilled from moves or is renamed; it cannot see a source whose content changed in place.
2. **Functional runs, paid and manual**: `agents-md.json` lists the fixtures and assertions, and `results.md` records the runs.

## The functional procedure

1. For each eval, make a scratch copy of a repository matching its fixture with `git clone --local`, so history and `origin/HEAD` come along and the live repository is never touched.
2. Install the plugin (`/plugin install bootstrap@nurbot`, or `codex plugin add bootstrap@nurbot`) and invoke `/bootstrap:agents-md` in the copy with the eval's prompt.
3. Answer the interview as the fixture's owner would.
4. Grade the run against the eval's assertions and the `every_eval` list, then record the result in `results.md`.
