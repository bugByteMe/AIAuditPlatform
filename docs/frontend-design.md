# Frontend Design System

The frontend uses a deliberately small set of visual primitives so new controls do not introduce one-off styling.

## Radius scale

- `--radius-control: 8px` applies to buttons, inputs, selectors, compact toggles, and other interactive controls.
- `--radius-container: 12px` applies to panels, cards, menus, dialogs, notifications, and content containers.
- `--radius-pill: 999px` is reserved for pills, badges, meters, and progress tracks.
- Circular indicators and avatars may continue to use `50%`. Structural square edges may use `0` where adjoining regions must meet.

These variables are defined in `frontend/css/base.css`. Component styles should reference the variables rather than adding new radius literals.

## Icons

Interface icons come from the inline SVG symbol sprite in `frontend/index.html` and use the shared `.ui-icon` class. Static markup references a symbol with `<use href="#icon-name">`; JavaScript-rendered markup uses the equivalent SVG helper.

Unicode characters must not be used as button, navigation, disclosure, loading, or file-type icons. Mathematical symbols inside rendered chat content and semantic text such as currency amounts are not interface icons and remain text.

## File trees

All workspace file-tree surfaces use the shared adapter in `frontend/js/fileTreeAdapter.js`. jQuery 3.7.1 and Fancytree 2.38.5 are pinned under `frontend/vendor/` with their license files and are loaded before the application module, so the application does not depend on a CDN or a frontend build step. The application supplies its own skin using the shared radius tokens and SVG icon sprite; stock Fancytree themes are not used.

The chat explorer and artifact selector enable lazy loading, keyboard navigation, F2/context-menu rename, and same-workspace drag/drop. The upload destination tree deliberately remains selection-only. File moves reject conflicts instead of overwriting or auto-renaming a destination.

The chat explorer and workspace artifact tree additionally accept external file and directory drops on folder nodes or the workspace root. Composer drops either insert the application-specific path payload from an existing tree node or upload external content before inserting its workspace-relative top-level paths. Drop targets use the shared SVG icon sprite, radius tokens, resumable progress, and permission/lock state rather than introducing a separate uploader.

## Responsive layout

Responsive behavior is based on the effective CSS viewport, including the
smaller viewport produced by browser zoom. The application must not counteract
zoom with transforms or a reduced root font size.

- At 1200px and wider, the full vertical sidebar and resizable chat side panels
  use the desktop layout.
- From 800px through 1199px, navigation becomes a 76px icon rail, secondary
  top-bar actions move into the shared overflow menu, and chat side-panel widths
  are clamped so the conversation keeps the majority of the available width.
- Below 800px, navigation becomes a short app bar. Session and file panels are
  closed initially and open as overlay drawers, without replacing the user's
  wider-layout fold preferences.

The chat header and composer use container queries because their usable width
depends on sidebar and chat-panel state, not only on the browser viewport.
Controls retain accessible target sizes while spacing, panel widths, and
secondary chrome become denser. Page-level horizontal scrolling and panel rails
covering composer controls are regressions.
