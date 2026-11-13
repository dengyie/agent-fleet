"""Streaming PNG integrity checks for the existing release-evidence gate."""
from __future__ import annotations

import struct
from typing import BinaryIO
import zlib


CHUNK_BYTES = 64 * 1024
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_CRITICAL = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8),
          (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _read_exact(source: BinaryIO, size: int) -> bytes:
    buffer = bytearray(size)
    offset = 0
    while offset < size:
        part = source.read(size - offset)
        if not part:
            raise ValueError("incomplete PNG chunk")
        buffer[offset:offset + len(part)] = part
        offset += len(part)
    return bytes(buffer)


def _check_crc(source: BinaryIO, expected: int) -> None:
    if int.from_bytes(_read_exact(source, 4), "big") != expected & 0xffffffff:
        raise ValueError("PNG checksum mismatch")


def _header_rows(header: bytes) -> tuple[int, int, tuple[tuple[int, int], ...]]:
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    if (not 1 <= width <= 0x7fffffff or not 1 <= height <= 0x7fffffff
            or color not in _DEPTHS or depth not in _DEPTHS[color]
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        raise ValueError("invalid PNG header")
    passes = _ADAM7 if interlace else ((0, 0, 1, 1),)
    layouts = [(0, 0)] * len(passes)
    count = 0
    for x_start, y_start, x_step, y_step in passes:
        columns = (width - x_start + x_step - 1) // x_step
        rows = (height - y_start + y_step - 1) // y_step
        if columns <= 0 or rows <= 0:
            continue
        layouts[count] = (1 + (columns * _CHANNELS[color] * depth + 7) // 8, rows)
        count += 1
    return color, depth, tuple(layouts[:count])


class _Pixels:
    """Parser state owned by one image; no decoded rows are retained."""

    def __init__(self, rows: tuple[tuple[int, int], ...]) -> None:
        self.decoder = zlib.decompressobj()
        self.rows = rows
        self.expected = sum(size * count for size, count in rows)
        self.decoded = 0
        self.next_filter = 0
        self.pass_index = 0
        self.pass_end = rows[0][0] * rows[0][1]

    def _check_rows(self, data: bytes) -> None:
        end = self.decoded + len(data)
        if end > self.expected:
            raise ValueError("PNG pixel data exceeds header")
        while self.next_filter < end:
            if data[self.next_filter - self.decoded] > 4:
                raise ValueError("invalid PNG row filter")
            self.next_filter += self.rows[self.pass_index][0]
            if self.next_filter == self.pass_end and self.pass_index + 1 < len(self.rows):
                self.pass_index += 1
                size, count = self.rows[self.pass_index]
                self.pass_end += size * count
        self.decoded = end

    def feed(self, data: bytes) -> None:
        pending = data
        try:
            while pending:
                decoded = self.decoder.decompress(pending, min(CHUNK_BYTES, self.expected - self.decoded + 1))
                if self.decoder.unused_data:
                    raise ValueError("trailing PNG compressed data")
                self._check_rows(decoded)
                tail = self.decoder.unconsumed_tail
                if not decoded and tail == pending:
                    raise ValueError("PNG decoder made no progress")
                pending = tail
        except zlib.error as exc:
            raise ValueError("invalid PNG compressed data") from exc

    def finish(self) -> None:
        if (not self.decoder.eof or self.decoder.unused_data or self.decoder.unconsumed_tail
                or self.decoded != self.expected or self.next_filter != self.expected):
            raise ValueError("incomplete PNG pixel data")


def _chunk_data(source: BinaryIO, length: int, kind: bytes, pixels: _Pixels | None) -> int:
    checksum = zlib.crc32(kind)
    remaining = length
    while remaining:
        data = source.read(min(remaining, CHUNK_BYTES))
        if not data:
            raise ValueError("incomplete PNG chunk")
        checksum = zlib.crc32(data, checksum)
        if pixels is not None:
            pixels.feed(data)
        remaining -= len(data)
    return checksum


def verify_png(source: BinaryIO) -> None:
    """Check structure, CRCs and complete bounded deflate output; leave ownership to the caller."""
    if _read_exact(source, 8) != _SIGNATURE:
        raise ValueError("invalid PNG signature")
    length, kind = struct.unpack(">I4s", _read_exact(source, 8))
    if length != 13 or kind != b"IHDR":
        raise ValueError("missing PNG header")
    header = _read_exact(source, 13)
    _check_crc(source, zlib.crc32(header, zlib.crc32(kind)))
    color, depth, rows = _header_rows(header)
    pixels = _Pixels(rows)
    seen_palette = False
    seen_data = False
    data_closed = False
    while True:
        length, kind = struct.unpack(">I4s", _read_exact(source, 8))
        if (length > 0x7fffffff or kind[2] & 32
                or not all(65 <= letter <= 90 or 97 <= letter <= 122 for letter in kind)):
            raise ValueError("invalid PNG chunk header")
        if kind != b"IDAT" and seen_data:
            data_closed = True
        if kind == b"IHDR":
            raise ValueError("duplicate PNG header")
        if kind == b"PLTE":
            if (seen_palette or seen_data or color in (0, 4) or not 0 < length <= 768
                    or length % 3 or (color == 3 and length // 3 > 1 << depth)):
                raise ValueError("invalid PNG palette")
            seen_palette = True
        if kind == b"IDAT":
            if data_closed or (color == 3 and not seen_palette):
                raise ValueError("invalid PNG image data order")
            seen_data = True
        if kind not in _CRITICAL and not kind[0] & 32:
            raise ValueError("unknown PNG critical chunk")
        if kind == b"IEND" and (length or not seen_data):
            raise ValueError("invalid PNG end chunk")
        checksum = _chunk_data(source, length, kind, pixels if kind == b"IDAT" else None)
        _check_crc(source, checksum)
        if kind != b"IEND":
            continue
        pixels.finish()
        if source.read(1):
            raise ValueError("data follows PNG end chunk")
        return
