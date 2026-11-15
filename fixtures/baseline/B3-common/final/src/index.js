const { createApp, listen, errors } = require("./setup");
const { plan } = require("./planner");

const app = createApp({ origin: "http://localhost:5173" });

app.post("/plan", (req, res) => {
  const { stops, start } = req.body;
  if (!Array.isArray(stops) || stops.length === 0) {
    return res.status(400).json({ error: "send at least one stop" });
  }
  const route = plan(start, stops);
  res.json({ order: route.order, distanceKm: Math.round(route.distance * 10) / 10 });
});

errors(app);
listen(app);
