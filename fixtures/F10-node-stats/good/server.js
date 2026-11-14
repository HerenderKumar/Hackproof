const express = require("express");

const app = express();

function parseValues(raw) {
  if (typeof raw !== "string") {
    return null;
  }
  const parts = raw.split(",").map((part) => part.trim());
  if (parts.some((part) => part === "")) {
    return null;
  }
  const values = parts.map(Number);
  return values.every(Number.isFinite) ? values : null;
}

function mean(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

app.get("/", (req, res) => {
  res.json({ status: "ok", service: "quick-stats" });
});

app.get("/stats", (req, res) => {
  const values = parseValues(req.query.values);
  if (!values) {
    return res.status(400).json({ error: "values must be numbers separated by commas, like 1,2,3" });
  }
  return res.json({ count: values.length, mean: mean(values), median: median(values) });
});

const port = Number(process.env.PORT) || 8080;
app.listen(port, "0.0.0.0", () => {
  console.log(`quick-stats listening on ${port}`);
});
