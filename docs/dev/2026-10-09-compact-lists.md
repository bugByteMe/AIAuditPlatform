---
title: Compact workspace and administration lists
form: trace
updated: 2026-10-09
status: active
tags: [implementation, frontend, layout]
---

# Compact workspace and administration lists

- Reduced desktop page chrome, panel, workspace-row, and admin-group spacing so more workspaces and users fit within one viewport.
- Kept workspace metadata and user account details visible, with wrapping rather than clipping on narrower screens. Touch-sized workspace actions and user settings controls remain on mobile.
- Added browser assertions for desktop row density and mobile horizontal overflow. Tests were not run locally per the earlier request; static checks only.
