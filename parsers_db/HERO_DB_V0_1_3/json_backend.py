from __future__ import annotations
from pathlib import Path
try:
    import orjson
except ImportError as exc:
    raise RuntimeError("Builder requires 'orjson'. Install it with: pip install orjson") from exc

_PRETTY = orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE
_COMPACT = orjson.OPT_SORT_KEYS

def loads(data):
    return orjson.loads(data)

def dumps(data, *, pretty=True):
    return orjson.dumps(data, option=_PRETTY if pretty else _COMPACT)

def load(path):
    return loads(Path(path).read_bytes())

def save(path, data, *, pretty=True):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(dumps(data, pretty=pretty))
