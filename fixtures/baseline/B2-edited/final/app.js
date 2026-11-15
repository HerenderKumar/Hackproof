const state = { items: [], filter: "" };

function render(list) {
  const visible = state.items.filter((item) => item.name.includes(state.filter));
  list.replaceChildren(
    ...visible.map((item) => {
      const row = document.createElement("li");
      row.textContent = `${item.name} (${item.count})`;
      row.dataset.slug = slugify(item.name);
      return row;
    }),
  );
}

function slugify(text) {
  return text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function debounce(fn, waitMs) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), waitMs);
  };
}

function start(input, list) {
  input.addEventListener(
    "input",
    debounce(() => {
      state.filter = input.value.trim();
      render(list);
    }, 150),
  );
  fetch("/api/items")
    .then((response) => response.json())
    .then((items) => {
      state.items = items;
      render(list);
    });
}

start(document.querySelector("#filter"), document.querySelector("#items"));
