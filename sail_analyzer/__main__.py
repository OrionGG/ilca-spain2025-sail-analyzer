"""python -m sail_analyzer [--port N] [--rebuild]   (from the repo root)
   python sail_analyzer [--port N] [--rebuild]      (the folder)"""

import os
import sys

if __package__ in (None, "") and "sail_analyzer" not in sys.modules:
    # the folder run as a script: load this checkout's package by its path (as app.py does)
    import importlib.util
    _pkg = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location("sail_analyzer", os.path.join(_pkg, "__init__.py"),
                                                   submodule_search_locations=[_pkg])
    sys.modules["sail_analyzer"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["sail_analyzer"])

from sail_analyzer.app import main  # noqa: E402

if __name__ == "__main__":
    main()
