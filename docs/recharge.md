# Recharge Panel

## Scope

The Recharge (`充值`) view is available to every authenticated user alongside Workspaces and Live Chat. It provides:

- the signed-in user's managed MicuAPI inference URL;
- the signed-in user's managed MicuAPI key, masked by default with explicit reveal and copy controls;
- WeChat Pay Native choices of CNY 50, CNY 100, and CNY 200;
- a CNY 1 debug choice for low-value integration checks;
- a per-order QR dialog with live payment and credit status;
- successful callback- or spreadsheet-confirmed recharge history showing payment time and the original face amount.

The API key is returned only by the authenticated `GET /api/recharge` endpoint for the current account. It is not included in public user or administrator account-list responses, and it is not written to the audit log.

## WeChat Pay Native Orders

`POST /api/recharge/orders` creates a unique 15-minute Native Pay order for the
signed-in user. Supported face amounts and credits are:

- CNY 1 -> CNY 0.80 (debug only)
- CNY 50 -> CNY 40
- CNY 100 -> CNY 80
- CNY 200 -> CNY 160

The backend stores the authenticated user's order before requesting its
`code_url`. WeChat calls `POST /api/payments/wechat/notify` through the public
HTTPS proxy. The backend verifies the raw request signature and timestamp,
decrypts the resource using the API v3 key, and validates the AppID, merchant
ID, order number, currency, and exact amount.

The transaction ID is hashed before persistence and reserved before the MicuAPI
balance call. This prevents duplicate credit across callbacks, order queries,
and spreadsheet imports. Uncertain balance results become `review_required`
and are not retried automatically. A reconciliation loop queries pending orders
after missed callbacks or restarts and closes expired unpaid orders.

The CNY 1 option is deliberately marked as debug-only. Remove it from
`RECHARGE_PRODUCTS` and the matching frontend button if it should not remain in
production.

## Spreadsheet Reconciliation

System administrators can upload an `.xlsx` payment export, preview its validation results, and confirm eligible records. The Linux control plane reads workbooks with `openpyxl`; it resets worksheet dimensions before sequential iteration because supported payment exports may incorrectly declare only `A1` as used.

Only sheets whose metadata contains `收款项：¥50.00 -`, `收款项：¥100.00 -`, or `收款项：¥200.00 -` qualify; the CNY 1 debug product is callback-only. The importer dynamically locates the detail header and requires `支付时间`, `支付金额`, `收款项`, `支付单号`, `订单状态`, and `用户名`. A row is eligible only when it is marked `支付成功`, its amount and payment item match the sheet denomination, its username matches an active account case-insensitively, and the account has a managed MicuAPI binding.

Confirmation credits 80% of the face amount: CNY 40, 80, or 160. Payment-number hashes are globally unique and persisted before the external balance call, so repeated or concurrent imports cannot credit the same payment twice. Provider errors are retained as `review_required` and are never retried automatically because the remote result may be uncertain.

After a credit succeeds, the resulting MicuAPI balance becomes the account's new recharge baseline. The user-facing remaining percentage is current balance divided by that baseline, so it is 100% immediately after the recharge and falls as the balance is consumed.

The uploaded workbook and complete payment numbers are not retained. Successful user history shows the original CNY 50, 100, or 200 face amount and payment time; it does not expose the reduced credit amount. Account deletion removes identifying history while retaining an anonymous payment hash tombstone for duplicate protection. Token usage remains informational and is not reset.
