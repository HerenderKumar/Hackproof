function normaliseTag(raw) {
  const tag = String(raw).trim().toLowerCase().replace(/\s+/g, "-");
  if (!/^[a-z0-9-]{1,30}$/.test(tag)) {
    throw new Error(`"${raw}" is not a tag: letters, digits and dashes, at most 30`);
  }
  return tag;
}

function tagCounts(notes) {
  const counts = {};
  for (const note of notes) {
    for (const tag of new Set(note.tags.map(normaliseTag))) {
      counts[tag] = (counts[tag] || 0) + 1;
    }
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

module.exports = { normaliseTag, tagCounts };
