const FOCUSABLE = 'button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function visibleControls(modal) {
  return [...modal.querySelectorAll(FOCUSABLE)].filter((element) => element.getClientRects().length > 0);
}

export function manageModalFocus() {
  const modals = [...document.querySelectorAll(".modal-backdrop")];
  let lastExternalFocus = null;
  const openers = new WeakMap();

  document.addEventListener("focusin", (event) => {
    if (!event.target.closest(".modal-backdrop")) lastExternalFocus = event.target;
  });

  modals.forEach((modal) => {
    let wasOpen = !modal.classList.contains("hidden");
    const observer = new MutationObserver(() => {
      const isOpen = !modal.classList.contains("hidden");
      if (isOpen === wasOpen) return;
      wasOpen = isOpen;
      if (isOpen) {
        openers.set(modal, lastExternalFocus);
        if (!modal.contains(document.activeElement)) (visibleControls(modal)[0] || modal).focus({ preventScroll: true });
      } else {
        const opener = openers.get(modal);
        if (opener?.isConnected && opener.getClientRects().length) opener.focus({ preventScroll: true });
      }
    });
    modal.tabIndex = -1;
    observer.observe(modal, { attributes: true, attributeFilter: ["class"] });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const modal = modals.findLast((item) => !item.classList.contains("hidden"));
    if (!modal) return;
    const controls = visibleControls(modal);
    if (!controls.length) {
      event.preventDefault();
      modal.focus();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
      event.preventDefault();
      first.focus();
    }
  });
}
