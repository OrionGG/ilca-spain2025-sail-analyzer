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
               "collections", "enum", "statistics", "contextlib"}
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


def injected(tree):
    """The gateways a use case is given: attributes an __init__ sets straight from its own
    parameters (self.repo = repo, or self.a, self.b = a, b)."""
    out = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "__init__"):
            continue
        params = {a.arg for a in fn.args.args[1:] + fn.args.kwonlyargs}
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                pairs = (zip(t.elts, node.value.elts) if isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple)
                         else [(t, node.value)])
                out |= {a.attr for a, v in pairs
                        if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "self"
                        and isinstance(v, ast.Name) and v.id in params}
    return out


def is_port_call(f, ports):
    """self.<gateway>.<method>(...) on an injected gateway: a use case reaching the outside world
    through its own interfaces, which is how I/O is meant to come in."""
    return isinstance(f.value, ast.Attribute) and f.value.attr in ports \
        and isinstance(f.value.value, ast.Name) and f.value.value.id == "self"


def side_effects(tree, ports=frozenset()):
    """(line, call) for calls that do I/O or read the clock, and I/O names imported to call bare.
    ports: injected gateways (see injected), whose calls are allowed."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in SIDE_EFFECT_ATTRS:
                    yield node.lineno, "import " + a.name
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in SIDE_EFFECT_CALLS:
                yield node.lineno, f.id + "()"
            elif isinstance(f, ast.Attribute) and f.attr in SIDE_EFFECT_ATTRS and not is_port_call(f, ports):
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
            tree = parse(path)
            found = list(side_effects(tree, injected(tree)))
            self.assertFalse(found, f"use_cases/{os.path.basename(path)} does I/O: {found}")

    def test_the_rule_itself(self):
        for ok in (".errors", ".gateways", "sail_analyzer.entities.track", "sail_analyzer.use_cases.errors",
                   "numpy", "typing", "dataclasses", "datetime"):
            self.assertTrue(use_case_may_import(ok), ok)
        for bad in ("sail_analyzer.app", "sail_analyzer.regatta", "sail_analyzer.results", "..app",
                    "sail_analyzer.interface_adapters.presenters", "sail_analyzer.use_cases_io",
                    "os", "json", "http.server", "urllib.request", "threading", "xml.etree.ElementTree"):
            self.assertFalse(use_case_may_import(bad), bad)
        code = ast.parse(
            "import datetime\n"
            "class Uc:\n"
            "    def __init__(self, repo, store, clock, n=1):\n"
            "        self.repo = repo\n"
            "        self.store, self.clock, self.n = store, clock, n\n"
            "        self.dt, self.np = datetime, make()\n"
            "    def run(self):\n"
            "        self.repo.load(); self.store.save(1); self.clock.now()\n"     # injected gateways
            "        self.dt.now(); self.np.load('x'); self.load(); repo.load(); self.repo.x.save()\n")
        self.assertEqual(injected(code), {"repo", "store", "clock", "n"})
        self.assertEqual(sorted(c for _l, c in side_effects(code, injected(code))),
                         sorted([".now()", ".load()", ".load()", ".load()", ".save()"]))


class CacheStaleness(unittest.TestCase):
    """Cached races are rebuilt when the code they come from changes: that has to cover every
    circle the analysis code lives in, wherever later steps move it."""

    def test_sources_cover_every_circle(self):
        from sail_analyzer import app as A
        want = {os.path.normcase(f) for f in glob.glob(os.path.join(glob.escape(support.PKG), "**", "*.py"),
                                                        recursive=True)
                if not os.path.basename(f).startswith(".")}
        got = {os.path.normcase(f) for f in A.ANALYSIS_SOURCES}
        self.assertEqual(sorted(want - got), [], "package code not counted for cache staleness")
        for d in ("entities", "use_cases"):
            self.assertTrue(any(os.sep + d + os.sep in f for f in got), d)

    def test_walk_skips_only_the_package_data_folders(self):
        import shutil
        import tempfile
        from sail_analyzer import app as A
        root = tempfile.mkdtemp(prefix="sa_src_")
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("a.py", "x/data/b.py", "x/static/c.py", "x/cache/d.py", "data/e.py", "cache/f.py",
                    "static/g.py", ".#lock.py", "x/__pycache__/h.py", "x/notes.txt"):
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            open(os.path.join(root, rel), "w").close()
        skip = [os.path.join(root, d) for d in ("cache", "data", "static")]
        found = sorted(os.path.relpath(f, root).replace(os.sep, "/") for f in A._package_sources(root, skip))
        self.assertEqual(found, ["a.py", "x/cache/d.py", "x/data/b.py", "x/static/c.py"])
        self.assertEqual(A._newest([os.path.join(root, "a.py"), os.path.join(root, "gone.py")]),
                         os.path.getmtime(os.path.join(root, "a.py")))


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
