---
name: create-thumbnail
description: Generate a polished 3:2 project thumbnail (SVG + PNG) from a README or any other specified file
argument-hint: "[source-file] [output-name]"
allowed-tools:
  - read
  - glob
  - grep
  - write
  - edit
  - exec
---

Create a project thumbnail image from a source document (default: `README.md` in the
current directory; the user may specify any other file and an optional output name,
default `thumbnail`).

## Step 1 — Understand the project

Read the source file and extract:

- The project name (usually the first heading).
- A short tagline (subtitle, elevator pitch, or the first strong sentence).
- The 3-5 core concepts worth visualizing: the main flow/pipeline, key components,
  technologies, or standout features. Architecture diagrams, tables, and flowcharts in
  the source are the best hints.

## Step 2 — Design the SVG

Write `<output-name>.svg` at 1200x800 (3:2 ratio — required by Devpost, GitHub social
previews, and most galleries). Design rules:

- Dark gradient background (e.g. deep navy `#0b1220` → `#16233f`) with a subtle grid
  (`stroke-opacity: 0.04`), or adapt to any brand colors mentioned in the source.
- Large bold project name top-left with a gradient accent underline; tagline below it
  in a muted light color (max 2 lines, ~34px).
- A simplified visual of the core flow: rounded-rect "cards" for each component,
  connected by gradient arrows. Use small abstract mockups (bars/rects) inside cards
  rather than real text-heavy UI.
- If relevant, a status badge (rounded pill, top-right) and a bottom "strip" listing
  key commands/tools/APIs in monospace.
- Use only `Helvetica, Arial, sans-serif` and `Menlo, Consolas, monospace` font stacks
  (no webfonts — the headless renderer won't load them).
- Keep text minimal and legible at small sizes; nothing important within 40px of edges.

## Step 3 — Render to PNG

Prefer whichever renderer exists (check with `which`):

1. `rsvg-convert -w 1200 -h 800 in.svg -o out.png`
2. `inkscape in.svg -w 1200 -h 800 -o out.png`
3. Headless Chrome (always available if Chrome is installed):

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --disable-gpu --screenshot="<output-name>.png" --window-size=1200,800 \
  "file://$PWD/<output-name>.svg"
```

On Linux use `google-chrome` / `chromium`; on Windows use the Chrome path under
`Program Files`. Ignore benign CVDisplayLink/GPU stderr warnings.

## Step 4 — Verify and iterate

- Read the generated PNG (the read tool renders images) and inspect it: check for
  clipped text, overlapping elements, broken arrows, or empty regions.
- Fix issues in the SVG and re-render. Repeat until it looks clean.
- Confirm the file is under 5 MB.

## Output

Report the PNG and SVG paths, dimensions, and file size. Do not commit or upload
anything unless the user asks.
