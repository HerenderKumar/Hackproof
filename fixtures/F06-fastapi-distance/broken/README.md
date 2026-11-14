# Distance converter

Kilometres to miles, for runners planning a race abroad.

## Run it

```sh
pip install -r requirements.txt
python main.py
```

The server listens on `$PORT` (8080 by default).

## API

`GET /convert?km=10` returns `{"km": 10.0, "miles": 6.214}`. Miles are rounded to three decimal
places; a negative distance returns 422.
