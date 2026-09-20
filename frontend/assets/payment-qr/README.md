# Payment QR assets

Place the production payment QR images in this directory with these exact names:

- `50.png`
- `100.png`
- `200.png`

The recharge panel reports a clear configuration message when an image is missing. Keep the images out of source control when they contain account-sensitive payment details, and deploy them through the environment's secret/static-asset process.
