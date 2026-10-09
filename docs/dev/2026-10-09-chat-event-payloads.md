---
title: Compact browser chat event payloads
form: trace
updated: 2026-10-09
status: active
tags: [implementation, chat, performance]
---

# Compact browser chat event payloads

- Browser-facing history and live SSE responses omit the stored `raw` runner payload. This prevents unused aggregated command output from being transferred with each chat page and live event.
- Event IDs, messages, run IDs, status, and tool-call IDs remain available for rendering, pagination, and live cursor reconciliation. Full raw events remain stored without mutation.
- Added focused HTTP history and SSE regression cases. Tests were not run locally per the earlier request; static checks only.
