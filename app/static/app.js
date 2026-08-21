function insertAtCursor(textarea, value) {
  if (!textarea) return;
  const start = typeof textarea.selectionStart === "number"
    ? textarea.selectionStart
    : textarea.value.length;
  const end = typeof textarea.selectionEnd === "number" ? textarea.selectionEnd : start;
  textarea.value = textarea.value.slice(0, start) + value + textarea.value.slice(end);
  const cursor = start + value.length;
  textarea.focus();
  textarea.setSelectionRange(cursor, cursor);
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

window.insertAtCursor = insertAtCursor;

function updateSchedulePlaceholderGroups(modal) {
  if (!modal) return;
  const selectedType = modal.querySelector("[data-placeholder-type]")?.value || "";
  modal.querySelectorAll("[data-placeholder-group]").forEach((group) => {
    group.hidden = group.dataset.placeholderGroup !== selectedType;
  });
}

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
      const hint = modal.querySelector("[data-modal-hint]");
      if (hint && trigger.dataset.hintText !== undefined) {
        hint.textContent = trigger.dataset.hintText;
      }
      modal.querySelectorAll("[data-modal-field]").forEach((field) => {
        const key = field.dataset.modalField;
        const value = trigger.getAttribute(`data-field-${key}`);
        if (value === null) return;
        if (field.type === "checkbox") {
          field.checked = ["1", "true", "on", "yes"].includes(value.toLowerCase());
        } else {
          field.value = value;
        }
      });
    }
    const previewArea = modal.querySelector("[data-script-preview-area]");
    if (previewArea) {
      previewArea.hidden = true;
      const output = previewArea.querySelector("[data-script-preview-output]");
      if (output) output.textContent = "";
    }
    updateSchedulePlaceholderGroups(modal);
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
    // 只在候选列表中精确匹配时才同步隐藏 ID；输入自定义值或纯数字不视为 ID
    // （曾把不存在的数字直接当 ID 提交，导致新增业务 500 FK 错误）。
    const option = Array.from(list.options).find((item) => item.value === input.value.trim());
    hidden.value = option?.dataset.id || "";
  };
  input.addEventListener("input", sync);
  input.addEventListener("change", sync);
  if (input.value && !hidden.value) sync();
}

document.querySelectorAll("[data-lookup-target]").forEach(bindLookup);

document.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-placeholder-chip][data-token]");
  if (chip) {
    const panel = chip.closest("[data-placeholder-panel]");
    const textarea = panel?.closest("form")?.querySelector("textarea[name='body']");
    if (textarea) insertAtCursor(textarea, chip.dataset.token);
    return;
  }

  const previewButton = event.target.closest("[data-script-preview]");
  if (previewButton) {
    const modal = previewButton.closest(".modal");
    const textarea = modal?.querySelector("textarea[name='body']");
    const previewArea = modal?.querySelector("[data-script-preview-area]");
    const output = previewArea?.querySelector("[data-script-preview-output]");
    if (!textarea || !previewArea || !output) return;

    const examples = {};
    modal.querySelectorAll("[data-placeholder-chip][data-token]").forEach((item) => {
      const match = item.dataset.token.match(/^\{\{\s*([^{}]+?)\s*\}\}$/);
      const token = match?.[1].trim();
      if (token && !Object.prototype.hasOwnProperty.call(examples, token)) {
        examples[token] = item.dataset.example || "";
      }
    });
    output.textContent = textarea.value.replace(/\{\{\s*([^{}]+?)\s*\}\}/g, (match, key) => {
      const normalized = key.trim();
      return Object.prototype.hasOwnProperty.call(examples, normalized)
        ? examples[normalized]
        : match;
    });
    previewArea.hidden = false;
    return;
  }

  const closePreview = event.target.closest("[data-script-preview-close]");
  if (closePreview) {
    const previewArea = closePreview.closest("[data-script-preview-area]");
    if (previewArea) previewArea.hidden = true;
  }
});

const schedulePlaceholderModal = document.getElementById("scan-schedules-modal");
if (schedulePlaceholderModal) {
  schedulePlaceholderModal.addEventListener("input", (event) => {
    if (event.target.matches("[data-placeholder-type], [data-modal-field='scan_type_search']")) {
      updateSchedulePlaceholderGroups(schedulePlaceholderModal);
    }
  });
  schedulePlaceholderModal.addEventListener("change", (event) => {
    if (event.target.matches("[data-placeholder-type], [data-modal-field='scan_type_search']")) {
      updateSchedulePlaceholderGroups(schedulePlaceholderModal);
    }
  });
  updateSchedulePlaceholderGroups(schedulePlaceholderModal);
}

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

document.querySelectorAll("[data-import-date]").forEach((input) => {
  const raw = input.value.trim();
  if (/^\d{8}$/.test(raw)) {
    input.value = `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  } else if (/^\d{4}-\d{2}-\d{2}/.test(raw)) {
    input.value = raw.slice(0, 10);
  }
});
