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

