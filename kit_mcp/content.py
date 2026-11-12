"""What a content answer may hold: text, and real PNG images, within fixed bounds.

A tool the gateway hands to its caller as content (``server.MCP_CONTENT_KEY``) is checked here first.
An image block is accepted only as ``image/png`` (the allowlist) whose bytes are nothing but an image:
IHDR, IDAT chunks and an empty IEND in that order, every CRC holding, no palette and no other chunk, the
pixel data inflating to exactly the size the header implies with no byte after the compressed stream,
and every row naming a real filter. So no byte of an accepted image goes unread. Every block, and the
whole answer, has a size bound. A refusal names the block's index, its type and its shape, never a
value the tool returned.
"""

import base64
import binascii
import struct
import zlib
from typing import Any, Dict, List, Tuple

IMAGE_MIME_TYPES = ("image/png",)
MAX_BLOCKS = 8
MAX_TEXT_CHARS = 65_536
MAX_IMAGE_BYTES = 8 * 1024 * 1024
"""Decoded bytes of one image: the platform camera's own encoded-still bound."""
MAX_ANSWER_BYTES = 16 * 1024 * 1024
"""Decoded image bytes plus text bytes of one answer."""
MAX_IMAGE_SIDE = 16_384
MAX_PNG_RAW_BYTES = 256 * 1024 * 1024
"""The largest decompressed scanline data a PNG header may promise before it is refused unread."""

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: The only chunks an image may carry: every other one, a palette and every ancillary chunk included, holds bytes
#: nothing here reads, so it is refused by name rather than passed through unread.
PNG_CHUNKS = (b"IHDR", b"IDAT", b"IEND")
_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 4: (8, 16), 6: (8, 16)}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


class ContentRefusal(ValueError):
    """A content answer the gateway will not hand to its caller; the message names no value."""


