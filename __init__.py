"""IcyLoom plugin wrapper — entrypoint for Hermes plugin system.

This wrapper sits at the root of the plugin directory (beside plugin.yaml)
and re-exports the actual IcyLoom package from src/icyloom.
"""

import sys
from pathlib import Path

# Add src to path so we can import icyloom
_src = Path(__file__).parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from icyloom import register as _register, __version__, PROJECT_NAME

__all__ = ["register", "__version__", "PROJECT_NAME"]

__version__ = __version__
PROJECT_NAME = PROJECT_NAME


def register(ctx) -> None:
    """Entry point for Hermes plugin system."""
    return _register(ctx)