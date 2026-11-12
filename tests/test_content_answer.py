"""A tool's image reaches its caller as content only from a sealed owner its launcher names, and only a real image.

Checked through a real MCP client session against the gateway's own tool registration, so what is asserted is
what a client receives, not what a function returned. With ``KIT_MCP_CONTENT_TOOLS`` unset the gateway converts
nothing: every tool registers, publishes its output schema and answers with its JSON text. The bridge's ``list_tools`` row is
unchanged for a tool that declares no content under an owner that is not sealed; a declaring tool's row and a sealed
owner's gain ``content_blocks`` and ``sealed`` whatever the gateway's environment.
"""

import asyncio
import base64
import hashlib
import json
import struct
import zlib

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from omni_kit_mcp.bridge import McpBridge, OwnerRegistrar, OwnerSealed, ToolDefinition

from kit_mcp import content as content_rules
from kit_mcp import server

OWNER = "trusted.lab_tools"


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def png(width=4, height=3, *, interlace=0, filter_byte=0, bad_pass=None, extra=(), stream_tail=b""):
    """An RGB PNG: valid chunk CRCs, a real header, pixel data of exactly the promised size (every pass's rows when
    interlaced), and any ``extra`` chunks between IHDR and IDAT. With ``bad_pass``, only that pass's rows carry
    ``filter_byte`` and every other pass's rows carry filter 0."""
    passes = content_rules._png_passes(width, height, 8, 2, interlace)
    raw = b"".join(bytes([filter_byte if bad_pass in (None, index) else 0])
                   + bytes((r * 40 + i * 9) % 256 for i in range(row - 1))
                   for index, (rows, row) in enumerate(passes) for r in range(rows))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, interlace))
            + b"".join(chunk(kind, body) for kind, body in extra)
            + chunk(b"IDAT", zlib.compress(raw) + stream_tail) + chunk(b"IEND", b""))


PNG = png()
IMAGE = {"type": "image", "data": base64.b64encode(PNG).decode("ascii"), "mimeType": "image/png"}
FACTS = {"type": "text", "text": json.dumps({"width": 4, "height": 3, "sha256": hashlib.sha256(PNG).hexdigest()})}
APPROVED = {"description": "capture", "fields": [], "namespace": "lab", "owner_id": OWNER,
            "content_blocks": True, "sealed": True}


@pytest.fixture(autouse=True)
def empty_hook_lists(monkeypatch):
    monkeypatch.setattr(server, "_PRE_DISPATCH_HOOKS", [])
    monkeypatch.setattr(server, "_POST_DISPATCH_HOOKS", [])
    monkeypatch.setenv("KIT_MCP_CONTENT_TOOLS", f"lab.camera_capture@{OWNER}")


class _Bridge:
    def __init__(self, answer):
        self.answer = answer

    def call(self, canonical, params=None):
        return self.answer


def _gateway(monkeypatch, answer, meta):
    monkeypatch.setattr(server, "get_client", lambda: _Bridge(answer))
    gateway = FastMCP("test")
    server.register_dynamic_tool(gateway, "lab__camera_capture", "lab.camera_capture", meta)
    return gateway


def _call(monkeypatch, answer, meta=APPROVED):
    gateway = _gateway(monkeypatch, answer, meta)

    async def run():
        async with create_connected_server_and_client_session(gateway._mcp_server) as client:
            return await client.call_tool("lab__camera_capture", {}), (await client.list_tools()).tools[0]

    return asyncio.run(run())


# ----------------------------------------------------------------------------- the approved tool

#: Through a client, an approved tool's image needs an mcp that passes a tool's own result through; under an older
#: one the same tool answers KIT_MCP_CONTENT_UNSUPPORTED (test_an_approved_tool_under_an_mcp_too_old_...).
needs_content_mcp = pytest.mark.skipif(not server._mcp_passes_content(),
                                       reason="this mcp is older than 1.19: content is version-gated off")


