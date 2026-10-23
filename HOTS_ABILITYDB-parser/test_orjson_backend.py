from pathlib import Path
import tempfile
from json_backend import dumps, save, load
def run():
    x={"юникод":"✓","b":2,"a":[1,{"z":3}]}
    b1=dumps(x); b2=dumps(x)
    assert isinstance(b1,bytes) and b1==b2 and b'"a"' in b1
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/"x.json"; save(p,x); assert load(p)==x; assert p.read_bytes().endswith(b"\n")
    return True
if __name__=="__main__": print("PASS" if run() else "FAIL")
