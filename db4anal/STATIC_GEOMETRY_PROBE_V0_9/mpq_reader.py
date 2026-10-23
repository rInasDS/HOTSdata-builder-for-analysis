from __future__ import annotations

import bz2
import os
import struct
import zlib
from pathlib import Path

# Minimal read-only MPQ reader sufficient for HoTS/SC2 .s2ma map packages.
# Supports listfile lookup, single-unit files and sector-compressed zlib/bzip2 files.

_MPQ_FILE_IMPLODE = 0x00000100
_MPQ_FILE_COMPRESS = 0x00000200
_MPQ_FILE_ENCRYPTED = 0x00010000
_MPQ_FILE_FIX_KEY = 0x00020000
_MPQ_FILE_SINGLE_UNIT = 0x01000000
_MPQ_FILE_SECTOR_CRC = 0x04000000
_MPQ_FILE_EXISTS = 0x80000000


def _crypt_table() -> list[int]:
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        for j in range(5):
            index = i + (j * 0x100)
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp1 = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp2 = seed & 0xFFFF
            table[index] = (temp1 | temp2) & 0xFFFFFFFF
    return table


_CRYPT = _crypt_table()


def mpq_hash(text: str, hash_type: int) -> int:
    data = text.replace("/", "\\").upper().encode("utf-8")
    seed1 = 0x7FED7FED
    seed2 = 0xEEEEEEEE
    for ch in data:
        seed1 = (_CRYPT[(hash_type << 8) + ch] ^ ((seed1 + seed2) & 0xFFFFFFFF)) & 0xFFFFFFFF
        seed2 = (ch + seed1 + seed2 + ((seed2 << 5) & 0xFFFFFFFF) + 3) & 0xFFFFFFFF
    return seed1


def _decrypt(data: bytes, key: int) -> bytes:
    if len(data) % 4:
        raise ValueError("encrypted MPQ block length must be divisible by 4")
    values = list(struct.unpack(f"<{len(data) // 4}I", data))
    seed1 = key & 0xFFFFFFFF
    seed2 = 0xEEEEEEEE
    out: list[int] = []
    for value in values:
        seed2 = (seed2 + _CRYPT[0x400 + (seed1 & 0xFF)]) & 0xFFFFFFFF
        plain = (value ^ ((seed1 + seed2) & 0xFFFFFFFF)) & 0xFFFFFFFF
        out.append(plain)
        seed1 = ((((~seed1) & 0xFFFFFFFF) << 21) + 0x11111111 | (seed1 >> 11)) & 0xFFFFFFFF
        seed2 = (plain + seed2 + ((seed2 << 5) & 0xFFFFFFFF) + 3) & 0xFFFFFFFF
    return struct.pack(f"<{len(out)}I", *out)


class MPQArchive:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._fh = self.path.open("rb")
        prefix = self._fh.read(min(self.path.stat().st_size, 1 << 20))
        header_at = prefix.find(b"MPQ\x1a")
        if header_at < 0:
            raise ValueError(f"not an MPQ archive: {self.path}")
        self.base_offset = header_at
        self._fh.seek(header_at)
        header = self._fh.read(32)
        (
            magic,
            _header_size,
            _archive_size,
            self.format_version,
            block_shift,
            hash_offset,
            block_offset,
            hash_count,
            block_count,
        ) = struct.unpack("<4sIIHHIIII", header)
        if magic != b"MPQ\x1a":
            raise ValueError("invalid MPQ magic")
        self.sector_size = 512 << block_shift

        self._fh.seek(self.base_offset + hash_offset)
        hash_raw = self._fh.read(hash_count * 16)
        hash_raw = _decrypt(hash_raw, mpq_hash("(hash table)", 3))
        self.hash_table = [
            struct.unpack_from("<IIHHI", hash_raw, i * 16)
            for i in range(hash_count)
        ]

        self._fh.seek(self.base_offset + block_offset)
        block_raw = self._fh.read(block_count * 16)
        block_raw = _decrypt(block_raw, mpq_hash("(block table)", 3))
        self.block_table = [
            struct.unpack_from("<IIII", block_raw, i * 16)
            for i in range(block_count)
        ]

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "MPQArchive":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _lookup(self, name: str) -> int | None:
        start = mpq_hash(name, 0) % len(self.hash_table)
        hash_a = mpq_hash(name, 1)
        hash_b = mpq_hash(name, 2)
        for step in range(len(self.hash_table)):
            entry = self.hash_table[(start + step) % len(self.hash_table)]
            block_index = entry[4]
            if block_index == 0xFFFFFFFF:
                return None
            if block_index != 0xFFFFFFFE and entry[0] == hash_a and entry[1] == hash_b:
                return block_index
        return None

    @staticmethod
    def _decompress(chunk: bytes, expected_size: int) -> bytes:
        if len(chunk) == expected_size:
            return chunk
        if not chunk:
            return chunk
        mask = chunk[0]
        data = chunk[1:]
        if mask & 0x10:
            data = bz2.decompress(data)
        if mask & 0x02:
            data = zlib.decompress(data)
        unsupported = mask & ~(0x10 | 0x02)
        if unsupported:
            raise NotImplementedError(f"unsupported MPQ compression mask: 0x{mask:02x}")
        return data

    def read(self, name: str) -> bytes | None:
        block_index = self._lookup(name)
        if block_index is None or block_index >= len(self.block_table):
            return None
        offset, compressed_size, file_size, flags = self.block_table[block_index]
        if not (flags & _MPQ_FILE_EXISTS):
            return None
        self._fh.seek(self.base_offset + offset)
        payload = self._fh.read(compressed_size)

        encrypted = bool(flags & _MPQ_FILE_ENCRYPTED)
        single_unit = bool(flags & _MPQ_FILE_SINGLE_UNIT)
        compressed = bool(flags & _MPQ_FILE_COMPRESS)
        implode = bool(flags & _MPQ_FILE_IMPLODE)
        key = mpq_hash(os.path.basename(name), 3)
        if flags & _MPQ_FILE_FIX_KEY:
            key = ((key + offset) ^ file_size) & 0xFFFFFFFF

        if single_unit:
            if encrypted:
                payload = _decrypt(payload, key)
            if compressed:
                payload = self._decompress(payload, file_size)
            elif implode:
                raise NotImplementedError("PKWARE implode compression is not supported")
            return payload[:file_size]

        sector_count = (file_size + self.sector_size - 1) // self.sector_size
        table_count = sector_count + 1
        if flags & _MPQ_FILE_SECTOR_CRC:
            table_count += 1
        table_size = table_count * 4
        offset_table = payload[:table_size]
        if encrypted:
            offset_table = _decrypt(offset_table, (key - 1) & 0xFFFFFFFF)
        sector_offsets = list(struct.unpack(f"<{table_count}I", offset_table))

        out = bytearray()
        for sector_index in range(sector_count):
            start = sector_offsets[sector_index]
            end = sector_offsets[sector_index + 1]
            chunk = payload[start:end]
            if encrypted:
                chunk = _decrypt(chunk, (key + sector_index) & 0xFFFFFFFF)
            expected = min(self.sector_size, file_size - sector_index * self.sector_size)
            if compressed:
                chunk = self._decompress(chunk, expected)
            elif implode:
                raise NotImplementedError("PKWARE implode compression is not supported")
            out.extend(chunk[:expected])
        return bytes(out[:file_size])

    def list_files(self) -> list[str]:
        raw = self.read("(listfile)")
        if not raw:
            return []
        return [line.strip() for line in raw.decode("utf-8", "ignore").splitlines() if line.strip()]