@needs_content_mcp
def test_an_approved_tool_reaches_the_client_as_a_real_image_and_its_text(monkeypatch):
    result, listed = _call(monkeypatch, {server.MCP_CONTENT_KEY: [IMAGE, FACTS]})

    assert result.isError is False
    assert [block.type for block in result.content] == ["image", "text"]
    assert base64.b64decode(result.content[0].data) == PNG and result.content[0].mimeType == "image/png"
    assert result.content[1].text == FACTS["text"]
    assert result.structuredContent is None and listed.outputSchema is None, \
        "a client that finds structured content reads it instead of the blocks, and the image never reaches the model"


@pytest.mark.parametrize("change", [
    {"content_blocks": None}, {"sealed": None}, {"owner_id": "agent.lab_tools"}, {"named": "lab.other@" + OWNER},
    {"named": "lab.camera_capture"},
])
def test_a_tool_not_declared_sealed_and_named_with_its_owner_passes_mcp_content_through_as_data(monkeypatch, change):
    meta = {k: v for k, v in {**APPROVED, **change}.items() if v is not None and k != "named"}
    if "named" in change:
        monkeypatch.setenv("KIT_MCP_CONTENT_TOOLS", change["named"])
    answer = {server.MCP_CONTENT_KEY: [IMAGE, FACTS]}
    result, listed = _call(monkeypatch, answer, meta)

    assert [block.type for block in result.content] == ["text"]
    assert json.loads(result.content[0].text) == answer
    assert listed.outputSchema is not None, "an unapproved tool registers exactly as any tool always did"


def test_an_approved_tool_under_an_mcp_too_old_to_pass_content_answers_an_error_by_name(monkeypatch):
    if server._mcp_passes_content():  # under an old mcp the real version check answers on its own
        monkeypatch.setattr(server, "_mcp_passes_content", lambda: False)
    result, listed = _call(monkeypatch, {server.MCP_CONTENT_KEY: [IMAGE, FACTS]})

    assert result.content[0].text.startswith("Error: KIT_MCP_CONTENT_UNSUPPORTED: lab.camera_capture")
    assert listed.outputSchema is not None


@needs_content_mcp
def test_post_dispatch_hooks_still_see_the_answer_as_text(monkeypatch):
    seen = []
    server.register_post_dispatch_hook(lambda name, params, result: seen.append(result))
    result, _ = _call(monkeypatch, {server.MCP_CONTENT_KEY: [IMAGE, FACTS]})

    assert json.loads(seen[0]) == {server.MCP_CONTENT_KEY: [IMAGE, FACTS]}
    assert [block.type for block in result.content] == ["image", "text"]


# ----------------------------------------------------------------------------- what an image may be

def _refused(monkeypatch, blocks):
    """The approved tool's answer to a refused declaration: an error string, on any mcp."""
    text = server._content_answer("lab.camera_capture", json.dumps({server.MCP_CONTENT_KEY: blocks}))
    assert isinstance(text, str)
    assert text.startswith("Error: lab.camera_capture answered with refused mcp_content: mcp_content")
    return text


def _broken_crc(data):
    return data[:-5] + bytes([data[-5] ^ 0xFF]) + data[-4:]


