from __future__ import annotations
from json_backend import load as _orjson_load, save as _orjson_save
from pathlib import Path
from typing import Any

def load(path):
    return _orjson_load(path)

def save(path, data):
    return _orjson_save(path, data)

def ability_records(herodata):
    for hero, h in herodata.get("heroes",{}).items():
        for a in h.get("abilities",[]):
            yield hero, a

def index_by_record_key(data):
    out={}
    for hero, rows in data.get("heroes",{}).items():
        out[hero]={}
        for row in rows:
            rk=row.get("record_key")
            if rk: out[hero][rk]=row
    return out

def source_fact(kind, value=None, source=None, confidence=None, **extra):
    x={"kind":kind}
    if value is not None:x["value"]=value
    if source is not None:x["source"]=source
    if confidence is not None:x["confidence"]=confidence
    x.update(extra)
    return x
