"""Which handlers of a tool package advance the simulator from inside their own call.

WHY THIS EXISTS. The bridge gives a handler the main loop to itself when the handler carries the
exclusive mark (`__omni_kit_mcp_exclusive__`), because a handler that calls `app.update()` under a
doorway task destroys every request started beside it. The mark is typed by the tool package, and
a typed roster drifts: it carried two verbs that never pump and missed four that do. This module
DERIVES the set from the package's syntax tree, so a package's test can hold its roster to it.

WHAT IT ANSWERS, per handler, one of three:

    PUMPS      a path of calls from the handler reaches a synchronous frame advance
    OPAQUE     the handler reaches code the tree cannot read (named below)
    ORDINARY   neither — and every call it reaches was resolved

The contract a package's test enforces is `marked == PUMPS ∪ OPAQUE`. OPAQUE is exclusive on
purpose: "no pump found" is only ever said of code that was read to the end, so a body the tree
cannot follow can never be classed ordinary by omission.

WHAT IS READ. Function calls by name; `module.function` through every import form, including an
import inside a function body; `self.method`; a class called (all its methods); a function passed
or stored as a value; a method on a receiver of unknown type (every method of that name in the
indexed code); modules loaded through a helper that wraps `importlib.import_module` when the name
is a constant; and CODE CARRIED IN STRINGS — the estate ships bodies as text, so every string
constant a reachable function holds (the static parts of an f-string included) is tokenised and
its `alias.function(` calls resolved through the package's binding tables.

WHAT MAKES A HANDLER OPAQUE, each reported with file and line:

    * `exec` / `eval` outside a declared door (a function whose bodies are the strings above)
    * `importlib.import_module` / `__import__` of a name that is not a constant
    * a call on the direct result of `getattr(x, <not a constant>)`, unless `x` is an object a
      library outside the index returned (its methods are that library's)
    * a string body that calls through an alias no binding table names, that names a function
      its module does not define, or that itself uses `exec`, `eval`, `getattr` or `__import__`

A FRAME ADVANCE is a zero-argument `.update()` call (the Kit app's; nothing else in this estate
is called that way) or a `.step(...)` on a simulation world or context. `await
app.next_update_async()` is not one: a coroutine that yields does not re-enter the loop.

A static reading of a dynamic language has a residue — a call assembled from two string pieces is
invisible here. The bridge's runtime witness (`DOORWAY_UNMARKED_PUMP`) exists for that residue.

Stdlib only, importable anywhere, like the rest of `kit_mcp`.
"""
import ast
import io
import os
import tokenize
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

PUMPS = "PUMPS"
OPAQUE = "OPAQUE"
ORDINARY = "ORDINARY"

#: Receivers a `.step(...)` advances frames on, by the terminal name of the receiver expression.
_STEP_RECEIVERS = ("world", "sim", "ctx", "context", "simulation")
#: Names a string body may call through without that being a call into the indexed code.
_BODY_EXTERNAL = frozenset({
    "json", "_json", "os", "sys", "time", "math", "importlib", "builtins", "carb", "omni", "pxr",
    "Usd", "UsdGeom", "UsdPhysics", "Gf", "Sdf", "np", "numpy", "traceback", "out", "self",
    "str", "dict", "list", "tuple", "set", "print", "len", "PhysxSchema", "isaacsim",
    "pathlib", "re", "shutil", "subprocess", "hashlib", "base64", "io", "socket", "threading",
})
_BODY_DYNAMIC = frozenset({"exec", "eval", "getattr", "__import__"})

Function = Tuple[str, str]      # (module, qualified name)


@dataclass
class _Module:
    name: str
    path: str
    tree: ast.Module
    functions: Dict[str, ast.AST] = field(default_factory=dict)      # qualname -> def node
    classes: Dict[str, List[str]] = field(default_factory=dict)      # class -> method qualnames
    names: Dict[str, tuple] = field(default_factory=dict)            # top-level name -> binding
    strings: Dict[str, str] = field(default_factory=dict)            # top-level NAME = "text"
    parents: Dict[str, Optional[str]] = field(default_factory=dict)  # qualname -> enclosing def


@dataclass
class Verdict:
    handler: str
    kind: str
    #: PUMPS: the call path to the frame advance. OPAQUE: what could not be read, and where.
    because: List[str]


