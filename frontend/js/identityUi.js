// Identity-specific controls stay separate from the main event bindings.
export function configureIdentityUi(state, api) {
  const enabled = state.runtimeConfig.authMode === "oidc";
  const zh = state.lang === "zh";
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
        renderCustomers(result.customers, id);
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

export function renderCustomers(customers, selected = "") {
  const select = document.querySelector("#identity-customer-select");
  if (!select) return;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "选择客户 / Choose customer";
  placeholder.disabled = true;
  placeholder.selected = placeholder.defaultSelected = !selected;
  select.replaceChildren(placeholder, ...(customers || []).map((customer) => {
    const option = document.createElement("option");
    option.value = customer.id;
    option.textContent = `${customer.name} (${customer.id})`;
    option.selected = option.defaultSelected = customer.id === selected;
    return option;
  }));
}