@pytest.mark.parametrize("blocks, named", [
    ([], "non-empty list"),
    ([{**IMAGE, "mimeType": "image/not-real"}], "[0] is an image block whose mimeType is not one of image/png"),
    ([{**IMAGE, "mimeType": "image/jpeg"}], "mimeType is not one of image/png"),
    ([{**IMAGE, "data": "not base64!"}], "[0] is an image block whose data is not base64"),
    ([{**IMAGE, "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(64))).decode()}], "[0] is an image block whose data is a truncated PNG"),
    ([{**IMAGE, "data": base64.b64encode(b"GIF89a" + bytes(32)).decode()}], "is not a PNG"),
    ([{**IMAGE, "data": base64.b64encode(_broken_crc(PNG)).decode()}], "fails its CRC"),
    ([{**IMAGE, "data": base64.b64encode(PNG[:-12]).decode()}], "ends before its IEND"),
    ([{**IMAGE, "extra": 1}], "[0] is a 4-key object of type 'image', neither"),
    ([FACTS, [["secret-token-123"]]], "[1] is a list, neither"),
    ([FACTS] * (content_rules.MAX_BLOCKS + 1), f"has {content_rules.MAX_BLOCKS + 1} blocks"),
    ([{"type": "text", "text": "x" * (content_rules.MAX_TEXT_CHARS + 1)}], "[0] is a text block of"),
])
def test_a_refused_block_is_an_error_naming_its_index_type_and_shape_never_its_value(monkeypatch, blocks, named):
    text = _refused(monkeypatch, blocks)

    assert named in text
    assert "secret-token-123" not in text and IMAGE["data"][:40] not in text


def test_a_png_whose_pixel_data_is_short_of_its_header_is_refused(monkeypatch):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    short = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b""))
    text = _refused(monkeypatch, [{**IMAGE, "data": base64.b64encode(short).decode()}])

    assert "its header promises" in text


def test_an_image_or_an_answer_over_its_bound_is_refused(monkeypatch):
    monkeypatch.setattr(content_rules, "MAX_IMAGE_BYTES", len(PNG) - 1)
    assert "over" in _refused(monkeypatch, [IMAGE])
    monkeypatch.setattr(content_rules, "MAX_IMAGE_BYTES", 10 * len(PNG))
    monkeypatch.setattr(content_rules, "MAX_ANSWER_BYTES", 2 * len(PNG))
    assert "is over" in _refused(monkeypatch, [IMAGE, IMAGE, IMAGE])


def test_a_png_the_validator_accepts_reads_back_its_own_size():
    assert content_rules.png_size(png(37, 11)) == (37, 11)
    assert content_rules.png_size(png(37, 11, interlace=1)) == (37, 11), "an interlaced image, every pass read"


SECRET = b"secret-token-123"


@pytest.mark.parametrize("kind", [b"tEXt", b"zTXt", b"iTXt", b"tIME", b"gAMA", b"eXIf", b"rNDm", b"PLTE"])
def test_a_png_carrying_any_chunk_but_ihdr_idat_iend_is_refused_by_that_chunks_name(kind):
    with pytest.raises(content_rules.ContentRefusal) as refused:
        content_rules.png_size(png(extra=[(kind, SECRET)]))
    assert f"carrying a {kind.decode()!r} chunk" in str(refused.value) and SECRET.decode() not in str(refused.value)


def test_a_paletted_png_is_refused_since_its_palette_is_bytes_nothing_reads():
    paletted = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b""))
    with pytest.raises(content_rules.ContentRefusal, match="paletted PNG"):
        content_rules.png_size(paletted)


def test_bytes_after_the_compressed_pixel_stream_are_refused():
    with pytest.raises(content_rules.ContentRefusal, match="bytes after the compressed stream"):
        content_rules.png_size(png(stream_tail=SECRET))


#: One case per pass that holds rows: non-interlaced has one pass; Adam7 at 5x5 has seven, all non-empty.
_FILTER_CASES = [(interlace, index) for interlace in (0, 1)
                 for index in range(len(content_rules._png_passes(5, 5, 8, 2, interlace)))]


@pytest.mark.parametrize("interlace,bad_pass", _FILTER_CASES)
def test_a_row_naming_no_real_filter_is_refused_on_every_pass(interlace, bad_pass):
    """Each pass is corrupted ALONE (filter 9 on its rows, filter 0 everywhere else), so a validator that checked only
    some passes fails the case for a pass it skips."""
    assert content_rules.png_size(png(5, 5, interlace=interlace)) == (5, 5), "the uncorrupted image is admitted"
    with pytest.raises(content_rules.ContentRefusal, match="names no valid filter"):
        content_rules.png_size(png(5, 5, interlace=interlace, filter_byte=9, bad_pass=bad_pass))


