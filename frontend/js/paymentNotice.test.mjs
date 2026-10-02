import test from "node:test";
import assert from "node:assert/strict";
import { renderPaymentNotice } from "./paymentNotice.js";
import { state } from "./state.js";

function fixture() {
  const nodes = new Map();
  return { querySelector(id) {
    if (!nodes.has(id)) {
      const classes = new Set();
      nodes.set(id, { textContent: "", classList: {
        toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
        add(name) { classes.add(name); }, contains(name) { return classes.has(name); },
      } });
    }
    return nodes.get(id);
  } };
}

test("payment received is prominent without prematurely claiming balance credit", () => {
  state.lang = "zh";
  const root = fixture();
  for (const status of ["paid", "crediting", "applied"]) {
    renderPaymentNotice(root, { status, amountCny: "50.00" });
    assert.equal(root.querySelector("#recharge-payment-notice").classList.contains("hidden"), false);
    assert.equal(root.querySelector("#recharge-qr-image").classList.contains("hidden"), true);
    assert.equal(root.querySelector("#recharge-payment-notice-amount").textContent, "¥50.00");
    assert.equal(root.querySelector("#recharge-payment-notice-title").textContent, status === "applied" ? "充值成功" : "支付成功");
  }
});

test("non-success orders and opening another order restore the ordinary status", () => {
  const root = fixture();
  renderPaymentNotice(root, { status: "applied", amountCny: "100" });
  for (const status of [undefined, "pending", "expired", "closed", "failed", "review_required"]) {
    renderPaymentNotice(root, { status });
    assert.equal(root.querySelector("#recharge-payment-notice").classList.contains("hidden"), true);
    assert.equal(root.querySelector("#recharge-order-status").classList.contains("hidden"), false);
  }
});
