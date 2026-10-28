"""Declared toolsets — a plain library module IS its tool surface.

One declaration replaces N per-tool registration files: point the bridge at a
module (``register_toolset``, or autoload a package that has no ``register()``
entrypoint) and every public function becomes a served tool. Schemas derive
from signatures — type hints map to JSON kinds, a default marks the parameter
optional — and descriptions from docstrings. Async functions are first-class:
dispatch awaits them on Kit's loop, exactly like hand-registered handlers.

Public surface: ``__all__`` when the module defines it, else every public
function defined in the module's own tree (imported names don't leak).

Staleness: registration fingerprints the module's source files (sha256),
surfaced per-namespace by the ``status`` builtin — a client compares hashes
to detect that the box serves older code than it expects.

Stdlib-only; importable off-Kit (tests, lint).
"""

import hashlib
import importlib
import inspect
import os
import typing
from typing import Any, Callable, Dict, List, Optional

from .bridge import ToolDefinition

# Order matters: bool is an int subclass, so it must match first.
_KINDS = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (list, "array"),
    (tuple, "array"),
    (dict, "object"),
)


def _json_kind(annotation) -> Optional[Dict[str, Any]]:
    """Annotation -> {"type": <json kind>} (plus the full schema for
    pydantic-style models); None when no useful mapping exists — an untyped
    parameter stays unconstrained rather than mis-advertised."""
    if annotation is inspect.Parameter.empty:
        return None
    origin = typing.get_origin(annotation)
    if origin is typing.Union:                     # Optional[T] / Union[T, None]
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _json_kind(args[0]) if len(args) == 1 else None
    if origin is not None:                         # List[int] -> list, ...
        annotation = origin
    schema_fn = getattr(annotation, "model_json_schema", None)
    if callable(schema_fn):                        # pydantic BaseModel, best-effort
        try:
            return {"type": "object", "schema": schema_fn()}
        except Exception:
            return {"type": "object"}
    for typ, kind in _KINDS:
        if isinstance(annotation, type) and issubclass(annotation, typ):
            return {"type": kind}
    return None


def derive_parameters(fn: Callable) -> Dict[str, Dict[str, Any]]:
    """A function signature as a tool parameter schema.

    Type hints map to JSON kinds; a parameter without a default is required;
    ``*args``/``**kwargs`` don't advertise (they still work at call time)."""
    params: Dict[str, Dict[str, Any]] = {}
    for p in inspect.signature(fn).parameters.values():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                      inspect.Parameter.VAR_KEYWORD):
            continue
        spec: Dict[str, Any] = {}
        kind = _json_kind(p.annotation)
        if kind:
            spec.update(kind)
        if p.default is inspect.Parameter.empty:
            spec["required"] = True
        else:
            spec["default"] = p.default
        params[p.name] = spec
    return params


#: Names the loader asks a package for, which are therefore NOT part of its agent-facing surface.
#: `install_policy` matters most: derived as a tool it would be an agent-callable way to re-arm —
#: or re-install — the very gate that refuses that agent's calls.
_NOT_TOOLS = frozenset({"register", "register_tools", "install_policy"})


def _public_functions(module) -> List[Callable]:
    """The module's declared surface: exactly ``__all__`` when defined, else
    every public function defined in the module's own tree."""
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return [fn for fn in (getattr(module, n, None) for n in declared)
                if callable(fn)]
    prefix = module.__name__
    return [
        fn for name, fn in sorted(vars(module).items())
        if not name.startswith("_")
        and name not in _NOT_TOOLS
        and inspect.isfunction(fn)
        and (fn.__module__ == prefix or fn.__module__.startswith(prefix + "."))
    ]


def derive_tools(module) -> List[ToolDefinition]:
    """Every public function as a ToolDefinition — description from the
    docstring's first line, schema from the signature."""
    tools = []
    for fn in _public_functions(module):
        doc = (inspect.getdoc(fn) or fn.__name__).strip().splitlines()[0]
        tools.append(ToolDefinition(
            name=fn.__name__, description=doc,
            parameters=derive_parameters(fn), handler=fn))
    if not tools:
        raise ValueError(f"{module.__name__} declares no public functions to serve")
    return tools


def source_hash(module) -> Optional[str]:
    """sha256 over the module's source file(s) — the staleness fingerprint
    ``status`` surfaces per namespace. None when there is no file (in-memory
    modules, frozen apps)."""
    f = getattr(module, "__file__", None)
    if not f or not os.path.exists(f):
        return None
    h = hashlib.sha256()
    if os.path.basename(f) == "__init__.py":       # package: the whole tree
        root = os.path.dirname(f)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if name.endswith(".py"):
                    p = os.path.join(dirpath, name)
                    h.update(os.path.relpath(p, root).encode())
                    with open(p, "rb") as fh:
                        h.update(fh.read())
    else:
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def register_toolset(bridge, module, namespace: Optional[str] = None,
                     owner_id: Optional[str] = None) -> Dict[str, Any]:
    """Introspect ``module`` (object or import path) and register every
    public function as a tool under ``namespace``.

    Namespace defaults to the module's ``MCP_NAMESPACE``, else its bare name.
    Rolls the owner back on any failure so a retry starts clean. Re-declaring
    after an edit rides the normal reload machinery: a register()-less package
    goes through ``reload_tools`` like any other (autoload falls back here).
    """
    if isinstance(module, str):
        module = importlib.import_module(module)
    mod_name = module.__name__
    oid = owner_id or mod_name
    ns = str(namespace or getattr(module, "MCP_NAMESPACE", None)
             or mod_name.rsplit(".", 1)[-1])
    fingerprint = source_hash(module)
    registrar = bridge.register_owner(
        owner_id=oid, namespace=ns,
        metadata={"module": mod_name, "toolset": True,
                  "source_hash": fingerprint,
                  # What the module says it was written against (bridge.ADMISSION_CONTRACT);
                  # None when it says nothing, and the bridge then refuses its tools by name.
                  "admission": getattr(module, "DOORWAY_ADMISSION", None)})
    try:
        for tool in derive_tools(module):
            registrar.add(tool)
    except Exception:
        bridge.unregister_owner(oid)
        raise
    return {"namespace": ns, "source_hash": fingerprint,
            "tools": sorted(bridge.get_owners()[oid]["tools"])}
