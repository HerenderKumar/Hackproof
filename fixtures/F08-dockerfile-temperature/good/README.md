# Celsius to Fahrenheit

A weather widget's backend. No dependencies: it uses only Python's standard library, and ships
with its own Dockerfile.

## Run it

```sh
docker build -t c2f .
docker run -p 8080:8080 c2f
```

Or without Docker: `python -m app.server`. The server listens on `$PORT` (8080 by default).

## API

`GET /c2f?c=100` returns `{"c": 100.0, "f": 212.0}`. Fahrenheit is rounded to two decimal
places; a `c` that is not a number returns 400.
