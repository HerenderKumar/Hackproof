"""Celsius to Fahrenheit."""

import math
import os

from flask import Flask, jsonify, request

app = Flask(__name__)


def c_to_f(celsius):
    return round(celsius * 9 / 5 + 32, 2)


@app.get("/")
def health():
    return jsonify(status="ok")


@app.get("/convert")
def convert():
    try:
        celsius = float(request.args.get("c", ""))
    except ValueError:
        celsius = math.nan
    if not math.isfinite(celsius):
        return jsonify(error="c must be a number, like /convert?c=100"), 400
    return jsonify(c=celsius, f=c_to_f(celsius))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
