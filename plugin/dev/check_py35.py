"""Static check that the plugin runs on STE Slicer's embedded Python 3.5.

Run with any modern Python (3.8+):
    python check_py35.py [path-to-STE-Slicer-install]

* syntax newer than 3.5: f-strings, variable annotations, walrus, positional-only
  parameters, async generators/comprehensions, underscores in numbers;
* API names added after 3.5 (removeprefix, capture_output, secrets, dataclasses...);
* every imported module must exist in the frozen python35.zip, as a root .pyd,
  as a built-in, or inside the plugin itself.
"""
import ast
import io
import os
import re
import sys
import tokenize
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "AIAssistant")
DEFAULT_INSTALL = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

BUILTINS = {
    "sys", "builtins", "math", "time", "itertools", "_thread", "gc", "zlib", "binascii", "array", "marshal",
    "errno", "nt", "_io", "_codecs", "_collections", "_functools", "_operator", "_sre", "_struct", "_weakref",
    "_warnings", "_imp", "atexit", "cmath", "_datetime", "_json", "_pickle", "_random", "_sha1", "_sha256",
    "_sha512", "_md5", "_bisect", "_heapq", "_locale", "_csv", "mmap", "msvcrt", "_signal", "winreg", "_winapi",
    "zipimport", "_string", "_stat", "_opcode", "faulthandler", "_ast", "_multibytecodec", "audioop",
}

NEWER_API = [
    (r"\.removeprefix\(|\.removesuffix\(", "str.removeprefix/removesuffix (3.9)"),
    (r"capture_output\s*=", "subprocess capture_output (3.7)"),
    (r"\bfromisoformat\(", "datetime.fromisoformat (3.7)"),
    (r"\bimport secrets\b|\bfrom secrets\b", "secrets module (3.6)"),
    (r"\bdataclasses\b", "dataclasses (3.7)"),
    (r"\bnullcontext\b", "contextlib.nullcontext (3.7)"),
    (r"asyncio\.run\(", "asyncio.run (3.7)"),
    (r"\btime_ns\(", "time.time_ns (3.7)"),
    (r"\.isascii\(", "str.isascii (3.7)"),
    (r"math\.(prod|isqrt|dist|comb|perm)\(", "math function (3.8)"),
    (r"\bcached_property\b", "functools.cached_property (3.8)"),
    (r"os\.fspath\(", "os.fspath (3.6)"),
    (r"random\.choices\(", "random.choices (3.6)"),
    (r"\bThreadingHTTPServer\b", "http.server.ThreadingHTTPServer (3.7)"),
    (r"\btext\s*=\s*True\b", "subprocess text=True (3.7)"),
    (r"typing\.(Literal|Protocol|TypedDict|Final)\b", "typing feature (3.8)"),
    (r"\bimportlib\.resources\b", "importlib.resources (3.7)"),
    (r"\.is_relative_to\(", "Path.is_relative_to (3.9)"),
]


def available_modules(install):
    names = set()
    with zipfile.ZipFile(os.path.join(install, "python35.zip")) as archive:
        for entry in archive.namelist():
            if entry.endswith(".pyc"):
                module = entry[:-4].replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[:-9]
                names.add(module)
    for entry in os.listdir(install):
        if entry.endswith(".pyd"):
            names.add(entry[:-4].split(".cp35")[0])
        elif os.path.isdir(os.path.join(install, entry)) and any(
                os.path.isfile(os.path.join(install, entry, "__init__" + ext)) for ext in (".py", ".pyc")):
            names.add(entry)
    return names


def module_exists(name, modules):
    if name in modules or name.split(".")[0] in BUILTINS:
        return True
    return any(m == name or m.startswith(name + ".") for m in modules)


class Checker(ast.NodeVisitor):
    def __init__(self, path, modules):
        self.path = path
        self.modules = modules
        self.problems = []
        self._function_stack = []

    def report(self, node, message):
        self.problems.append("%s:%d: %s" % (os.path.relpath(self.path, PLUGIN), getattr(node, "lineno", 0), message))

    def visit_JoinedStr(self, node):
        self.report(node, "f-string (Python 3.6+)")

    def visit_AnnAssign(self, node):
        self.report(node, "variable annotation (Python 3.6+)")
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        self.report(node, "walrus operator (Python 3.8+)")
        self.generic_visit(node)

    def _function(self, node):
        if getattr(node.args, "posonlyargs", None):
            self.report(node, "positional-only parameters (Python 3.8+)")
        self._function_stack.append(node)
        self.generic_visit(node)
        self._function_stack.pop()

    visit_FunctionDef = _function

    def visit_AsyncFunctionDef(self, node):
        for child in ast.walk(node):
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                self.report(node, "async generator (Python 3.6+)")
                break
        self._function(node)

    def visit_comprehension(self, node):
        if getattr(node, "is_async", 0):
            self.report(node.target, "async comprehension (Python 3.6+)")
        self.generic_visit(node)

    def _optional(self, node):
        # Imports guarded by try/except and marked "# optional" may legitimately be missing.
        return "# optional" in self.lines[node.lineno - 1]

    def visit_Import(self, node):
        for alias in node.names:
            if not module_exists(alias.name, self.modules) and not self._optional(node):
                self.report(node, "module '%s' is not available in STE Slicer's Python" % alias.name)

    def visit_ImportFrom(self, node):
        if node.level:
            return  # relative import inside the plugin
        if node.module and not module_exists(node.module, self.modules) and not self._optional(node):
            self.report(node, "module '%s' is not available in STE Slicer's Python" % node.module)


def check_file(path, modules):
    with open(path, "r", encoding="utf-8") as stream:
        source = stream.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return ["%s:%s: syntax error: %s" % (os.path.relpath(path, PLUGIN), e.lineno, e.msg)]
    checker = Checker(path, modules)
    checker.lines = source.splitlines()
    checker.visit(tree)
    problems = checker.problems
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NUMBER and "_" in token.string:
            problems.append("%s:%d: underscore in number literal (Python 3.6+)" % (os.path.relpath(path, PLUGIN), token.start[0]))
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.split("#", 1)[0]
        for pattern, label in NEWER_API:
            if re.search(pattern, stripped):
                problems.append("%s:%d: %s" % (os.path.relpath(path, PLUGIN), number, label))
    return problems


def main():
    install = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INSTALL
    if not os.path.isfile(os.path.join(install, "python35.zip")):
        print("python35.zip not found in %s — pass the STE Slicer install folder" % install)
        return 2
    modules = available_modules(install)
    files = []
    for root, dirs, names in os.walk(PLUGIN):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        files.extend(os.path.join(root, n) for n in names if n.endswith(".py"))
    problems = []
    for path in sorted(files):
        problems.extend(check_file(path, modules))
    for problem in problems:
        print(problem)
    print("%d files checked, %d problem(s)" % (len(files), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
