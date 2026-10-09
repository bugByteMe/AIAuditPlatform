---
title: Chat composer top-edge resize
form: trace
updated: 2026-10-09
status: active
tags: [implementation, chat, accessibility]
---

# Chat composer top-edge resize

- Added a visible top-edge grip because the native textarea resize corner sits at the bottom of a viewport-anchored composer and cannot be dragged downward reliably.
- Dragging up expands the textarea and dragging down shrinks it; the grip also supports Arrow Up/Down and Home/End, and has localized accessible labels. The native resize corner remains available.
- Preserved the existing 82px minimum, 320px/35dvh maximum, multiline input, and separate send controls. Updated browser coverage for desktop and narrow layouts. Tests were not run locally as requested.
