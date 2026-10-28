"""What a failed bind MEANS, as a typed answer rather than one guess.

The bridge binds one socket at startup on an address somebody DECLARED — a port from
`knobs.PORT`, an address from `knobs.BIND`. When that bind fails there is more than one
reason it can fail, and they send whoever reads the log to different places:

  EADDRNOTAVAIL   the ADDRESS does not exist on this box. Nothing is holding it; there is
                  no interface carrying it. The fix is upstream of this process.
  EADDRINUSE      the PORT is held by another process on an address that does exist.
  EACCES          the port is privileged and this process is not.

Treating every bind `OSError` as "port taken" and falling back to an ephemeral port fails
three ways at once:

  * a typed condition becomes a MISDIAGNOSIS: when the address does not exist, a log saying the
    port is taken sends the reader to hunt a process that was never there;
  * a DECLARED address is silently abandoned for one nobody asked for;
  * and the bridge comes up on a port no client can predict, so whatever waits for it times out
    with no cause in it — the least informative shape a known error can take.

So: **a declared bind that fails REFUSES, loudly, naming the address and the reason.** An
extension that fails to start reports its own cause, and the readiness check that is waiting on
it reports that cause instead of a timeout. There is no fallback here, and there is no
undeclared-bind case that would want one: the bridge's socket is only started when a port was
configured (`extension.on_startup`), so every bind this module speaks for is a declared one.

`McpBridge.start(0)` is untouched and stays what it always was: a CALLER asking for an
OS-assigned port, which is a request rather than a recovery. Discovery advertises it through the
portfile exactly as before.
"""

import errno

#: What each errno means to whoever is reading the log, and where it sends them. A table
#: rather than a chain of `if`s because these are parallel facts about one syscall, and the
#: reader's next step is different for each — which is the whole reason the bare `except
#: OSError` was wrong.
_REASONS = {
    errno.EADDRNOTAVAIL: (
        "the ADDRESS does not exist on this box. Nothing is holding it — there is no "
        "interface carrying {host}. Whatever creates that address has not run yet or has "
        "gone: for an enclave-bridge address it is the bridge network, for a tailnet or "
        "campus address it is the link itself"
    ),
    errno.EADDRINUSE: (
        "another process already holds {host}:{port}. The address exists and something is "
        "listening on it; `--list-bridges` names the bridges this box is advertising"
    ),
    errno.EACCES: (
        "this process may not bind {host}:{port}. Ports below 1024 are privileged and the "
        "bridge is not run as root, deliberately — it serves run_python"
    ),
}


def declared_bind_refusal(host: str, port: int, error: OSError) -> RuntimeError:
    """The refusal for a DECLARED bind that failed, naming the address and the reason.

    Returned rather than raised so the caller keeps the `raise ... from error` and the original
    traceback stays attached: the errno is the evidence and it should not be paraphrased away.

    An errno with no entry above is reported as itself. A reason this module does not know is
    still a reason, and inventing the nearest one it does know is exactly the substitution that
    made `[Errno 99]` read as "port taken".
    """
    reason = _REASONS.get(error.errno)
    detail = (
        reason.format(host=host, port=port) if reason
        else f"the bind failed with errno {error.errno} ({error.strerror or error})"
    )
    return RuntimeError(
        f"omni.kit.mcp: cannot serve on the declared address {host}:{port} — {detail}. "
        f"The bridge does NOT fall back to another port: the address was declared, a client "
        f"is going to dial exactly it, and a bridge that quietly moved would be a bridge "
        f"nobody can find — which is how a readiness check comes to report a timeout instead "
        f"of a cause. Fix the address or change what declared it "
        f"(OMNI_KIT_MCP_BIND / OMNI_KIT_MCP_PORT, or the persisted autostart settings)."
    )
