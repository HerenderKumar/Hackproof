"""Convert kilometres to miles."""

import os
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Query

MILES_PER_KM = 0.621371

app = FastAPI(title="Distance converter")


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/convert")
def convert(km: Annotated[float, Query(ge=0)]) -> dict[str, float]:
    return {"km": km, "miles": round(km * MILES_PER_KM, 3)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
