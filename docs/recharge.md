# Recharge Panel

## Scope

The Recharge (`充值`) view is available to every authenticated user alongside Workspaces and Live Chat. It provides:

- the signed-in user's managed MicuAPI inference URL;
- the signed-in user's managed MicuAPI key, masked by default with explicit reveal and copy controls;
- fixed recharge choices of CNY 50, CNY 100, and CNY 200;
- a payment QR dialog that reminds the payer to put their platform username in the payment note.

The API key is returned only by the authenticated `GET /api/recharge` endpoint for the current account. It is not included in public user or administrator account-list responses, and it is not written to the audit log.

## Payment Assets

Deploy the three QR images under the frontend static root:

- `frontend/assets/payment-qr/50.png`
- `frontend/assets/payment-qr/100.png`
- `frontend/assets/payment-qr/200.png`

The UI displays a configuration warning if the selected image is absent. QR images are deployment assets and should not be committed when they expose sensitive payment-account information.

## Reconciliation

This panel presents payment instructions only. It does not verify payment, process a callback, or change MicuAPI balance automatically. After matching a received payment by the username in its note, an administrator uses the existing budget action to add the paid amount to the user's current MicuAPI balance. Token usage remains informational and is not reset.
