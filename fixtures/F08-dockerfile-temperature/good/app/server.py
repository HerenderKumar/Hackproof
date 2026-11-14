"""Celsius to Fahrenheit over HTTP, with only the standard library."""

import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def c_to_f(celsius):
    return round(celsius * 9 / 5 + 32, 2)


def parse_celsius(query):
    values = parse_qs(query).get("c")
    if not values:
        return None
    try:
        celsius = float(values[0])
    except ValueError:
        return None
    return celsius if math.isfinite(celsius) else None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            self.send_json(200, {"status": "ok"})
        elif url.path == "/c2f":
            celsius = parse_celsius(url.query)
            if celsius is None:
                self.send_json(400, {"error": "c must be a number, like /c2f?c=100"})
            else:
                self.send_json(200, {"c": celsius, "f": c_to_f(celsius)})
        else:
            self.send_json(404, {"error": "not found"})

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"c2f listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
