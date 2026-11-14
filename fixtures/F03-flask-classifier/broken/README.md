# Review mood

Paste a restaurant review and find out whether it is positive or negative.

## Run it

```sh
pip install -r requirements.txt
python main.py
```

The server listens on `$PORT` (8080 by default).

## API

`POST /classify` with `{"text": "The food was terrible"}` returns
`{"text": "The food was terrible", "label": "negative"}`.

A review is negative when it uses more negative words (bad, terrible, hate, awful, sad, poor,
horrible, bland) than positive ones (good, great, love, excellent, happy, nice, amazing,
tasty), and positive otherwise.
