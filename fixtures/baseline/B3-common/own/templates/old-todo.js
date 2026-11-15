const todos = [];
function addTodo(text) {
  const todo = { id: todos.length + 1, text, done: false };
  todos.push(todo);
  return todo;
}
function toggle(id) {
  const todo = todos.find((t) => t.id === id);
  if (todo) todo.done = !todo.done;
  return todo;
}
module.exports = { addTodo, toggle, todos };
