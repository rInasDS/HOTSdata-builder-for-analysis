import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
import integrity_gate as g

def test_version():
    assert g.VERSION == "0.2"

def test_gate_helper():
    assert g.gate(True)["status"] == "PASS"
    assert g.gate(False)["status"] == "FAIL"
