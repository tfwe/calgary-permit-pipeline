import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # repository root: pipeline, config, tools
sys.path.insert(0, str(HERE))         # tests dir: _harness, _env
import _env  # noqa: E402,F401  (must run before config is imported)
