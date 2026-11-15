import { nearestWaypoint } from "./distance.js";

// Orders the stops of a day: always the nearest one not yet visited.
export function planDay(start, stops) {
  const left = [...stops];
  const order = [];
  let here = start;
  while (left.length > 0) {
    const next = nearestWaypoint(here, left);
    order.push(next.name);
    left.splice(left.indexOf(next), 1);
    here = next;
  }
  return order;
}

export function summary(order) {
  if (order.length === 0) return "Nothing planned.";
  return `Today: ${order.join(" -> ")} (${order.length} stops)`;
}
