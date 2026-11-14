"""Tell whether a short review is positive or negative from the words it uses."""

import os
import re

from flask import Flask, jsonify, request

app = Flask(__name__)

POSITIVE = {"good", "great", "love", "excellent", "happy", "nice", "amazing", "tasty"}
NEGATIVE = {"bad", "terrible", "hate", "awful", "sad", "poor", "horrible", "bland"}


def classify(text):
    words = re.findall(r"[A-Z']+", text.lower())
    positive = sum(word in POSITIVE for word in words)
    negative = sum(word in NEGATIVE for word in words)
    return "negative" if negative > positive else "positive"


@app.get("/")
def health():
    return jsonify(status="ok")


@app.post("/classify")
def classify_text():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify(error="text must be a non-empty string"), 400
    return jsonify(text=text, label=classify(text))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
