// Identity-specific controls stay separate from the main event bindings.
export function configureIdentityUi(state, api) {
  const enabled = state.runtimeConfig.authMode === "oidc";
  const zh = state.lang === "zh";
  const labels = zh ? {
    customer: "客户", newCustomer: "添加新客户", customerId: "客户 ID", customerName: "客户名称",
    addCustomer: "添加", fullDelete: "完全删除", changeCustomer: "更改客户",
    customerHelp: "中央归属变更会影响所有平台的准入，不会更改本地小组。",
  } : {
    customer: "Customer", newCustomer: "Add new customer", customerId: "Customer ID", customerName: "Customer name",
    addCustomer: "Add", fullDelete: "Full deletion", changeCustomer: "Change customer",
    customerHelp: "Changes central membership across platforms, not local groups.",
  };
  document.querySelectorAll("[data-identity-label]").forEach((element) => {
    element.textContent = labels[element.dataset.identityLabel] || "";
  });
  document.querySelectorAll('#login-form input').forEach((input) => {
    input.disabled = enabled;
    input.closest("label")?.classList.toggle("hidden", enabled);
  });
  document.querySelector("#identity-customer-fields")?.classList.toggle("hidden", !enabled);
  const customerSelect = document.querySelector("#identity-customer-select");
  if (customerSelect) { customerSelect.required = enabled; customerSelect.disabled = !enabled; }
  const existing = document.querySelector("#identity-existing-login");
  if (existing) {
    existing.classList.toggle("hidden", !enabled);
    existing.onclick = () => {
      const invite = document.querySelector('#register-form [name="inviteToken"]').value.trim();
      window.location.assign("/api/oidc/login" + (invite ? "?inviteToken=" + encodeURIComponent(invite) : ""));
    };
  }
  const add = document.querySelector("#identity-add-customer");
  if (add) {
    add.classList.toggle("hidden", !enabled || Boolean(state.runtimeConfig.deploymentCustomerId));
    add.onclick = async () => {
      try {
        const id = document.querySelector("#identity-customer-id").value.trim();
        const name = document.querySelector("#identity-customer-name").value.trim();
        const result = await api("/api/customers", { method: "POST", body: JSON.stringify({ id, name }) });
        state.customers = result.customers;
        renderCustomers(result.customers, id);
        document.querySelector("#identity-customer-id").value = "";
        document.querySelector("#identity-customer-name").value = "";
        document.querySelector(".identity-customer-create").open = false;
      } catch (error) {
        document.querySelector("#batch-account-error").textContent = error.message;
      }
    };
  }
  document.querySelectorAll("[data-identity-customer-create]").forEach((element) => {
    element.classList.toggle("hidden", !enabled || Boolean(state.runtimeConfig.deploymentCustomerId));
  });
  if (enabled) {
    const label = document.querySelector('#login-form button[type="submit"]');
    if (label) label.textContent = zh ? "统一账号登录" : "Sign in with shared account";
  }
}

export function renderCustomers(customers, selected = "", selector = "#identity-customer-select") {
  const select = document.querySelector(selector);
  if (!select) return;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = document.documentElement?.lang?.startsWith("zh") ? "选择客户" : "Choose customer";
  placeholder.disabled = true;
  placeholder.selected = placeholder.defaultSelected = !selected;
  select.replaceChildren(placeholder, ...(customers || []).filter((customer) => customer.enabled !== false).map((customer) => {
    const option = document.createElement("option");
    option.value = customer.id;
    option.textContent = `${customer.name} (${customer.id})`;
    option.selected = option.defaultSelected = customer.id === selected;
    return option;
  }));
}