class Analysis:
    """An index of source trees, and the classification of handlers over it.

    `roots` are `(directory, package)` pairs: `package` is the dotted name the directory is
    imported as, or None for a directory whose modules are imported flat. Every module is also
    reachable by its bare file name, because in-cell modules are imported that way off
    `sys.path`. `doors` names the functions allowed to `exec` (their bodies are the strings this
    reads), as `module.qualname`.
    """

    def __init__(self, roots: Iterable[Tuple[str, Optional[str]]], doors: Iterable[str] = ()):
        self.modules: Dict[str, _Module] = {}
        self.flat: Dict[str, List[str]] = {}
        self.methods: Dict[str, List[Function]] = {}
        self.bindings: Dict[str, Set[str]] = {}          # body alias -> flat module names
        self.doors = set(doors)
        self._edges: Dict[Function, tuple] = {}
        for directory, package in roots:
            self._index(os.path.abspath(directory), package)
        for module in self.modules.values():
            self._collect_bindings(module)

    # ------------------------------------------------------------------ indexing
    def _index(self, directory: str, package: Optional[str]) -> None:
        for current, folders, files in os.walk(directory):
            folders[:] = sorted(f for f in folders
                                if f not in ("__pycache__", "tests", "test", ".git", ".venv"))
            for filename in sorted(files):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(current, filename)
                relative = os.path.relpath(path, directory)[:-3].split(os.sep)
                if relative[-1] == "__init__":
                    relative = relative[:-1]
                dotted = ".".join(([package] if package else []) + relative) or (package or "")
                if not dotted:
                    continue
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                module = _Module(name=dotted, path=path, tree=tree)
                self._define(module, tree.body, prefix="", enclosing=None, in_class=None)
                self._top_level(module)
                self.modules[dotted] = module
                self.flat.setdefault(dotted.rsplit(".", 1)[-1], []).append(dotted)

    def _define(self, module, body, prefix, enclosing, in_class) -> None:
        for node in body:
            for child in _statements(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = prefix + child.name
                    module.functions[qualname] = child
                    module.parents[qualname] = enclosing
                    if in_class is not None:
                        module.classes[in_class].append(qualname)
                        self.methods.setdefault(child.name, []).append((module.name, qualname))
                    self._define(module, child.body, qualname + ".", qualname, None)
                elif isinstance(child, ast.ClassDef):
                    qualname = prefix + child.name
                    module.classes[qualname] = []
                    self._define(module, child.body, qualname + ".", enclosing, qualname)

    def _top_level(self, module: _Module) -> None:
        for node in module.tree.body:
            for child in _statements(node):
                self._bind(module, child, module.names, module.strings)

    def _bind(self, module, node, names, strings=None) -> None:
        """Record what one statement binds: a def, a class, an import, a constant."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return                               # resolved through `functions` by scope
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                names[bound] = ("module", alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            base = self._relative(module.name, node.level, node.module, module.path)
            for alias in node.names:
                names[alias.asname or alias.name] = ("from", base, alias.name)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
            text = _static_text(value)
            if text is not None and strings is not None:
                strings[target] = text
            loaded = self._loaded_modules(module, value, names)
            if loaded:
                names[target] = ("modules", tuple(loaded))

    def _relative(self, module_name, level, target, path) -> str:
        if not level:
            return target or ""
        parts = module_name.split(".")
        if not path.endswith("__init__.py"):
            parts = parts[:-1]
        parts = parts[:len(parts) - (level - 1)] if level > 1 else parts
        return ".".join(parts + ([target] if target else []))

    def _collect_bindings(self, module: _Module) -> None:
        """Every `{alias: module}` table and `dict(table, alias="module")` in the module: how a
        string body's names map to the modules the cell imports for it."""
        for node in ast.walk(module.tree):
            pairs = []
            if isinstance(node, ast.Dict):
                pairs = [(k.value, v.value) for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)
                         and isinstance(v, ast.Constant) and isinstance(v.value, str)]
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "dict":
                pairs = [(k.arg, k.value.value) for k in node.keywords
                         if k.arg and isinstance(k.value, ast.Constant)
                         and isinstance(k.value.value, str)]
            for alias, target in pairs:
                if alias.isidentifier() and self._modules_named(target):
                    self.bindings.setdefault(alias, set()).add(target)

    def _modules_named(self, name: str) -> List[str]:
        if name in self.modules:
            return [name]
        return list(self.flat.get(name.rsplit(".", 1)[-1], [])) if "." not in name \
            else [m for m in self.modules if m.endswith("." + name)]

    # ------------------------------------------------------------------ resolution
    def _scope(self, module: _Module, qualname: Optional[str]) -> Dict[str, tuple]:
        """Names visible inside a function: the module's, then each enclosing def's own."""
        chain, current = [], qualname
        while current is not None:
            chain.append(current)
            current = module.parents.get(current)
        names = dict(module.names)
        for scope in reversed(chain):
            node = module.functions[scope]
            for child in _own_nodes(node):
                self._bind(module, child, names)
            for nested, parent in module.parents.items():
                if parent == scope:
                    names[nested.rsplit(".", 1)[-1]] = ("function", nested)
        return names

    def _loaded_modules(self, module, value, names,
                        qualname: Optional[str] = None) -> Optional[List[str]]:
        """The modules a call may load: `[names]` when every name is known from the tree, `[]`
        when it loads one whose name is NOT known (the caller reports that), and None when the
        expression is not a module load at all.

        A name is known when it is a constant, or a parameter of the function (or of a def that
        encloses it) that every call site in the module passes a constant for — the shape of a
        registration helper called once per tool with that tool's module.
        """
        if not isinstance(value, ast.Call) or not value.args:
            return None
        prefix = None
        if _is_import_call(value.func):
            prefix = ""
        elif isinstance(value.func, ast.Name):
            prefix = self._loader(module, value.func.id, names)
        if prefix is None:
            return None
        argument = value.args[0]
        text = _static_text(argument)
        if text is not None:
            return [prefix + text]
        if isinstance(argument, ast.Name) and qualname is not None:
            constants = self._parameter_constants(module, qualname, argument.id)
            if constants:
                return [prefix + constant for constant in constants]
        if isinstance(argument, ast.JoinedStr) and len(argument.values) == 2 \
                and isinstance(argument.values[0], ast.Constant) \
                and isinstance(argument.values[1], ast.FormattedValue) \
                and isinstance(argument.values[1].value, ast.Name) and qualname is not None:
            constants = self._parameter_constants(module, qualname, argument.values[1].value.id)
            if constants:
                return [prefix + argument.values[0].value + c for c in constants]
        return []

    def _parameter_constants(self, module, qualname, parameter) -> List[str]:
        """Every constant a parameter is called with, when ALL its call sites pass one."""
        scope = qualname
        while scope is not None:
            node = module.functions.get(scope)
            arguments = [a.arg for a in node.args.args] if node is not None \
                and not isinstance(node, ast.Lambda) else []
            if parameter in arguments:
                position = arguments.index(parameter)
                simple = scope.rsplit(".", 1)[-1]
                found: List[str] = []
                for call in ast.walk(module.tree):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                            and call.func.id == simple:
                        value = call.args[position] if len(call.args) > position else next(
                            (k.value for k in call.keywords if k.arg == parameter), None)
                        text = _static_text(value) if value is not None else None
                        if text is None:
                            return []
                        found.append(text)
                return sorted(set(found))
            scope = module.parents.get(scope)
        return []

    def _loader(self, module, name, names) -> Optional[str]:
        """The prefix a helper prepends when it is `return importlib.import_module(<its arg>)`."""
        binding = names.get(name)
        qualname = binding[1] if binding and binding[0] == "function" else name
        node = module.functions.get(qualname)
        if node is None or not node.args.args:
            return None
        argument = node.args.args[0].arg
        for child in _own_nodes(node):
            if isinstance(child, ast.Return) and isinstance(child.value, ast.Call) \
                    and _is_import_call(child.value.func) and child.value.args:
                loaded = child.value.args[0]
                if isinstance(loaded, ast.Name) and loaded.id == argument:
                    return ""
                if isinstance(loaded, ast.JoinedStr) and len(loaded.values) == 2 \
                        and isinstance(loaded.values[0], ast.Constant) \
                        and isinstance(loaded.values[1], ast.FormattedValue) \
                        and isinstance(loaded.values[1].value, ast.Name) \
                        and loaded.values[1].value.id == argument:
                    return loaded.values[0].value
        return None

    def _targets(self, module_name: str, attribute: str,
                 _seen: Optional[Set[Tuple[str, str]]] = None) -> Tuple[List[Function], bool]:
        """What `module.attribute` is, as functions to follow; and whether the name exists."""
        found: List[Function] = []
        exists = False
        seen = set() if _seen is None else _seen
        if (module_name, attribute) in seen:
            return found, True                   # a re-export cycle: already being followed
        seen.add((module_name, attribute))
        for candidate in self._modules_named(module_name):
            target = self.modules[candidate]
            if attribute in target.functions:
                found.append((candidate, attribute))
                exists = True
            elif attribute in target.classes:
                found.extend((candidate, m) for m in target.classes[attribute])
                exists = True
            elif attribute in target.names or attribute in target.strings:
                exists = True
                binding = target.names.get(attribute)
                if binding and binding[0] == "from":
                    more, _ = self._targets(binding[1], binding[2], seen)
                    found.extend(more)
            elif self._modules_named(candidate + "." + attribute):
                exists = True
        return found, exists

    # ------------------------------------------------------------------ one function
    def edges(self, function: Function) -> tuple:
        """(callees, pump lines, opaque reasons) of one function, read once."""
        if function in self._edges:
            return self._edges[function]
        module = self.modules[function[0]]
        node = module.functions[function[1]]
        names = self._scope(module, function[1])
        where = f"{os.path.basename(module.path)}"
        callees: List[Function] = []
        pumps: List[str] = []
        opaque: List[str] = []
        is_door = f"{function[0]}.{function[1]}" in self.doors
        near = {module.name}
        for binding in names.values():
            if binding[0] == "module":
                near.update(self._modules_named(binding[1]))
            elif binding[0] == "from":
                near.update(self._modules_named(binding[1]))
                near.update(self._modules_named(f"{binding[1]}.{binding[2]}"))
        texts: List[Tuple[int, str]] = []

        def follow(name_binding) -> List[Function]:
            kind = name_binding[0]
            if kind == "function":
                return [(module.name, name_binding[1])]
            if kind == "from":
                return self._targets(name_binding[1], name_binding[2])[0]
            return []

        def modules_of(expression) -> Optional[List[str]]:
            """The modules an expression may denote; None when it denotes none."""
            if isinstance(expression, ast.Name):
                binding = names.get(expression.id)
                if binding is None:
                    return None
                if binding[0] == "module":
                    return [binding[1]]
                if binding[0] == "modules":
                    return list(binding[1])
                if binding[0] == "from":
                    dotted = f"{binding[1]}.{binding[2]}" if binding[1] else binding[2]
                    return [dotted] if self._modules_named(dotted) else None
                return None
            if isinstance(expression, ast.Attribute):
                base = modules_of(expression.value)
                return None if base is None else [f"{b}.{expression.attr}" for b in base]
            loaded = self._loaded_modules(module, expression, names, function[1])
            return loaded or None

        def external_root(expression) -> bool:
            """Whether an expression is rooted in an import this index does not hold."""
            while isinstance(expression, (ast.Attribute, ast.Call)):
                expression = expression.value if isinstance(expression, ast.Attribute) \
                    else expression.func
            if not isinstance(expression, ast.Name):
                return False
            binding = names.get(expression.id)
            if binding is None or binding[0] not in ("module", "from"):
                return False
            dotted = binding[1] if binding[0] == "module" else (binding[1] or binding[2])
            return not self._modules_named(dotted) \
                and not self._modules_named(dotted.split(".")[0])

        def external_object(expression) -> bool:
            """Whether a name holds an object made by a library outside this index (a USD API
            object, an Isaac world): a dynamic attribute of one is that library's method, not
            code this index was asked to read."""
            if not isinstance(expression, ast.Name):
                return external_root(expression)
            values = [child.value for child in _own_nodes(node)
                      if isinstance(child, ast.Assign) and len(child.targets) == 1
                      and isinstance(child.targets[0], ast.Name)
                      and child.targets[0].id == expression.id]
            parts = []
            for value in values:
                parts.extend(value.values if isinstance(value, ast.BoolOp) else [value])
            return bool(parts) and all(isinstance(part, ast.Call) and external_root(part)
                                       for part in parts)

        docstring = ast.get_docstring(node, clean=False) if not isinstance(node, ast.Lambda) \
            else None
        for child in _own_nodes(node):
            line = getattr(child, "lineno", 0)
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if child.value != docstring:
                    texts.append((line, child.value))
            elif isinstance(child, ast.JoinedStr):
                texts.append((line, _static_text(child, placeholder=" _X_ ") or ""))
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                binding = names.get(child.id)
                if binding is not None:
                    callees.extend(follow(binding))
                elif child.id in module.functions and module.parents.get(child.id) is None:
                    callees.append((module.name, child.id))
                elif child.id in module.classes:
                    callees.extend((module.name, m) for m in module.classes[child.id])
                if child.id in module.strings:
                    texts.append((line, module.strings[child.id]))
            elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                owners = [o for o in (modules_of(child.value) or ()) if self._modules_named(o)]
                if owners:
                    for owner in owners:
                        callees.extend(self._targets(owner, child.attr)[0])
                elif isinstance(child.value, ast.Name) and child.value.id in ("self", "cls"):
                    owner_class = function[1].rsplit(".", 1)[0]
                    if f"{owner_class}.{child.attr}" in module.functions:
                        callees.append((module.name, f"{owner_class}.{child.attr}"))
            if not isinstance(child, ast.Call):
                continue
            callee = child.func
            if isinstance(callee, ast.Name) and callee.id in ("exec", "eval") and not is_door:
                opaque.append(f"{where}:{line} {function[1]} calls {callee.id}() and is not a "
                              f"declared door")
            if self._loaded_modules(module, child, names, function[1]) == []:
                # A helper that IS the loader (`return import_module(<its argument>)`) is read at
                # its call sites, where the name is; everywhere else an unknown name is unread code.
                own = function[1].rsplit(".", 1)[-1]
                enclosing = self._scope(module, module.parents.get(function[1]))
                if not (_is_import_call(callee)
                        and self._loader(module, own, enclosing) is not None):
                    opaque.append(f"{where}:{line} {function[1]} loads a module whose name is "
                                  f"not known from the tree ({_spelled(callee)}(...))")
            if isinstance(callee, ast.Call) and isinstance(callee.func, ast.Name) \
                    and callee.func.id == "getattr" and len(callee.args) >= 2 \
                    and _static_text(callee.args[1]) is None \
                    and not external_object(callee.args[0]):
                opaque.append(f"{where}:{line} {function[1]} calls the result of getattr() "
                              f"with a name that is not a constant")
            if isinstance(callee, ast.Attribute):
                awaited = False
                if callee.attr == "update" and not child.args and not child.keywords:
                    pumps.append(f"{where}:{line} {function[1]}: "
                                 f"{_spelled(callee)}() advances a frame synchronously")
                elif callee.attr == "step" and (
                        any(k.arg == "render" for k in child.keywords)
                        or any(word in _terminal(callee.value).lower()
                               for word in _STEP_RECEIVERS)):
                    pumps.append(f"{where}:{line} {function[1]}: "
                                 f"{_spelled(callee)}(...) steps the simulation synchronously")
                owner = modules_of(callee.value)
                receiver_known = (owner is not None
                                  or (isinstance(callee.value, ast.Name)
                                      and callee.value.id in ("self", "cls")))
                if not receiver_known and not awaited:
                    # A method on a receiver of unknown type: every method of that name on a
                    # class this module defines or imports from. An object's class is one its
                    # user could name; methods of unrelated modules are not candidates.
                    callees.extend(m for m in self.methods.get(callee.attr, ())
                                   if m[0] in near)

        for line, text in texts:
            more, reasons = self._body(text, f"{where}:{line} {function[1]}")
            callees.extend(more)
            opaque.extend(reasons)

        result = (sorted(set(c for c in callees if c != function)), pumps, opaque)
        self._edges[function] = result
        return result

    def _body(self, text: str, where: str) -> Tuple[List[Function], List[str]]:
        """Calls a string makes, when the string is code the cell will run."""
        tokens = _tokens(text)
        calls, assigned, dynamic, imported = [], set(), [], {}
        for index, token in enumerate(tokens):
            following = tokens[index + 1:index + 4]
            if token in _BODY_DYNAMIC and following[:1] == ["("]:
                dynamic.append(token)
            if token == "import" and following and following[0].isidentifier() \
                    and self._modules_named(following[0]):
                imported[following[0]] = following[0]      # an indexed module, imported by name
            if token.isidentifier() and following[:1] == ["="] and following[1:2] != ["="]:
                assigned.add(token)
            if token.isidentifier() and len(following) == 3 and following[0] == "." \
                    and following[1].isidentifier() and following[2] == "(" \
                    and (index == 0 or tokens[index - 1] != "."):
                calls.append((token, following[1]))
        resolved: List[Function] = []
        unknown: List[str] = []
        missing: List[str] = []
        for alias, attribute in calls:
            targets = set(self.bindings.get(alias, ())) | set(imported.get(alias, ()) and [alias])
            if not targets and alias in self.flat and alias not in _BODY_EXTERNAL:
                targets = {alias}
            if not targets:
                if alias not in _BODY_EXTERNAL and alias not in assigned:
                    unknown.append(f"{alias}.{attribute}(")
                continue
            found_any = False
            for target in targets:
                functions, exists = self._targets(target, attribute)
                resolved.extend(functions)
                found_any = found_any or exists
            if not found_any:
                missing.append(f"{alias}.{attribute}(")
        is_code = bool(resolved or missing) or ("out" in assigned)
        if not is_code:
            return [], []
        reasons = [f"{where}: a string body calls {call} and no binding table names that alias"
                   for call in unknown]
        reasons += [f"{where}: a string body calls {call}, which its module does not define"
                    for call in missing]
        reasons += [f"{where}: a string body uses {name}()" for name in dynamic]
        return resolved, reasons

    # ------------------------------------------------------------------ classification
    def classify(self, handler: Function) -> Verdict:
        """Follow every call from `handler`; say what was found."""
        label = f"{handler[0]}.{handler[1]}"
        if handler[0] not in self.modules or handler[1] not in self.modules[handler[0]].functions:
            return Verdict(label, OPAQUE, [f"{label} is not a function this index holds"])
        came_from: Dict[Function, Optional[Function]] = {handler: None}
        queue: List[Function] = [handler]
        opaque: List[str] = []
        while queue:
            current = queue.pop(0)
            callees, pumps, reasons = self.edges(current)
            if pumps:
                path, step = [], current
                while step is not None:
                    path.append(f"{step[0]}.{step[1]}")
                    step = came_from[step]
                return Verdict(label, PUMPS, list(reversed(path)) + [pumps[0]])
            opaque.extend(reasons)
            for callee in callees:
                if callee not in came_from and callee[0] in self.modules \
                        and callee[1] in self.modules[callee[0]].functions:
                    came_from[callee] = current
                    queue.append(callee)
        if opaque:
            return Verdict(label, OPAQUE, sorted(set(opaque)))
        return Verdict(label, ORDINARY, [])

    def classify_lambda(self, module_name: str, enclosing: Optional[str], node: ast.AST,
                        label: str) -> Verdict:
        """Classify a handler that is an expression (a lambda, a name) inside `enclosing`."""
        module = self.modules[module_name]
        qualname = f"<handler {label}>"
        module.functions[qualname] = node if isinstance(node, ast.Lambda) else \
            ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[],
                                          defaults=[]), body=node)
        module.parents[qualname] = enclosing
        verdict = self.classify((module_name, qualname))
        return Verdict(label, verdict.kind, verdict.because)


