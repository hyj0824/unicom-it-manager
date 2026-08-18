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
