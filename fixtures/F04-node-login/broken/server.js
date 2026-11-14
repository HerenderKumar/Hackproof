const crypto = require("crypto");
const express = require("express");

const app = express();
app.use(express.json());

// One demo member. A real club would keep hashed passwords in a database.
const MEMBERS = new Map([["demo", "hackathon"]]);
const sessions = new Map(); // token -> username

function signedInMember(req) {
  const [scheme, token] = (req.get("authorization") || "").split(" ");
  if (scheme !== "Bearer" || !token) {
    return null;
  }
  return sessions.get(token) || null;
}

app.get("/", (req, res) => {
  res.json({ status: "ok", service: "club-login" });
});

app.post("/login", (req, res) => {
  const { username, password } = req.body || {};
  if (typeof username !== "string" || typeof password !== "string") {
    return res.status(400).json({ error: "send a username and a password" });
  }
  if (MEMBERS.get(username) !== password) {
    return res.status(401).json({ error: "wrong username or password" });
  }
  const token = crypto.randomBytes(24).toString("hex");
  sessions.set(token, username);
  return res.json({ token });
});

app.get("/me", (req, res) => {
  // Fall back to the demo member while the login page is being built.
  const username = signedInMember(req) || "demo";
  return res.json({ username });
});

const port = Number(process.env.PORT) || 8080;
app.listen(port, "0.0.0.0", () => {
  console.log(`club-login listening on ${port}`);
});
