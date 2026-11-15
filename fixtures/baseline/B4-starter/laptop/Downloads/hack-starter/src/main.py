"""The event's starter: a FastAPI app with a health check and one example route."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Hackathon starter")


class Echo(BaseModel):
    message: str
    times: int = 1


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/echo")
def echo(body: Echo) -> dict:
    if body.times < 1 or body.times > 10:
        raise HTTPException(status_code=422, detail="times must be between 1 and 10")
    return {"echo": [body.message] * body.times}
