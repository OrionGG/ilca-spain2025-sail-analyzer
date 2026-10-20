"""The dependency rule of the Clean Architecture refactor (#6), checked statically (ast).

Entities (sail_analyzer/entities): the enterprise business rules. They import only pure
stdlib modules, numpy and each other: no files, network, HTTP, clock, threads or UI, and
they don't reach outwards (use cases, adapters, app).

Use cases (sail_analyzer/use_cases): the application business rules. They import only the
entities, each other, pure stdlib modules and numpy, and do no I/O: the outside world comes
in through the gateway interfaces they declare.
"""

import ast
import glob
import os
import unittest

from tests import support

ENTITIES = os.path.join(support.PKG, "entities")
USE_CASES = os.path.join(support.PKG, "use_cases")
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
    return what.split(".")[0] in PURE_STDLIB | THIRD_PARTY or what == "sail_analyzer.entities" \
        or what.startswith("sail_analyzer.entities.")


def use_case_may_import(what):
    """Another use-case module (relative, same package), an entity, or a pure stdlib module / numpy."""
    if what.startswith("."):
        return not what.startswith("..")
    return entity_may_import(what) or what == "sail_analyzer.use_cases" \
        or what.startswith("sail_analyzer.use_cases.")


def layer_files(test, root, name):
    files = sorted(glob.glob(os.path.join(glob.escape(root), "**", "*.py"), recursive=True))
    test.assertTrue(files, f"no {name} found")
    return files


def side_effects(tree):
    """(line, call) for calls that do I/O or read the clock, and I/O names imported to call bare."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in SIDE_EFFECT_ATTRS:
                    yield node.lineno, "import " + a.name
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
        files = sorted(glob.glob(os.path.join(glob.escape(ENTITIES), "**", "*.py"), recursive=True))
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
                    "glob", "..app", "..regatta", "sail_analyzer.app", "sail_analyzer", "time",
                    "sail_analyzer.entities_io"):
            self.assertFalse(entity_may_import(bad), bad)
        bad = ast.parse("from numpy import load, mean\nopen('f')\nprint(1)\nnp.load('x')\na.tofile('y')\n"
                        "datetime.now()\nnp.mean(a)\n")
        self.assertEqual(sorted(c for _l, c in side_effects(bad)),
                         sorted(["import load", "open()", "print()", ".load()", ".tofile()", ".now()"]))


class UseCasesDependOnEntitiesOnly(unittest.TestCase):
    def files(self):
        return layer_files(self, USE_CASES, "use cases")

    def test_imports_point_inwards_only(self):
        for path in self.files():
            for line, what in imports(parse(path)):
                self.assertTrue(use_case_may_import(what),
                                f"use_cases/{os.path.basename(path)}:{line} imports {what!r} "
                                "(use cases depend only on entities and their own interfaces)")

    def test_no_side_effects(self):
        for path in self.files():
            found = list(side_effects(parse(path)))
            self.assertFalse(found, f"use_cases/{os.path.basename(path)} does I/O: {found}")

    def test_the_rule_itself(self):
        for ok in (".errors", ".gateways", "sail_analyzer.entities.track", "sail_analyzer.use_cases.errors",
                   "numpy", "typing", "dataclasses", "datetime"):
            self.assertTrue(use_case_may_import(ok), ok)
        for bad in ("sail_analyzer.app", "sail_analyzer.regatta", "sail_analyzer.results", "..app",
                    "sail_analyzer.interface_adapters.presenters", "sail_analyzer.use_cases_io",
                    "os", "json", "http.server", "urllib.request", "threading", "xml.etree.ElementTree"):
            self.assertFalse(use_case_may_import(bad), bad)


class PackageEntryPoints(unittest.TestCase):
    def test_importable_as_a_package(self):
        import importlib
        app = importlib.import_module("sail_analyzer.app")
        self.assertTrue(callable(app.main))

    def test_script_mode_like_the_launcher(self):
        """The .bat runs `python app.py` inside sail_analyzer/ (and launch.json `python sail_analyzer/app.py`):
        app.py must make the package importable itself. Loaded in a fresh interpreter, without
        starting the server (not run as __main__)."""
        self.run_fresh(support.PKG, "g = runpy.run_path('app.py', run_name='launcher_check')\n"
                                    "assert callable(g['main'])\n")

    def test_folder_mode(self):
        """`python sail_analyzer` (the folder) runs __main__.py, which loads the package the same way.
        Run from a folder that doesn't contain the package, so only __main__.py's loader can find it."""
        import tempfile
        with tempfile.TemporaryDirectory() as elsewhere:
            self.run_fresh(elsewhere, f"runpy.run_path({support.PKG!r}, run_name='folder_check')\n"
                                      "assert 'sail_analyzer.app' in sys.modules\n")

    def run_fresh(self, cwd, body):
        """Run `body` in a fresh interpreter from `cwd` (no PYTHONPATH), where the package is not
        importable by itself: it must come from this checkout's folder, with sys.path unchanged."""
        import subprocess
        import sys
        code = ("import os, runpy, sys\n"
                "assert not any(os.path.isdir(os.path.join(p or os.getcwd(), 'sail_analyzer')) for p in sys.path), "
                "'the package is importable without the loader: the check would prove nothing'\n"
                "before = list(sys.path)\n"
                + body +
                "import sail_analyzer\n"
                "assert 'sail_analyzer.entities.course' in sys.modules\n"
                f"assert [os.path.normcase(p) for p in sail_analyzer.__path__] == [os.path.normcase({support.PKG!r})], "
                "sail_analyzer.__path__\n"
                "assert sys.path == before, ('sys.path changed', before, sys.path)\n"
                "print('ok')\n")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr[-2000:])

    def test_module_entry_point_runs_only_as_main(self):
        tree = parse(os.path.join(support.PKG, "__main__.py"))
        top_calls = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
        self.assertFalse(top_calls, "__main__.py must not start the app when imported")
        guards = [n for n in tree.body if isinstance(n, ast.If) and "__name__" in ast.dump(n.test)]
        self.assertTrue(guards, "__main__.py: main() under if __name__ == '__main__'")


if __name__ == "__main__":
    unittest.main()
