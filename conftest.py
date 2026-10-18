import sys
from pathlib import Path

# make the repo-root package importable no matter how pytest is invoked
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