def _png_passes(width: int, height: int, depth: int, colour: int, interlace: int) -> List[Tuple[int, int]]:
    """(rows, bytes per row with its filter byte) of each pass the header declares."""
    bits = _PNG_CHANNELS[colour] * depth
    shapes = []
    for x0, y0, dx, dy in ((0, 0, 1, 1),) if interlace == 0 else _ADAM7:
        columns, rows = max(0, -(-(width - x0) // dx)), max(0, -(-(height - y0) // dy))
        if columns and rows:
            shapes.append((rows, 1 + -(-(columns * bits) // 8)))
    return shapes


def png_size(data: bytes) -> Tuple[int, int]:
    """(width, height) of a PNG that is IHDR, IDAT chunks and an empty IEND in that order and nothing else, every CRC
    holding, whose image stream inflates to exactly the size the header declares with no byte after it, and whose
    every row names a real filter; or ContentRefusal naming what is not."""
    if not data.startswith(_PNG_SIGNATURE):
        raise ContentRefusal("is not a PNG (no PNG signature)")
    position, header, stream, kinds = len(_PNG_SIGNATURE), None, [], []
    while True:
        if position + 12 > len(data):
            raise ContentRefusal("is a truncated PNG (it ends before its IEND)")
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        end = position + 12 + length
        if end > len(data):
            raise ContentRefusal("is a truncated PNG (a chunk runs past the end)")
        body = data[position + 8:position + 8 + length]
        if struct.unpack(">I", data[position + 8 + length:end])[0] != zlib.crc32(kind + body) & 0xFFFFFFFF:
            raise ContentRefusal("is a corrupt PNG (a chunk fails its CRC)")
        if kind not in PNG_CHUNKS:
            raise ContentRefusal(f"is a PNG carrying a {kind.decode('latin-1')!r} chunk; only IHDR, IDAT and IEND are "
                                 "accepted, since any other chunk holds bytes nothing here reads")
        if (not kinds and kind != b"IHDR") or (kinds and kind == b"IHDR") or (kind == b"IEND" and b"IDAT" not in kinds):
            raise ContentRefusal("is not a whole PNG (its chunks are not IHDR, IDAT..., IEND in that order)")
        kinds.append(kind)
        if kind == b"IHDR":
            if length != 13:
                raise ContentRefusal("is not a whole PNG (its IHDR is not 13 bytes)")
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if colour == 3:
                raise ContentRefusal("is a paletted PNG; its palette holds bytes nothing here reads")
            if not (0 < width <= MAX_IMAGE_SIDE and 0 < height <= MAX_IMAGE_SIDE):
                raise ContentRefusal(f"is a PNG of {width}x{height}, outside 1..{MAX_IMAGE_SIDE} a side")
            if depth not in _PNG_DEPTHS.get(colour, ()) or compression or filtering or interlace > 1:
                raise ContentRefusal("is not a valid PNG (its IHDR names no valid colour type, depth or method)")
            header = (width, height, depth, colour, interlace)
        elif kind == b"IDAT":
            stream.append(body)
        else:
            if length:
                raise ContentRefusal("is not a whole PNG (its IEND is not empty)")
            position = end
            break
        position = end
    if position != len(data):
        raise ContentRefusal(f"is not a whole PNG ({len(data) - position} bytes after its IEND)")
    shapes = _png_passes(*header)
    expected = sum(rows * row for rows, row in shapes)
    if expected > MAX_PNG_RAW_BYTES:
        raise ContentRefusal(f"is a PNG whose pixels would decompress to {expected} bytes, over {MAX_PNG_RAW_BYTES}")
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(stream), expected + 1)
    except zlib.error:
        raise ContentRefusal("is a corrupt PNG (its pixel data does not decompress)") from None
    tail = len(inflater.unused_data) + len(inflater.unconsumed_tail)
    if len(raw) != expected or not inflater.eof or tail:
        raise ContentRefusal(f"is not a whole PNG (its pixel data is {len(raw)} bytes, its header promises "
                             f"{expected}, with {tail} bytes after the compressed stream)")
    cursor = 0
    for rows, row in shapes:
        if any(raw[cursor + index * row] > 4 for index in range(rows)):
            raise ContentRefusal("is a corrupt PNG (a scanline names no valid filter)")
        cursor += rows * row
    return header[0], header[1]


_DECODERS = {"image/png": png_size}
_TEXT_KEYS, _IMAGE_KEYS = {"type", "text"}, {"type", "data", "mimeType"}


def _shape(block: Any) -> str:
    if isinstance(block, dict):
        return f"a {len(block)}-key object" + (f" of type {block['type']!r}" if block.get("type") in ("text", "image") else "")
    return f"a {type(block).__name__}"


def validate(blocks: Any) -> List[Dict[str, Any]]:
    """The checked blocks, each image with its measured ``width`` and ``height``, or ContentRefusal."""
    if not isinstance(blocks, list) or not blocks:
        raise ContentRefusal(f"must be a non-empty list of content blocks, got {_shape(blocks)}")
    if len(blocks) > MAX_BLOCKS:
        raise ContentRefusal(f"has {len(blocks)} blocks, over {MAX_BLOCKS}")
    checked, total = [], 0
    for index, block in enumerate(blocks):
        where = f"[{index}]"
        if isinstance(block, dict) and set(block) == _TEXT_KEYS and block["type"] == "text" and isinstance(block["text"], str):
            if len(block["text"]) > MAX_TEXT_CHARS:
                raise ContentRefusal(f"{where} is a text block of {len(block['text'])} characters, over {MAX_TEXT_CHARS}")
            total += len(block["text"].encode("utf-8"))
            checked.append({"type": "text", "text": block["text"]})
        elif isinstance(block, dict) and set(block) == _IMAGE_KEYS and block["type"] == "image" \
                and isinstance(block["data"], str) and isinstance(block["mimeType"], str):
            if block["mimeType"] not in IMAGE_MIME_TYPES:
                raise ContentRefusal(f"{where} is an image block whose mimeType is not one of {', '.join(IMAGE_MIME_TYPES)}")
            if len(block["data"]) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise ContentRefusal(f"{where} is an image block of {len(block['data'])} base64 characters, over the "
                                     f"{MAX_IMAGE_BYTES}-byte image bound")
            try:
                image = base64.b64decode(block["data"], validate=True)
            except (binascii.Error, ValueError):
                raise ContentRefusal(f"{where} is an image block whose data is not base64") from None
            if len(image) > MAX_IMAGE_BYTES:
                raise ContentRefusal(f"{where} is an image of {len(image)} bytes, over {MAX_IMAGE_BYTES}")
            try:
                width, height = _DECODERS[block["mimeType"]](image)
            except ContentRefusal as refusal:
                raise ContentRefusal(f"{where} is an image block whose data {refusal}") from None
            total += len(image)
            checked.append({"type": "image", "data": block["data"], "mimeType": block["mimeType"],
                            "width": width, "height": height})
        else:
            raise ContentRefusal(f"{where} is {_shape(block)}, neither a text block {{type, text}} nor an image "
                                 f"block {{type, data, mimeType}}")
        if total > MAX_ANSWER_BYTES:
            raise ContentRefusal(f"is over {MAX_ANSWER_BYTES} bytes by block {index}")
    return checked
