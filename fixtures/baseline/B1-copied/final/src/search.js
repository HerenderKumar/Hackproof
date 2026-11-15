function tokens(text) {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((word) => word.length > 2);
}

function score(note, query) {
  const wanted = new Set(tokens(query));
  if (wanted.size === 0) return 0;
  let hits = 0;
  for (const word of tokens(`${note.title} ${note.body}`)) {
    if (wanted.has(word)) hits += 1;
  }
  return hits / wanted.size;
}

function search(notes, query, limit = 10) {
  return notes
    .map((note) => ({ note, score: score(note, query) }))
    .filter((hit) => hit.score > 0)
    .sort((a, b) => b.score - a.score || b.note.id - a.note.id)
    .slice(0, limit)
    .map((hit) => hit.note);
}

module.exports = { search, tokens };
