# Report Artifacts

Every producing skill renders its output as self-contained HTML under `/tmp/{project-slug}/reports/`.
This reference owns the shared etiquette; each skill states only its own output filename and data shape.

## Rendering

- Copy the skill's template to the output path, replacing **only the data block** between its `*_DATA_START` / `*_DATA_END` markers.
  The rendering engine below the markers is generic and reads only that shape - never touch it on a data refresh.
- Before first authoring or restyling a template, load an installed artifact or frontend design skill; a plain data refresh on an existing template doesn't need it again.
- Open the rendered file with the host's browser integration when available; otherwise give the user a clickable local path.
  Do not fail solely because GUI launch is unavailable.

## Publishing

The local file is the deliverable.
Publish with an artifact-publishing tool only when the user asks for a shareable link, using a stable per-skill favicon and a title and description naming the artifact's subject.
Never publish unprompted, and if the host has no publisher, the local HTML remains the deliverable - say so instead of apologizing.

## Reading another skill's report

Consumers of a rendered report read its data block, not the whole file, and extract only the fields they need.
In particular, `E2E_DATA` embeds one base64 `dataUri` per screenshot step: never ingest those payloads - scenario ids, titles, statuses, and the `summary` are the useful content.
