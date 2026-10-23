import os
from pathlib import Path
import pytest

@pytest.fixture
def replay_path():
    candidates = []
    if os.environ.get("PYHOTS_EXTREME_REPLAY"):
        candidates.append(Path(os.environ["PYHOTS_EXTREME_REPLAY"]))
    candidates.append(Path('/mnt/data/2023-11-12_21.11.07_Towers_of_Doom(1).StormReplay'))
    for p in candidates:
        if p.exists():
            return p
    pytest.skip("Extreme replay fixture not supplied; set PYHOTS_EXTREME_REPLAY")
