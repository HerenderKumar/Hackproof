class NoteStore {
  constructor() {
    this.notes = new Map();
    this.nextId = 1;
  }

  add(title, body, tags) {
    const note = { id: this.nextId++, title, body, tags, createdAt: new Date().toISOString() };
    this.notes.set(note.id, note);
    return note;
  }

  all() {
    return [...this.notes.values()].sort((a, b) => b.id - a.id);
  }

  withTag(tag) {
    return this.all().filter((note) => note.tags.includes(tag));
  }

  remove(id) {
    return this.notes.delete(id);
  }
}

module.exports = { NoteStore };
