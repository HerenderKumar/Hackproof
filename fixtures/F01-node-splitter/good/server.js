const express = require("express");

const app = express();
app.use(express.json());

// Split an amount in rupees between people. Work in whole paisa so the shares always add
// back up to the total: the paisa that do not divide evenly go to the first few people.
function splitTotal(total, people) {
  const paisa = Math.round(total * 100);
  const base = Math.floor(paisa / people);
  const remainder = paisa - base * people;
  const shares = [];
  for (let i = 0; i < people; i += 1) {
    shares.push((base + (i < remainder ? 1 : 0)) / 100);
  }
  return shares;
}

app.get("/", (req, res) => {
  res.json({ status: "ok", service: "bill-splitter" });
});

app.post("/split", (req, res) => {
  const { total, people } = req.body || {};
  if (typeof total !== "number" || !Number.isFinite(total) || total <= 0) {
    return res.status(400).json({ error: "total must be a positive number of rupees" });
  }
  if (!Number.isInteger(people) || people < 1 || people > 1000) {
    return res.status(400).json({ error: "people must be a whole number from 1 to 1000" });
  }
  return res.json({ total, people, shares: splitTotal(total, people) });
});

const port = Number(process.env.PORT) || 8080;
app.listen(port, "0.0.0.0", () => {
  console.log(`bill-splitter listening on ${port}`);
});
