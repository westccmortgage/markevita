"""Make the bundled production engine available before any Studio imports.

The service starts in studio/, while its engine lives in ../pipeline/serial.
Keep this bootstrap at package entry so direct submodule imports and Uvicorn
use the same path, without depending on runner or pytest import order.
"""
from pathlib import Path
import sys

PIPELINE_DIR = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))