def test_every_adam7_pass_is_its_own_case():
    assert sum(1 for interlace, _ in _FILTER_CASES if interlace == 1) == 7


def test_chunks_out_of_order_are_refused():
    body = png()
    idat_first = body[:8] + body[33:-12] + body[8:33] + body[-12:]
    with pytest.raises(content_rules.ContentRefusal, match="not IHDR, IDAT..., IEND in that order"):
        content_rules.png_size(idat_first)


# ----------------------------------------------------------------------------- the registry

def test_a_registry_with_no_declaring_tool_and_no_sealed_owner_lists_exactly_as_before():
    bridge = McpBridge(schedule_coroutine=lambda coroutine: asyncio.get_event_loop())
    owner = bridge.register_owner("plain", "plain")
    owner.add(ToolDefinition(name="read", description="read", parameters={}, handler=lambda: {}))

    assert set(bridge.get_registered_tools()["plain.read"]) == {"description", "parameters", "fields", "namespace",
                                                                "owner_id"}


def test_a_declaring_tool_and_a_sealed_owner_list_their_keys_and_an_unset_gateway_still_converts_nothing(monkeypatch):
    bridge = McpBridge(schedule_coroutine=lambda coroutine: asyncio.get_event_loop())
    owner = bridge.register_owner(OWNER, "lab")
    owner.add(ToolDefinition(name="camera_capture", description="still", parameters={},
                             handler=lambda: {server.MCP_CONTENT_KEY: [IMAGE]}, content_blocks=True))
    owner.seal()
    row = bridge.get_registered_tools()["lab.camera_capture"]
    assert row["content_blocks"] is True and row["sealed"] is True, "the keys list whatever the gateway names"

    monkeypatch.delenv("KIT_MCP_CONTENT_TOOLS")
    answer = {server.MCP_CONTENT_KEY: [IMAGE, FACTS]}
    result, listed = _call(monkeypatch, answer, row)
    assert [block.type for block in result.content] == ["text"] and json.loads(result.content[0].text) == answer
    assert listed.outputSchema is not None, "unset, the gateway registers the declaring tool exactly as any tool"


def test_the_approved_owner_cannot_be_replaced_or_extended_through_the_bridges_api_once_sealed():
    """Replacing an approved owner through the bridge's API: unregister the launcher's owner, then register a
    look-alike under its name. Code patching the bridge's private state is the stated residual (``OwnerRegistrar.seal``)."""
    bridge = McpBridge(schedule_coroutine=lambda coroutine: asyncio.get_event_loop())
    trusted = bridge.register_owner(OWNER, "lab")
    trusted.add(ToolDefinition(name="camera_capture", description="still", parameters={},
                               handler=lambda: {server.MCP_CONTENT_KEY: [IMAGE]}, content_blocks=True))
    trusted.seal()
    row = bridge.get_registered_tools()["lab.camera_capture"]
    assert row["content_blocks"] is True and row["sealed"] is True and row["owner_id"] == OWNER

    with pytest.raises(OwnerSealed):
        bridge.unregister_owner(OWNER)
    with pytest.raises(ValueError):
        bridge.register_owner(OWNER, "lab2")
    with pytest.raises(ValueError):
        bridge.register_owner("agent", "lab")
    with pytest.raises(OwnerSealed):
        OwnerRegistrar(bridge, OWNER, "lab").add(ToolDefinition(
            name="camera_capture_2", description="x", parameters={}, handler=lambda: {}, content_blocks=True))

    agent = bridge.register_owner("agent", "agent")
    agent.add(ToolDefinition(name="camera_capture", description="x", parameters={}, handler=lambda: {},
                             content_blocks=True))
    agent.seal()
    listing = bridge.get_registered_tools()
    assert listing["lab.camera_capture"]["owner_id"] == OWNER
    assert server._content_tools() == {"lab.camera_capture": OWNER}, "the agent's look-alike is named by nothing"
