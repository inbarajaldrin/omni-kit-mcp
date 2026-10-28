"""`kit_mcp.marks` — which handlers advance the simulator, read from the syntax tree.

The reader is what a tool package's test holds its exclusive roster to, so it is tested here on
SYNTHETIC sources: that it finds a pump through every way code reaches one (a call, a method, a
function passed as a value, a body shipped as text), that a pump merely NAMED in a comment, a
docstring or a message is not one, and that code it cannot read is reported OPAQUE and never
classed ordinary by omission.
"""
from kit_mcp import marks


# ---------------------------------------------------------------- the reader itself
def _synthetic(tmp_path, source):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for name, text in source.items():
        (package / f"{name}.py").write_text(text)
    return marks.Analysis([(str(package), "pkg")], doors=("pkg.door._locally",))


def test_NEGATIVE_a_pump_named_only_in_a_comment_a_docstring_or_a_message_is_not_a_pump(tmp_path):
    analysis = _synthetic(tmp_path, {"tools": (
        "def read():\n"
        "    '''This never calls app.update() or R.pump(3).'''\n"
        "    # app.update()\n"
        "    return {'note': 'app.update() is what the composer does'}\n")})
    assert analysis.classify(("pkg.tools", "read")).kind == marks.ORDINARY


def test_a_pump_is_found_through_calls_methods_values_and_a_body_shipped_as_text(tmp_path):
    analysis = _synthetic(tmp_path, {
        "incell": ("import omni.kit.app\n"
                   "def await_here(key):\n"
                   "    app = omni.kit.app.get_app()\n"
                   "    app.update()\n"
                   "def poll(key):\n"
                   "    return {}\n"),
        "door": ("def _locally(body):\n    exec(body, {})\n"
                 "def call(body, bindings):\n    return _locally(body)\n"),
        "tools": ("from . import door\n"
                  "_BINDINGS = {'_jobs': 'incell'}\n"
                  "class Stepper:\n"
                  "    def run(self):\n"
                  "        import omni.kit.app\n"
                  "        omni.kit.app.get_app().update()\n"
                  "def direct():\n    return helper()\n"
                  "def helper():\n    return Stepper().run()\n"
                  "def shipped(key):\n"
                  "    return door.call(f'out = _jobs.await_here({key!r})', _BINDINGS)\n"
                  "def shipped_read(key):\n"
                  "    return door.call(f'out = _jobs.poll({key!r})', _BINDINGS)\n"
                  "async def yields(app):\n"
                  "    await app.next_update_async()\n"
                  "def passed():\n    return run_it(helper)\n"
                  "def run_it(work):\n    return work()\n")})
    kinds = {name: analysis.classify(("pkg.tools", name)).kind
             for name in ("direct", "shipped", "shipped_read", "yields", "passed")}
    assert kinds == {"direct": marks.PUMPS, "shipped": marks.PUMPS, "passed": marks.PUMPS,
                     "shipped_read": marks.ORDINARY, "yields": marks.ORDINARY}


def test_code_the_tree_cannot_read_is_OPAQUE_never_ordinary(tmp_path):
    analysis = _synthetic(tmp_path, {
        "incell": "def poll(key):\n    return {}\n",
        "door": ("def _locally(body):\n    exec(body, {})\n"
                 "def call(body, bindings):\n    return _locally(body)\n"),
        "tools": ("import importlib\n"
                  "from . import door\n"
                  "_BINDINGS = {'_jobs': 'incell'}\n"
                  "def runs_text(code):\n    exec(code)\n"
                  "def loads(name):\n    return importlib.import_module(name).go()\n"
                  "def by_name(thing, verb):\n    return getattr(thing, verb)()\n"
                  "def unbound_alias():\n"
                  "    return door.call('out = _mystery.go(1)', _BINDINGS)\n"
                  "def missing_function():\n"
                  "    return door.call('out = _jobs.not_there(1)', _BINDINGS)\n"
                  "def dynamic_body():\n"
                  "    return door.call('out = getattr(_jobs, \"poll\")(1); _jobs.poll(1)', "
                  "_BINDINGS)\n")})
    for name in ("runs_text", "loads", "by_name", "unbound_alias", "missing_function",
                 "dynamic_body"):
        verdict = analysis.classify(("pkg.tools", name))
        assert verdict.kind == marks.OPAQUE, (name, verdict)
        assert verdict.because and "tools.py:" in verdict.because[0]
