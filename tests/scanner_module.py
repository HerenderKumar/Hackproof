"""The kickoff scanner, loaded from its file: it is a script for participants, not a package."""

from scripts.baselines import SCANNER_PATH as PATH
from scripts.baselines import load_scanner as load

__all__ = ["PATH", "load"]
