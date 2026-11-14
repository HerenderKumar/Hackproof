"""A tiny pantry API: add an item, then read it back."""

import os
from itertools import count
from typing import Annotated

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Pantry")


class NewItem(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    quantity: Annotated[int, Field(ge=0)] = 1


class Item(NewItem):
    id: int


ITEMS: dict[int, Item] = {}
NEXT_ID = count(1)


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/items", status_code=201)
def create_item(new: NewItem) -> Item:
    item = Item(id=next(NEXT_ID), **new.model_dump())
    ITEMS[item.id] = item
    return item


@app.get("/items/{item_id}")
def read_item(item_id: int) -> Item:
    item = ITEMS.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