# ---------------------------------------------------------------------- syntax helpers
def _statements(node):
    """A statement and the statements nested in its control flow (not in defs or classes)."""
    yield node
    for name in ("body", "orelse", "finalbody", "handlers"):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        for child in getattr(node, name, []) or []:
            if isinstance(child, ast.AST):
                yield from _statements(child)


def _own_nodes(node):
    """Every node of a function EXCEPT the bodies of the defs and classes nested in it.

    A nested def is its own function, followed when it is named; a lambda is not, so its body
    belongs to the function that wrote it."""
    body = node.body if isinstance(node.body, list) else [node.body]
    pending = list(body)
    if not isinstance(node, ast.Lambda):
        pending.extend(node.args.defaults)
        pending.extend(d for d in node.args.kw_defaults if d is not None)
    while pending:
        current = pending.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            pending.extend(getattr(current, "decorator_list", []))
            continue
        pending.extend(ast.iter_child_nodes(current))


def _static_text(node, placeholder: Optional[str] = None) -> Optional[str]:
    """A string known from the tree: a constant, constants joined with `+`, or — when a
    `placeholder` is given — an f-string with each formatted value replaced by it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left, placeholder), _static_text(node.right, placeholder)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr) and placeholder is not None:
        return "".join(part.value if isinstance(part, ast.Constant) else placeholder
                       for part in node.values)
    return None


def _is_import_call(callee) -> bool:
    if isinstance(callee, ast.Name):
        return callee.id in ("__import__", "import_module")
    return isinstance(callee, ast.Attribute) and callee.attr == "import_module"


def _terminal(expression) -> str:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    if isinstance(expression, ast.Call):
        return _terminal(expression.func)
    return ""


def _spelled(expression) -> str:
    try:
        return ast.unparse(expression)
    except Exception:                                                         # noqa: BLE001
        return _terminal(expression)


def _tokens(text: str) -> List[str]:
    """The Python tokens of a string that may be only a fragment of code."""
    found: List[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.NAME, tokenize.OP):
                found.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass                      # a fragment ends mid-expression; what was read stands
    return found
