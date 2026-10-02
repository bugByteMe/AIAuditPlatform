import { t } from "./i18n.js";

export function renderPaymentNotice(root, order) {
  const received = ["paid", "crediting", "applied"].includes(order?.status);
  root.querySelector("#recharge-payment-heading").textContent = t(received ? "recharge.wechatPay" : "recharge.payTitle");
  root.querySelector("#recharge-payment-notice").classList.toggle("hidden", !received);
  root.querySelector("#recharge-scan-help").classList.toggle("hidden", received);
  root.querySelector("#recharge-order-status").classList.toggle("hidden", received);
  if (!received) return;
  root.querySelector("#recharge-qr-image").classList.add("hidden");
  root.querySelector("#recharge-qr-missing").classList.add("hidden");
  root.querySelector("#recharge-payment-notice-title").textContent = t(order.status === "applied" ? "recharge.rechargeComplete" : "recharge.paymentReceived");
  const amount = Number(order.amountCny);
  root.querySelector("#recharge-payment-notice-amount").textContent = Number.isFinite(amount) && amount > 0 ? `¥${amount.toFixed(2)}` : "";
  root.querySelector("#recharge-payment-notice-detail").textContent = t(`recharge.order.${order.status}`);
}
