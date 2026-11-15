const express = require("express");
const { NoteStore } = require("./store");

const app = express();
const notes = new NoteStore();
app.use(express.json());

app.get("/notes", (req, res) => {
  const tag = req.query.tag;
  const found = tag ? notes.withTag(tag) : notes.all();
  res.json({ notes: found, count: found.length });
});

app.post("/notes", (req, res) => {
  const { title, body, tags } = req.body;
  if (!title || title.length > 120) {
    return res.status(400).json({ error: "a title of 1 to 120 characters is needed" });
  }
  const note = notes.add(title, body || "", Array.isArray(tags) ? tags : []);
  res.status(201).json(note);
});

app.delete("/notes/:id", (req, res) => {
  const removed = notes.remove(Number(req.params.id));
  res.status(removed ? 204 : 404).end();
});

app.listen(process.env.PORT || 3000);
