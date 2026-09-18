# Foreman evals

Status: maintained

Cases captured from real runs at the moment the foreman would have been asked: the digest as it stood after one attempt, the event message for that attempt, and the actions a correct foreman may or may not choose.
They exist so a skill or model change is measured against the failures that used to park runs, and they are the hindsight labeller's check set: `factory history label --check-cases` matches each case to its history world point and prints every disagreement, where the hand label wins.

- `cases/<name>/digest.json` - the run's digest after the named attempt, with repository identity redacted.
- `cases/<name>/event.md` - the `attempt.finished` message for that attempt.
- `cases/<name>/expected.json` - `accept` and `reject` action lists, an optional `allow_override`, and a note.

`run.py` and `capture.py` are thin wrappers over the dream loop in `runner/dream.py` and `runner/history.py`: `run.py` replays each case as a point with `dream.replay` and scores it with `dream.score_point`, and `capture.py` also builds the run's history world and records the case's accept and reject lists as a human label once the run has finished.

Capture a new case from a run on this machine:

```bash
python3 factory/evals/foreman/capture.py ~/.factory/runs/<id> --stage ship --attempt 4 --name <case> \
    --accept publish repair --reject park cancel --note "why this case matters"
```

Run the cases against the real foreman (one paid Codex call per case; never part of `scripts/validate.sh`):

```bash
python3 factory/evals/foreman/run.py
python3 factory/evals/foreman/run.py --case two-ideas-ship-4 --effort high
```

`runner/tests/test_foreman.py` checks offline that every case is well formed: a `factory.digest/1` digest, a non-empty event, and expected actions the decision schema knows.
