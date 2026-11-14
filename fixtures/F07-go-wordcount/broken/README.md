# Word counter

How long is your essay? Send the text and get the word count back.

## Run it

```sh
go run .
```

The server listens on `$PORT` (8080 by default).

## API

`POST /wordcount` with `{"text": "the quick brown fox"}` returns `{"words": 4}`. Words are
separated by any amount of whitespace, so an empty text has 0 words.
