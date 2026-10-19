"""The dependency rule of the Clean Architecture refactor (#6), checked statically (ast).

Entities (sail_analyzer/entities): the enterprise business rules. They import only pure
stdlib modules, numpy and each other: no files, network, HTTP, clock, threads or UI, and
they don't reach outwards (use cases, adapters, app).
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
# I/O and clock reached through an allowed module: np.load(...), arr.tofile(...), datetime.now()
SIDE_EFFECT_ATTRS = {"load", "loadtxt", "genfromtxt", "fromfile", "memmap", "save", "savez", "savez_compressed",
                     "savetxt", "tofile", "now", "utcnow", "today"}


def imports(tree):
    """(line, what) for every import: absolute module names, or '.' + relative ones."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, "." * node.level + (node.module or "")


def entity_may_import(what):
    """Another entity (relative, same package) or a pure stdlib module / numpy."""
    if what.startswith("."):
        return not what.startswith("..")
    return what.split(".")[0] in PURE_STDLIB | THIRD_PARTY or what.startswith("sail_analyzer.entities")


def side_effects(tree):
    """(line, call) for calls that do I/O or read the clock."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in SIDE_EFFECT_CALLS:
                yield node.lineno, f.id + "()"
            elif isinstance(f, ast.Attribute) and f.attr in SIDE_EFFECT_ATTRS:
                yield node.lineno, "." + f.attr + "()"


def parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), path)


class EntitiesArePure(unittest.TestCase):
    def files(self):
        files = sorted(glob.glob(os.path.join(ENTITIES, "*.py")))
        self.assertTrue(files, "no entities found")
        return files

    def test_imports_point_inwards_only(self):
        for path in self.files():
            for line, what in imports(parse(path)):
                self.assertTrue(entity_may_import(what),
                                f"entities/{os.path.basename(path)}:{line} imports {what!r} (entities stay pure)")

    def test_no_side_effects(self):
        for path in self.files():
            found = list(side_effects(parse(path)))
            self.assertFalse(found, f"entities/{os.path.basename(path)} does I/O: {found}")

    def test_the_rule_itself(self):
        """The checks reject what they should and accept what they should."""
        for ok in (".wind", ".kinematics", "math", "numpy", "numpy.linalg", "dataclasses",
                   "sail_analyzer.entities.track"):
            self.assertTrue(entity_may_import(ok), ok)
        for bad in ("os", "io", "json", "urllib.request", "http.server", "threading", "xml.etree.ElementTree",
                    "glob", "..app", "..regatta", "sail_analyzer.app", "sail_analyzer", "time"):
            self.assertFalse(entity_may_import(bad), bad)
        bad = ast.parse("open('f')\nprint(1)\nnp.load('x')\na.tofile('y')\ndatetime.now()\nnp.mean(a)\n")
        self.assertEqual([c for _l, c in side_effects(bad)],
                         ["open()", "print()", ".load()", ".tofile()", ".now()"])


class PackageEntryPoints(unittest.TestCase):
    def test_importable_as_a_package(self):
        import importlib
        app = importlib.import_module("sail_analyzer.app")
        self.assertTrue(callable(app.main))

    def test_module_entry_point_runs_only_as_main(self):
        tree = parse(os.path.join(support.PKG, "__main__.py"))
        top_calls = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        self.assertFalse(top_calls, "__main__.py must not start the app when imported")
        guards = [n for n in tree.body if isinstance(n, ast.If) and "__name__" in ast.dump(n.test)]
        self.assertTrue(guards, "__main__.py: main() under if __name__ == '__main__'")


if __name__ == "__main__":
    unittest.main()
