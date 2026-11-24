"""Shared test helpers."""
import json
import os

from app.models import Network

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load(name: str) -> Network:
    with open(os.path.join(DATA_DIR, name), "r", encoding="utf-8") as fh:
        return Network.from_dict(json.load(fh))
