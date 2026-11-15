"""The kickoff scanner, loaded from its file: it is a script for participants, not a package."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PATH = Path(__file__).resolve().parent.parent / "scanner" / "hackproof_scan.py"


def load() -> ModuleType:
    name = "hackproof_scan"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the file runs
    spec.loader.exec_module(module)
    return module
