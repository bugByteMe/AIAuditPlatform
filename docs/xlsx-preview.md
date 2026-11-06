---
title: XLSX Preview with SheetJS
form: implementation-plan
updated: 2026-11-06
status: proposed
tags: [frontend, workspace, preview, xlsx, sheetjs]
---

# XLSX Preview with SheetJS

## Goal and scope

Add read-only `.xlsx` previews to the existing authorized file-preview modal.
The first release will display workbook sheets, formatted cell values, merged
cells, basic row/column sizing, and sheet visibility. Editing, recalculation,
password-protected workbooks, macros, charts, images, pivot-table interaction,
and pixel-perfect Excel rendering are out of scope.

The preview is convenience UI only. Downloading the original workbook remains
the authoritative way to inspect unsupported Excel features.

## Dependency and delivery

- Pin a reviewed SheetJS Community Edition release rather than using a moving
  `latest` URL. The implementation should record the exact version and checksum.
- Store the browser distribution and upstream license under
  `frontend/vendor/sheetjs/`, consistent with the existing no-build, locally
  vendored Fancytree and jQuery dependencies. Production clients must not fetch
  executable code from a CDN.
- Load SheetJS only when an eligible workbook is opened. Parsing runs in a
  dedicated Web Worker so a large or malformed workbook cannot freeze chat or
  navigation UI.

SheetJS's standalone-browser documentation supports both local vendoring and Web
Workers, which matches the current frontend architecture. The implementation
should re-check the pinned release notes and license before merging.

## Request and permission flow

1. The existing preview metadata request continues to enforce workspace access
   and path containment. For an eligible `.xlsx`, it returns `mode: "xlsx"`, the
   actual byte size, and the configured preview limits.
2. Add an authenticated XLSX-preview content route that rechecks access, suffix,
   actual size, and workspace path before streaming the bytes. It must reject
   over-limit files before reading or returning the body. Do not expose storage
   paths or create a public preview URL.
3. The browser fetches the workbook with an `AbortController`, transfers its
   `ArrayBuffer` to the worker, and terminates the worker when the modal closes,
   the selected workspace changes, or parsing exceeds the time limit.
4. The worker returns a bounded, plain-data view model. It does not return the
   workbook object or generated HTML to the main thread.

The raw download endpoint remains unchanged and is not used as a way to bypass
preview limits.

## Parsing and rendering

- Parse from an `ArrayBuffer` with dense worksheet storage. Do not evaluate
  formulas or follow external workbook links. Show cached formula results when
  present and optionally expose the formula text as inert text.
- Convert only the selected sheet into a bounded matrix. Preserve cell display
  text, types, merge ranges, hidden-sheet state, and practical row/column sizes.
- Render with application-created DOM nodes and `textContent`; do not inject
  workbook strings or SheetJS-generated HTML through `innerHTML`.
- Use sheet tabs with clear badges for hidden and very-hidden sheets. All sheets
  remain inspectable because hidden audit data may still be relevant.
- Use a scrollable, virtualized grid with sticky row/column headings. Switching
  sheets asks the worker for that sheet's bounded view instead of materializing
  every worksheet in the DOM.
- Render hyperlinks as plain text in the first release. Comments, external
  references, drawings, and unsupported objects receive a non-blocking warning.

## Resource limits and failure behavior

Introduce explicit server-controlled limits, with initial values selected after
testing representative audit workbooks:

- maximum compressed workbook bytes;
- maximum parse time;
- maximum sheets exposed;
- maximum rows, columns, and non-empty cells returned per sheet; and
- maximum worker response bytes.

The UI must distinguish an unsupported workbook, an over-limit workbook, a
password-protected workbook, malformed content, parsing timeout, and an ordinary
network failure. Truncated previews show their row/column limits and retain the
download action. No preview failure may affect the stored file or workspace
snapshot.

## Delivery sequence

1. Vendor and pin SheetJS; add license/checksum records and a minimal worker
   wrapper with parser unit tests.
2. Add backend preview-mode detection, the bounded content route, configuration,
   permission tests, and audit-safe error responses.
3. Add the workbook worker protocol, cancellation/timeout handling, and tests for
   malformed, oversized, formula, hidden-sheet, merged-cell, and hostile-string
   fixtures.
4. Add the modal sheet tabs and virtualized grid, localized states, responsive
   styling, keyboard navigation, and browser tests.
5. Measure memory and interaction latency with representative small, wide, deep,
   sparse, and compressed workbooks before enabling the mode by default.

## Acceptance criteria

- An authorized user can preview a supported `.xlsx` without downloading it;
  an unauthorized user cannot fetch metadata or workbook bytes.
- Opening and closing large previews leaves the rest of the application
  responsive and releases fetch/worker resources.
- Workbook text cannot create markup, run scripts, evaluate formulas, or trigger
  external navigation/network requests.
- Limits and truncation are visible, deterministic, server-configurable, and
  covered by tests.
- Existing text, image, PDF, unsupported-Office, and download behaviors remain
  unchanged.
