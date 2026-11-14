"""Convert kilometres to miles."""

import json
import os
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Query

# Conversion rates live in rates.json so more units can be added without code changes.
RATES = json.loads((Path(__file__).parent / "rates.json").read_text())
MILES_PER_KM = RATES["miles_per_km"]

app = FastAPI(title="Distance converter")


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/convert")
def convert(km: Annotated[float, Query(ge=0)]) -> dict[str, float]:
    return {"km": km, "miles": round(km * MILES_PER_KM, 3)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
