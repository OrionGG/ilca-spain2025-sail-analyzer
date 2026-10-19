"""The dependency rule of the Clean Architecture refactor (#6), checked statically (ast).

Entities (sail_analyzer/entities): the enterprise business rules. They import only pure
stdlib modules, numpy and each other: no files, network, HTTP, threads or UI, and they
don't reach outwards (use cases, adapters, app).
"""

import ast
import glob
import os
import unittest

from tests import support

ENTITIES = os.path.join(support.PKG, "entities")
PURE_STDLIB = {"__future__", "math", "re", "typing", "dataclasses", "datetime", "functools", "itertools",
               "collections", "enum", "statistics"}
THIRD_PARTY = {"numpy"}
SIDE_EFFECT_CALLS = {"open", "print", "input", "exec", "eval", "__import__", "breakpoint"}


def imports(tree):
    """(line, what) for every import: absolute module names, or '.' + relative ones."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, "." * node.level + (node.module or "")


class EntitiesArePure(unittest.TestCase):
    def files(self):
        files = sorted(glob.glob(os.path.join(ENTITIES, "*.py")))
        self.assertTrue(files, "no entities found")
        return files

    def test_imports_point_inwards_only(self):
        for path in self.files():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for line, what in imports(tree):
                name = os.path.basename(path)
                if what.startswith("."):
                    ok = what.startswith(".") and not what.startswith("..")       # another entity
                else:
                    root = what.split(".")[0]
                    ok = root in PURE_STDLIB | THIRD_PARTY or what.startswith("sail_analyzer.entities")
                self.assertTrue(ok, f"entities/{name}:{line} imports {what!r} (entities stay pure)")

    def test_no_side_effects(self):
        for path in self.files():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in SIDE_EFFECT_CALLS:
                    self.fail(f"entities/{os.path.basename(path)}:{node.lineno} calls {node.func.id}()")

    def test_rule_catches_violations(self):
        """The checks themselves work (a file would fail them)."""
        bad = ast.parse("import os\nfrom ..app import x\nfrom .wind import y\nopen('f')\n")
        found = [w for _l, w in imports(bad)]
        self.assertEqual(found, ["os", "..app", ".wind"])
        self.assertTrue(any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open" for n in ast.walk(bad)))


class PackageEntryPoints(unittest.TestCase):
    def test_importable_as_a_package(self):
        import importlib
        app = importlib.import_module("sail_analyzer.app")
        self.assertTrue(callable(app.main))
        self.assertTrue(os.path.isfile(os.path.join(support.PKG, "__main__.py")))


if __name__ == "__main__":
    unittest.main()
