import sys
from pathlib import Path

# Make the top-level modules (settings, storage, stats) importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
