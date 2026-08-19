/* 通用弹窗工具：页面按钮通过 data-modal-open 打开，data-field-* 填充编辑值。 */
(function setupModals() {
  const modalById = (value) => {
    if (!value) return null;
    return typeof value === "string" ? document.getElementById(value) : value;
  };

  function closeModal(value) {
    const modal = modalById(value);
    if (!modal) return;
    modal.classList.remove("is-open");
    document.body.classList.remove("modal-open");
  }

  function openModal(value, trigger) {
    const modal = modalById(value);
    if (!modal) return;
    const form = modal.querySelector("[data-modal-form]");
    if (trigger && form) {
      form.reset();
      form.action = trigger.dataset.editAction || form.dataset.defaultAction || form.action;
      const title = modal.querySelector("[id$='-modal-title']");
      if (title && trigger.dataset.modalTitle) title.textContent = trigger.dataset.modalTitle;
      modal.querySelectorAll("[data-modal-field]").forEach((field) => {
        const key = field.dataset.modalField;
        const value = trigger.getAttribute(`data-field-${key}`);
        if (value !== null) field.value = value;
      });
    }
    modal.classList.add("is-open");
    document.body.classList.add("modal-open");
    const first = modal.querySelector("input:not([type='hidden']), select, textarea");
    first?.focus();
  }

  window.openModal = openModal;
  window.closeModal = closeModal;

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-modal-open]");
    if (opener) {
      event.preventDefault();
      openModal(opener.dataset.modalOpen, opener);
      return;
    }
    const closer = event.target.closest("[data-modal-close]");
    if (closer) closeModal(closer.closest(".modal"));
  });

  document.addEventListener("click", (event) => {
    const modal = event.target.classList.contains("modal-backdrop")
      ? event.target.closest(".modal")
      : null;
    if (modal) closeModal(modal);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const modal = document.querySelector(".modal.is-open");
    if (modal) closeModal(modal);
  });

  document.addEventListener("submit", (event) => {
    if (event.target.matches("[data-modal-form]")) {
      closeModal(event.target.closest(".modal"));
    }
  });

  document.querySelectorAll(".modal[data-modal-auto-open]").forEach((modal) => openModal(modal));
})();

function bindLookup(input) {
  const list = document.getElementById(input.getAttribute("list"));
  const hidden = document.getElementById(input.dataset.lookupTarget);
  if (!list || !hidden) return;
  const sync = () => {
    const option = Array.from(list.options).find((item) => item.value === input.value.trim());
    hidden.value = option?.dataset.id || (/^\d+$/.test(input.value.trim()) ? input.value.trim() : "");
  };
  input.addEventListener("input", sync);
  input.addEventListener("change", sync);
  if (input.value && !hidden.value) sync();
}

document.querySelectorAll("[data-lookup-target]").forEach(bindLookup);

document.addEventListener("submit", (event) => {
  const form = event.target;
  const message = form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

document.querySelectorAll(".dictionary-input[list]").forEach((input) => {
  const list = document.getElementById(input.getAttribute("list"));
  if (!list) return;

  const values = Array.from(list.options, (option) => option.value);
  const refreshSuggestions = () => {
    const query = input.value.trim().toLocaleLowerCase();
    const matches = values
      .filter((value) => value.toLocaleLowerCase().includes(query))
      .slice(0, 20);
    list.replaceChildren(...matches.map((value) => {
      const option = document.createElement("option");
      option.value = value;
      return option;
    }));
  };

  input.addEventListener("input", refreshSuggestions);
  input.addEventListener("focus", refreshSuggestions);
});

const correctionPicker = document.querySelector("[data-correction-picker]");
if (correctionPicker) {
  const fields = Array.from(document.querySelectorAll("[data-correction-field]"));
  correctionPicker.addEventListener("change", () => {
    const selected = correctionPicker.value;
    fields.forEach((field) => {
      field.hidden = selected !== "all" && field.dataset.correctionField !== selected;
    });
    if (selected !== "all") {
      const input = document.querySelector(`[data-correction-field="${CSS.escape(selected)}"] input`);
      input?.focus();
    }
  });
}

document.querySelectorAll("[data-import-date]").forEach((input) => {
  const raw = input.value.trim();
  if (/^\d{8}$/.test(raw)) {
    input.value = `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  } else if (/^\d{4}-\d{2}-\d{2}/.test(raw)) {
    input.value = raw.slice(0, 10);
  }
});
