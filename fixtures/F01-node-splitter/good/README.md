# Bill splitter

Split a restaurant bill between friends. Every share is in rupees and paisa, and the shares
always add up to the bill.

## Run it

```sh
npm install
npm start
```

The server listens on `$PORT` (8080 by default).

## API

`POST /split` with `{"total": 100, "people": 3}` returns:

```json
{"total": 100, "people": 3, "shares": [33.34, 33.33, 33.33]}
```

`total` is in rupees and must be positive; `people` is a whole number from 1 to 1000.
