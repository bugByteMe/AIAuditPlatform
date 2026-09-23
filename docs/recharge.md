# Recharge Panel

## Scope

The Recharge (`充值`) view is available to every authenticated user alongside Workspaces and Live Chat. It provides:

- the signed-in user's managed MicuAPI inference URL;
- the signed-in user's managed MicuAPI key, masked by default with explicit reveal and copy controls;
- fixed recharge choices of CNY 50, CNY 100, and CNY 200;
- a payment QR dialog that reminds the payer to put their platform username in the payment note.
- successful spreadsheet-confirmed recharge history showing payment time and the original face amount.

The API key is returned only by the authenticated `GET /api/recharge` endpoint for the current account. It is not included in public user or administrator account-list responses, and it is not written to the audit log.

## Payment Assets

Deploy the three QR images under the frontend static root:

- `frontend/assets/payment-qr/50.png`
- `frontend/assets/payment-qr/100.png`
- `frontend/assets/payment-qr/200.png`

The UI displays a configuration warning if the selected image is absent. QR images are deployment assets and should not be committed when they expose sensitive payment-account information.

## Spreadsheet Reconciliation

System administrators can upload an `.xlsx` payment export, preview its validation results, and confirm eligible records. The Linux control plane reads workbooks with `openpyxl`; it resets worksheet dimensions before sequential iteration because supported payment exports may incorrectly declare only `A1` as used.

Only sheets whose metadata contains `收款项：¥50.00 -`, `收款项：¥100.00 -`, or `收款项：¥200.00 -` qualify. The importer dynamically locates the detail header and requires `支付时间`, `支付金额`, `收款项`, `支付单号`, `订单状态`, and `用户名`. A row is eligible only when it is marked `支付成功`, its amount and payment item match the sheet denomination, its username matches an active account case-insensitively, and the account has a managed MicuAPI binding.

Confirmation credits 80% of the face amount: CNY 40, 80, or 160. Payment-number hashes are globally unique and persisted before the external balance call, so repeated or concurrent imports cannot credit the same payment twice. Provider errors are retained as `review_required` and are never retried automatically because the remote result may be uncertain.

After a credit succeeds, the resulting MicuAPI balance becomes the account's new recharge baseline. The user-facing remaining percentage is current balance divided by that baseline, so it is 100% immediately after the recharge and falls as the balance is consumed.

The uploaded workbook and complete payment numbers are not retained. Successful user history shows the original CNY 50, 100, or 200 face amount and payment time; it does not expose the reduced credit amount. Account deletion removes identifying history while retaining an anonymous payment hash tombstone for duplicate protection. Token usage remains informational and is not reset.
