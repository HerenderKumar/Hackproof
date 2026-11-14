const form = document.getElementById("add-form");
const input = document.getElementById("item");
const list = document.getElementById("items");

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) {
    return;
  }
  const item = document.createElement("li");
  item.textContent = text;
  list.appendChild(item);
  input.value = "";
  input.focus();
});
