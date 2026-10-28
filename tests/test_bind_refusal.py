"""A declared bind that fails refuses, and says which failure it was.

Treating every bind `OSError` as "port taken" and falling back to an ephemeral port would turn
a missing address (`[Errno 99] Cannot assign requested address`) into a misdiagnosis, abandon
the declared address, and leave whatever waits for the bridge to time out with no cause.

So the tests below are one per errno, plus the two properties a fallback would break: that the
declared address is never abandoned, and that the reason reaches whoever is reading.
"""

import errno
import socket

import pytest

from omni_kit_mcp.bind import declared_bind_refusal


def _oserror(number: int) -> OSError:
    return OSError(number, {
        errno.EADDRNOTAVAIL: "Cannot assign requested address",
        errno.EADDRINUSE: "Address already in use",
        errno.EACCES: "Permission denied",
    }.get(number, "unknown"))


def test_an_address_that_does_not_exist_is_not_reported_as_a_taken_port():
    """THE LIVE ONE. `[Errno 99]` means nothing is holding the address because there is no
    address; saying "port taken" sends the reader to hunt a process that was never there."""
    refusal = declared_bind_refusal("192.0.2.1", 9009, _oserror(errno.EADDRNOTAVAIL))
    message = str(refusal)

    assert "ADDRESS does not exist" in message
    assert "192.0.2.1" in message
    assert "taken" not in message, "the misdiagnosis this exists to remove"
    assert "bridge network" in message, "and it names what creates such an address"


def test_a_port_another_process_holds_says_so_and_says_how_to_look():
    refusal = declared_bind_refusal("127.0.0.1", 9009, _oserror(errno.EADDRINUSE))
    message = str(refusal)

    assert "another process already holds 127.0.0.1:9009" in message
    assert "--list-bridges" in message


def test_a_privileged_port_is_its_own_reason():
    refusal = declared_bind_refusal("127.0.0.1", 80, _oserror(errno.EACCES))

    assert "privileged" in str(refusal)
    assert "not run as root" in str(refusal)


def test_an_errno_this_module_does_not_know_is_reported_as_itself():
    """A reason it does not know is still a reason. Substituting the nearest one it does know
    is exactly what made `[Errno 99]` read as "port taken"."""
    refusal = declared_bind_refusal("127.0.0.1", 9009, _oserror(errno.ENETUNREACH))
    message = str(refusal)

    assert f"errno {errno.ENETUNREACH}" in message
    assert "does not exist" not in message and "already holds" not in message


@pytest.mark.parametrize(
    "number", [errno.EADDRNOTAVAIL, errno.EADDRINUSE, errno.EACCES, errno.ENETUNREACH]
)
def test_no_reason_ever_offers_another_port(number):
    """The property the fallback broke, asserted for every branch rather than for the one that
    happened to be measured. A client is going to dial the declared address; a bridge that
    moved would be a bridge nobody can find."""
    message = str(declared_bind_refusal("192.0.2.1", 9009, _oserror(number)))

    assert "does NOT fall back" in message
    assert "ephemeral" not in message


def test_the_real_syscall_produces_the_error_this_module_speaks_for():
    """The errno is not asserted from a table alone. An address no interface carries really
    does raise EADDRNOTAVAIL on this box, which is what the live run met."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        with pytest.raises(OSError) as raised:
            # A documented TEST-NET-1 address (RFC 5737), on no interface anywhere.
            probe.bind(("192.0.2.1", 0))

    assert raised.value.errno == errno.EADDRNOTAVAIL
    assert "ADDRESS does not exist" in str(
        declared_bind_refusal("192.0.2.1", 0, raised.value)
    )


def test_a_port_really_in_use_produces_the_other_one():
    """And the two are genuinely different syscall outcomes, not two readings of one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
            with pytest.raises(OSError) as raised:
                second.bind(("127.0.0.1", taken))

    assert raised.value.errno == errno.EADDRINUSE
    assert "already holds" in str(
        declared_bind_refusal("127.0.0.1", taken, raised.value)
    )
