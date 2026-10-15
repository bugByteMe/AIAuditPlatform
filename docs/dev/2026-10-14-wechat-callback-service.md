---
title: Independent WeChat callback ingress
form: trace
updated: 2026-10-14
status: active
tags: [implementation, payments, operations]
---

# Independent WeChat callback ingress

- Added a separate callback image, independent SQLite notification inbox, shared signature/decryption verifier, and private authenticated receipt endpoints. The existing public callback path now targets this service in the Nginx example.
- The main server consumes receipts by querying WeChat for authoritative payment details before its existing idempotent credit logic; periodic order reconciliation remains a fallback. Conditional order-state updates prevent fast callbacks from overwriting credited status.
- Added callback, inbox, consumer, and early-payment regression cases. Tests were not run locally as requested; deploy and verify in staging before switching the Nginx route.
