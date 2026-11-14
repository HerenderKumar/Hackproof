# Quick stats

Paste a list of numbers, get the mean and median.

## Run it

```sh
npm install
npm start
```

The server listens on `$PORT` (8080 by default).

## API

`GET /stats?values=1,2,3,10` returns `{"count": 4, "mean": 4, "median": 2.5}`. Values are
numbers separated by commas; anything else returns 400.
