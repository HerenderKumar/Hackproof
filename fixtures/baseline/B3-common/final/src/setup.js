const express = require("express");
const cors = require("cors");
const morgan = require("morgan");

function createApp(options = {}) {
  const app = express();
  app.disable("x-powered-by");
  app.use(cors({ origin: options.origin || "*" }));
  app.use(express.json({ limit: options.bodyLimit || "1mb" }));
  if (options.log !== false) app.use(morgan("tiny"));
  app.get("/health", (req, res) => res.json({ ok: true, uptime: process.uptime() }));
  return app;
}

function listen(app, port = Number(process.env.PORT) || 3000) {
  return app.listen(port, () => console.log(`listening on ${port}`));
}

function errors(app) {
  app.use((req, res) => res.status(404).json({ error: "not found" }));
  app.use((err, req, res, next) => {
    console.error(err);
    res.status(500).json({ error: "something went wrong" });
  });
}

module.exports = { createApp, listen, errors };
