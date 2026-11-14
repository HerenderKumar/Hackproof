# Pantry

Keep track of what is in the kitchen.

## Run it

```sh
pip install -r requirements.txt
python main.py
```

The server listens on `$PORT` (8080 by default).

## API

- `POST /items` with `{"name": "rice", "quantity": 2}` adds an item and returns it with its
  `id` (status 201).
- `GET /items/{id}` returns the item, or 404 if there is no such item.
