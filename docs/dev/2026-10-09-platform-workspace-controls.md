---
title: Platform administrator workspace controls
form: trace
updated: 2026-10-09
status: active
tags: [implementation, permissions, workspace]
---

# Platform administrator workspace controls

- Platform administrators can rename, fork, share, and delete workspaces owned by other groups, including private ones. They can also upload or delete files and create, change, run, fork, stop, or delete chats there.
- Forks belong to the requesting administrator and use their group quota; existing-workspace file changes use the owner's group quota. Active-run lock restrictions still apply, while the exclusive run-lock setting remains owner-only.
- Workspace cards expose the controls and inline rename; chat and file tools are available in private workspaces. Added backend and browser permission regression cases. Tests were not run on this local machine per the earlier request.
