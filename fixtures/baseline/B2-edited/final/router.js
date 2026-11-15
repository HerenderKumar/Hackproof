// Routing for the API, kept small on purpose.

function matchRoute(routes, method, path) {
  for (const route of routes) {
    if (route.method !== method) continue;
    const found = route.parsed.regex.exec(path);
    if (!found) continue;
    const values = {};
    route.parsed.params.forEach((name, index) => {
      values[name] = decodeURIComponent(found[index + 1]);
    });
    return { handler: route.handler, params: values };
  }
  return null;
}

const routes = [];

function addRoute(method, path, handler) {
  routes.push({ method, parsed: parseRoute(path), handler });
}

function parseRoute(path) {
  const parts = path.split("/").filter(Boolean);
  const params = [];
  const pattern = parts.map((part) => {
    if (part.startsWith(":")) {
      params.push(part.slice(1));
      return "([^/]+)";
    }
    return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  });
  return { regex: new RegExp(`^/${pattern.join("/")}/?$`), params };
}

module.exports = { addRoute, matchRoute, routes };
