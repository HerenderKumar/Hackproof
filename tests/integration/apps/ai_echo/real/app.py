"""A model-backed app for the model door's tests: `/` answers only once a model has replied.

It uses the variables HackProof sets for a run with the door on (OPENAI_BASE_URL, OPENAI_API_KEY,
HP_MODELS), as a team's app using the stock SDK would. Standard library only.
"""

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def ask(question: str) -> str:
    model = os.environ["HP_MODELS"].split(",")[0]
    body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": question}]}
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as reply:
        return json.load(reply)["choices"][0]["message"]["content"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            status, body = 200, {"answer": ask("Reply with the single word: ready")}
        except Exception as error:  # the model did not answer: the app is not ready
            status, body = 502, {"error": f"{type(error).__name__}: {error}"}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
