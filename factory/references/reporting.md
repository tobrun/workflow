# Report Artifacts

Every producing skill renders its output as self-contained HTML in the report directory: `report_dir` from `.dev/factory-run.json` during a factory run ([factory-run.md](factory-run.md)), otherwise `/tmp/{project-slug}/reports/`.
Skill text that names `/tmp/{project-slug}/reports/` means this directory.
This reference owns the shared etiquette; each skill states only its own output filename and data shape.

## Rendering

- Copy the skill's template to the output path, replacing **only the data block** between its `*_DATA_START` / `*_DATA_END` markers.
  The rendering engine below the markers is generic and reads only that shape - never touch it on a data refresh.
- Before first authoring or restyling a template, load an installed artifact or frontend design skill; a plain data refresh on an existing template doesn't need it again.
- Outside a factory run, open the rendered file with the host's browser integration when available; otherwise give the user a clickable local path.
  Do not fail solely because GUI launch is unavailable.

## Publishing

The local file is the deliverable.
During a factory run, never open a browser and never publish; the runner records the path.
Publish with an artifact-publishing tool only when the user asks for a shareable link, using a stable per-skill favicon and a title and description naming the artifact's subject.
Never publish unprompted, and if the host has no publisher, the local HTML remains the deliverable - say so instead of apologizing.

## Reading another skill's report

Consumers of a rendered report read its data block, not the whole file, and extract only the fields they need.
The e2e results are the `factory.e2e/1` record `{report_dir}/{plan-name}-e2e.json`, not the HTML: read that JSON, never open its screenshot files, and take scenario ids, titles, statuses, and assertions from it.
A report's data block is inert JSON written by a render script; never evaluate it.
