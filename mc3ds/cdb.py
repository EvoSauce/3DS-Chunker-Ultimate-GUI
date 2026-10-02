"""
Canonical MC3DS chunk database (``db/cdb``) format.

This module is the single definition of the MC3DS chunk storage format used by
both conversion directions. It follows the reference inserter in
``refs/3dsleveledit.py``; where the old reader disagreed, the reference wins.

World layout::

    <world>/level.dat           8 byte Bedrock-style header + little endian NBT
    <world>/db/vdb/slt*.vdb     misc. named data (untouched by the converters)
    <world>/db/cdb/index.cdb    chunk index
    <world>/db/cdb/newindex.cdb chunk index, kept identical to index.cdb
    <world>/db/cdb/slt<N>.cdb   "slot" files, 128 fixed size subfiles each

Slot file::

    0x00  u16 ?, u16 ?, u32 subfileCount, u32 dataOffset (0x14),
          u32 subfileSize (0x2800), u32 type (0x4 for CDB)
    dataOffset + n * subfileSize: subfile n

Chunk subfile (offsets relative to the start of the subfile)::

    0x00  u32 magic (0xABCDEF98, some worlds use 0xABCDEF99; 0 = unused subfile)
    0x04  u32 packed position, u8 param0, u8 param1, u16 unk0, u16 unk1, u16 unk2
    0x10  6 section descriptors: i32 index, i32 offset, i32 compressedSize,
          i32 decompressedSize (all -1/-1/0/0 when unused)
    0x70  zlib compressed section data, back to back, zero padded to subfileSize

Terrain section (section 0), decompressed::

    u8 subchunkCount
    subchunkCount * (u8 version, 4096 block ids, 2048 data nibbles,
                     2048 sky light nibbles, 2048 block light nibbles)
    512 byte heightmap, 256 byte biomes
"""

import io
import struct
import zlib
from collections import defaultdict
from pathlib import Path

MAGIC_CDB = 0xABCDEF98
ALT_MAGIC_CDB = 0xABCDEF99

SUBFILE_COUNT = 0x80
SUBFILE_SIZE = 0x2800
FILE_HEADER_FORMAT = "<HHIIII"
FILE_HEADER_SIZE = struct.calcsize(FILE_HEADER_FORMAT)

INDEX_HEADER_FORMAT = "<6I"
INDEX_HEADER_SIZE = struct.calcsize(INDEX_HEADER_FORMAT)
INDEX_ENTRY_FORMAT = "<I H H H H B B H"
INDEX_ENTRY_SIZE = struct.calcsize(INDEX_ENTRY_FORMAT)
INDEX_CONSTANT0 = 2
INDEX_CONSTANT1_OBSERVED = 0x440
INDEX_ENTRY_CONSTANT0 = 0x20FF
INDEX_ENTRY_CONSTANT1 = 0x000A
INDEX_ENTRY_CONSTANT2 = 0x8000

CHUNK_FIXED_FORMAT = "<I BB HHH"
CHUNK_UNKNOWN0 = 0
CHUNK_UNKNOWN1 = 3
CHUNK_UNKNOWN2 = 0
SECTION_DESCRIPTOR_FORMAT = "<iiii"
SECTION_COUNT = 6
EMPTY_SECTION = (-1, -1, 0, 0)
# magic + fixed header + descriptors; section offsets are measured from the
# start of the subfile, so the first section always starts at 0x70
CHUNK_HEADER_SIZE = (
    4
    + struct.calcsize(CHUNK_FIXED_FORMAT)
    + SECTION_COUNT * struct.calcsize(SECTION_DESCRIPTOR_FORMAT)
)

SECTION_TERRAIN = 0
SECTION_BLOCK_ENTITIES = 1
SECTION_ENTITIES = 2
SECTION_TICKS = 3

WORLD_HEIGHT = 128
MAX_SUBCHUNKS = WORLD_HEIGHT // 16
SUBCHUNK_VERSION = 0
BLOCKS_SIZE = 0x1000
NIBBLES_SIZE = 0x800
SUBCHUNK_PAYLOAD_SIZE = BLOCKS_SIZE + 3 * NIBBLES_SIZE
# each subchunk is prefixed with a one byte version
SUBCHUNK_STRIDE = 1 + SUBCHUNK_PAYLOAD_SIZE
HEIGHTMAP_SIZE = 0x200
BIOMES_SIZE = 0x100

# Chunk coordinates are stored as 14 bit offset-binary values biased by 0x2000,
# so chunk 0 is stored as 0x2000 and the usable range is -0x2000..0x1FFF.
POSITION_BIAS = 0x2000
POSITION_MASK = 0x3FFF
MIN_CHUNK_COORDINATE = -POSITION_BIAS
MAX_CHUNK_COORDINATE = POSITION_MASK - POSITION_BIAS


def pack_position(chunk_x: int, chunk_z: int, dimension: int) -> int:
    if not (
        MIN_CHUNK_COORDINATE <= chunk_x <= MAX_CHUNK_COORDINATE
        and MIN_CHUNK_COORDINATE <= chunk_z <= MAX_CHUNK_COORDINATE
    ):
        raise ValueError(
            f"chunk ({chunk_x:d}, {chunk_z:d}) is outside the MC3DS coordinate range "
            f"{MIN_CHUNK_COORDINATE:d}..{MAX_CHUNK_COORDINATE:d}"
        )
    stored_x = (chunk_x + POSITION_BIAS) & POSITION_MASK
    stored_z = (chunk_z + POSITION_BIAS) & POSITION_MASK
    return ((dimension & 0xF) << 28) | (stored_z << 14) | stored_x


def unpack_position(packed: int) -> tuple[int, int, int]:
    chunk_x = (packed & POSITION_MASK) - POSITION_BIAS
    chunk_z = ((packed >> 14) & POSITION_MASK) - POSITION_BIAS
    dimension = (packed >> 28) & 0xF
    return chunk_x, chunk_z, dimension


def unpack_position_fields(stored_x: int, stored_z: int, dimension: int):
    return stored_x - POSITION_BIAS, stored_z - POSITION_BIAS, dimension


def block_index(x: int, y: int, z: int) -> int:
    "index of a block inside a subchunk (x major, then z, then y)"
    return x * 0x100 + z * 0x10 + y


# -------------------------
# Terrain section
# -------------------------
def build_terrain_section(subchunks: list[tuple[bytes, bytes]]) -> tuple[bytes, int]:
    """
    Builds the compressed terrain section from ``(block ids, data nibbles)``
    pairs. Light, the heightmap and biomes are written as zeroes, exactly like
    the reference converter; the game recalculates light and heightmaps.
    """
    raw = bytearray()
    raw.append(len(subchunks) & 0xFF)
    zero_nibbles = bytes(NIBBLES_SIZE)
    for blocks, data in subchunks:
        assert len(blocks) == BLOCKS_SIZE and len(data) == NIBBLES_SIZE
        raw.append(SUBCHUNK_VERSION)
        raw += blocks
        raw += data
        raw += zero_nibbles  # sky light
        raw += zero_nibbles  # block light
    raw += bytes(HEIGHTMAP_SIZE)
    raw += bytes(BIOMES_SIZE)
    return zlib.compress(bytes(raw)), len(raw)


def parse_terrain_section(raw: bytes) -> tuple[tuple[bytes, ...], bytes, bytes]:
    """
    Splits a decompressed terrain section into the per-subchunk payloads
    (without their version byte), the heightmap, and the biomes.
    """
    count = raw[0]
    if count > MAX_SUBCHUNKS:
        raise ValueError(f"terrain has {count:d} subchunks, maximum is {MAX_SUBCHUNKS:d}")
    subchunks = []
    for i in range(count):
        start = 1 + i * SUBCHUNK_STRIDE
        payload = raw[start + 1 : start + SUBCHUNK_STRIDE]
        if len(payload) != SUBCHUNK_PAYLOAD_SIZE:
            raise ValueError("terrain section is truncated")
        subchunks.append(payload)
    trailer = raw[1 + count * SUBCHUNK_STRIDE :]
    if len(trailer) < BIOMES_SIZE:
        raise ValueError("terrain section is missing biomes")
    return tuple(subchunks), trailer[:-BIOMES_SIZE], trailer[-BIOMES_SIZE:]


# -------------------------
# Chunk subfile
# -------------------------
def build_chunk_blob(
    packed_position: int,
    terrain_compressed: bytes,
    terrain_raw_size: int,
    block_entities_compressed: bytes | None,
    block_entities_raw_size: int,
    page_magic: int,
    entities_compressed: bytes | None = None,
    entities_raw_size: int = 0,
) -> bytes:
    offset = CHUNK_HEADER_SIZE
    fixed = struct.pack(
        CHUNK_FIXED_FORMAT,
        packed_position,
        1,
        0,
        CHUNK_UNKNOWN0,
        CHUNK_UNKNOWN1,
        CHUNK_UNKNOWN2,
    )

    sections = [(SECTION_TERRAIN, offset, len(terrain_compressed), terrain_raw_size)]
    offset += len(terrain_compressed)
    if block_entities_compressed:
        sections.append(
            (
                SECTION_BLOCK_ENTITIES,
                offset,
                len(block_entities_compressed),
                block_entities_raw_size,
            )
        )
        offset += len(block_entities_compressed)
    else:
        sections.append(EMPTY_SECTION)
    if entities_compressed:
        sections.append(
            (SECTION_ENTITIES, offset, len(entities_compressed), entities_raw_size)
        )
        offset += len(entities_compressed)
    else:
        sections.append(EMPTY_SECTION)
    while len(sections) < SECTION_COUNT:
        sections.append(EMPTY_SECTION)

    out = io.BytesIO()
    out.write(struct.pack("<I", page_magic))
    out.write(fixed)
    for section in sections:
        out.write(struct.pack(SECTION_DESCRIPTOR_FORMAT, *section))
    out.write(terrain_compressed)
    if block_entities_compressed:
        out.write(block_entities_compressed)
    if entities_compressed:
        out.write(entities_compressed)
    return out.getvalue()


# -------------------------
# Little endian NBT writer (block entities)
# -------------------------
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10

# MC3DS CompoundTag getters are type-strict (dump: getInt @ 0x6a1038 requires
# TAG_INT, getShort @ 0x6a11e0 TAG_SHORT, getFloat @ 0x6a1148 TAG_FLOAT,
# getByte @ 0x6a1080 / 0x6a0a4c TAG_BYTE). Inferring the smallest integer tag
# breaks BlockEntity::create which reads x/y/z via getInt @ 0x172bec+.
_FORCE_INT = frozenset(
    {
        "x",
        "y",
        "z",
        "pairx",
        "pairz",
        "TransferCooldown",
        "SuccessCount",
        "OutputSignal",
        "Delay",
        "MinSpawnDelay",
        "MaxSpawnDelay",
        "SpawnCount",
        "MaxNearbyEntities",
        "RequiredPlayerRange",
        "SpawnRange",
        "Levels",
        "Primary",
        "Secondary",
        "Base",
        "facing",
        "Facing",
    }
)
_FORCE_SHORT = frozenset(
    {
        "Damage",
        "HurtTime",
        "Fire",
        "Air",
        "BurnTime",
        "CookTime",
        "BrewTime",
        "ItemRotation",
        "Rot",
        "SkullType",
        "mData",
    }
)
_FORCE_BYTE = frozenset(
    {
        "Count",
        "Slot",
        "isMovable",
        "State",
        "NewState",
        "Sticky",
        "TrackOutput",
        "auto",
        "powered",
        "Findable",
        "note",
    }
)
_FORCE_FLOAT = frozenset(
    {
        "Progress",
        "LastProgress",
        "ItemDropChance",
        "fallDistance",
    }
)


def _write_string(buffer, value: str) -> None:
    encoded = value.encode("utf-8")
    buffer.write(struct.pack("<H", len(encoded) & 0xFFFF))
    buffer.write(encoded)


def _infer_tag_type(value) -> int:
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, int):
        if -128 <= value <= 127:
            return TAG_BYTE
        if -32768 <= value <= 32767:
            return TAG_SHORT
        if -2147483648 <= value <= 2147483647:
            return TAG_INT
        return TAG_LONG
    if isinstance(value, float):
        return TAG_FLOAT
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, (bytes, bytearray)):
        return TAG_BYTE_ARRAY
    if isinstance(value, (list, tuple)):
        return TAG_LIST
    if isinstance(value, dict):
        return TAG_COMPOUND
    return TAG_STRING


def _fit_signed(value, bits: int) -> int:
    """Wrap any int into a signed bits-wide two's-complement value for struct.pack."""
    mask = (1 << bits) - 1
    v = int(value) & mask
    sign = 1 << (bits - 1)
    return v - (1 << bits) if v >= sign else v


def _tag_type_for_key(key: str, value) -> int:
    if key in _FORCE_FLOAT and isinstance(value, (int, float)):
        return TAG_FLOAT
    if isinstance(value, bool) or key in _FORCE_BYTE:
        if isinstance(value, (int, bool)):
            return TAG_BYTE
    if key in _FORCE_SHORT and isinstance(value, int):
        return TAG_SHORT
    if key in _FORCE_INT and isinstance(value, int):
        return TAG_INT
    if key in ("UniqueID", "UUIDMost", "UUIDLeast") and isinstance(value, int):
        return TAG_LONG
    if key == "id" and isinstance(value, int):
        return TAG_SHORT if -32768 <= value <= 32767 else TAG_INT
    if key == "id" and isinstance(value, str):
        return TAG_STRING
    return _infer_tag_type(value)


def _write_payload(buffer, tag_type: int, value) -> None:
    if tag_type == TAG_BYTE:
        buffer.write(struct.pack("<b", _fit_signed(value, 8)))
    elif tag_type == TAG_SHORT:
        buffer.write(struct.pack("<h", _fit_signed(value, 16)))
    elif tag_type == TAG_INT:
        buffer.write(struct.pack("<i", _fit_signed(value, 32)))
    elif tag_type == TAG_LONG:
        buffer.write(struct.pack("<q", _fit_signed(value, 64)))
    elif tag_type == TAG_FLOAT:
        buffer.write(struct.pack("<f", float(value)))
    elif tag_type == TAG_DOUBLE:
        buffer.write(struct.pack("<d", float(value)))
    elif tag_type == TAG_STRING:
        _write_string(buffer, str(value))
    elif tag_type == TAG_BYTE_ARRAY:
        data = bytes(value)
        buffer.write(struct.pack("<i", len(data)))
        buffer.write(data)
    elif tag_type == TAG_LIST:
        items = list(value)
        if not items:
            subtype = TAG_END
        elif isinstance(items[0], dict):
            subtype = TAG_COMPOUND
        elif isinstance(items[0], float) and not isinstance(items[0], bool):
            subtype = TAG_FLOAT
        elif isinstance(items[0], bool):
            subtype = TAG_BYTE
        elif isinstance(items[0], int) and not isinstance(items[0], bool):
            subtype = TAG_INT
            for item in items:
                if not (-2147483648 <= int(item) <= 2147483647):
                    subtype = TAG_LONG
                    break
        else:
            subtype = _infer_tag_type(items[0])
        buffer.write(struct.pack("<B", subtype))
        buffer.write(struct.pack("<i", len(items)))
        for item in items:
            _write_payload(buffer, subtype, item)
    elif tag_type == TAG_COMPOUND:
        for key, item in dict(value).items():
            if isinstance(item, int) and not isinstance(item, bool):
                # Unsigned 64-bit / oversized Python ints → signed long range
                if item.bit_length() > 63:
                    item = _fit_signed(item, 64)
            item_type = _tag_type_for_key(str(key), item)
            buffer.write(struct.pack("<B", item_type))
            _write_string(buffer, str(key))
            _write_payload(buffer, item_type, item)
        buffer.write(struct.pack("<B", TAG_END))
    else:
        _write_string(buffer, str(value))


def write_root_compound(compound: dict) -> bytes:
    buffer = io.BytesIO()
    buffer.write(struct.pack("<B", TAG_COMPOUND))
    _write_string(buffer, "")
    _write_payload(buffer, TAG_COMPOUND, compound)
    return buffer.getvalue()


def write_nbt_stream(compounds: list[dict]) -> bytes:
    "block entities are stored as root compounds written back to back"
    return b"".join(write_root_compound(compound) for compound in compounds)


_SCALAR_FORMATS = {
    TAG_BYTE: "<b",
    TAG_SHORT: "<h",
    TAG_INT: "<i",
    TAG_LONG: "<q",
    TAG_FLOAT: "<f",
    TAG_DOUBLE: "<d",
}


def _read_payload(stream, tag_type: int):
    if tag_type in _SCALAR_FORMATS:
        fmt = _SCALAR_FORMATS[tag_type]
        return struct.unpack(fmt, stream.read(struct.calcsize(fmt)))[0]
    if tag_type == TAG_BYTE_ARRAY:
        (length,) = struct.unpack("<i", stream.read(4))
        return stream.read(length)
    if tag_type == TAG_STRING:
        (length,) = struct.unpack("<H", stream.read(2))
        return stream.read(length).decode("utf-8")
    if tag_type == TAG_LIST:
        subtype, length = struct.unpack("<Bi", stream.read(5))
        return [_read_payload(stream, subtype) for _ in range(length)]
    if tag_type == TAG_COMPOUND:
        result = {}
        while True:
            (item_type,) = struct.unpack("<B", stream.read(1))
            if item_type == TAG_END:
                return result
            name = _read_payload(stream, TAG_STRING)
            result[name] = _read_payload(stream, item_type)
    raise ValueError(f"unsupported NBT tag type {tag_type:d}")


def read_nbt_stream(raw: bytes) -> list[dict]:
    stream = io.BytesIO(raw)
    compounds = []
    while stream.tell() < len(raw):
        (tag_type,) = struct.unpack("<B", stream.read(1))
        if tag_type != TAG_COMPOUND:
            raise ValueError("block entity stream must contain root compounds")
        _read_payload(stream, TAG_STRING)
        compounds.append(_read_payload(stream, TAG_COMPOUND))
    return compounds


# -------------------------
# Slot files
# -------------------------
def read_slot_header(stream) -> tuple[int, int, int, int, int, int]:
    raw = stream.read(FILE_HEADER_SIZE)
    if len(raw) != FILE_HEADER_SIZE:
        raise ValueError("bad slt*.cdb header")
    return struct.unpack(FILE_HEADER_FORMAT, raw)


def detect_page_magic(stream, data_offset: int) -> int:
    "new chunks reuse whichever chunk magic the slot's first subfile uses"
    stream.seek(data_offset)
    raw = stream.read(4)
    if len(raw) == 4:
        magic = struct.unpack("<I", raw)[0]
        if magic in (MAGIC_CDB, ALT_MAGIC_CDB):
            return magic
    return MAGIC_CDB


def slot_path(cdb_directory: Path, slot: int) -> Path:
    return cdb_directory / f"slt{slot:d}.cdb"


def slot_number(path: Path) -> int:
    digits = "".join(c if c.isdigit() else " " for c in path.name).split()
    return int(digits[0]) if digits else 0


def list_slot_files(cdb_directory: Path) -> list[Path]:
    return sorted(cdb_directory.glob("slt*.cdb"), key=slot_number)


def write_chunk_to_slot(cdb_directory: Path, slot: int, subfile: int, blob_parts) -> None:
    """
    Writes a chunk into a subfile. ``blob_parts`` is a callable receiving the
    page magic of the slot and returning the chunk bytes. The rest of the
    subfile is zero filled so no stale data from a previous chunk survives.
    """
    with open(slot_path(cdb_directory, slot), "r+b") as stream:
        _, _, _, data_offset, subfile_size, _ = read_slot_header(stream)
        page_magic = detect_page_magic(stream, data_offset)
        blob = blob_parts(page_magic)
        if len(blob) > subfile_size:
            raise ValueError(
                f"chunk data is too large ({len(blob):d} > {subfile_size:d} bytes)"
            )
        stream.seek(data_offset + subfile * subfile_size)
        stream.write(blob)
        stream.write(bytes(subfile_size - len(blob)))


# -------------------------
# Index files
# -------------------------
class IndexFile:
    """
    ``index.cdb``/``newindex.cdb``. Entries are kept sorted by packed position
    and ``pointers`` is the sorted list of slot numbers that contain chunks.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.constant0 = INDEX_CONSTANT0
        self.chunk_capacity = 0
        self.entry_size = INDEX_ENTRY_SIZE
        self.constant1 = INDEX_CONSTANT1_OBSERVED
        self.pointers: list[int] = []
        self.entries: list[bytes] = []
        self._positions: dict[int, list[int]] = defaultdict(list)
        self._dirty = False

    def load(self) -> "IndexFile":
        with open(self.path, "rb") as stream:
            header = stream.read(INDEX_HEADER_SIZE)
            if len(header) != INDEX_HEADER_SIZE:
                raise ValueError(f"index header too small: {self.path}")
            (
                self.constant0,
                entry_count,
                self.chunk_capacity,
                self.entry_size,
                pointer_count,
                self.constant1,
            ) = struct.unpack(INDEX_HEADER_FORMAT, header)
            raw_pointers = stream.read(pointer_count * 4)
            if len(raw_pointers) != pointer_count * 4:
                raise ValueError(f"index pointers are truncated: {self.path}")
            self.pointers = list(struct.unpack(f"<{pointer_count:d}I", raw_pointers))
            self.entries = []
            for _ in range(entry_count):
                raw = stream.read(self.entry_size)
                if len(raw) != self.entry_size:
                    break
                self.entries.append(raw[:INDEX_ENTRY_SIZE].ljust(INDEX_ENTRY_SIZE, b"\0"))
        self._positions = defaultdict(list)
        for i, raw in enumerate(self.entries):
            self._positions[struct.unpack_from("<I", raw)[0]].append(i)
        return self

    def parsed_entries(self):
        for raw in self.entries:
            yield struct.unpack(INDEX_ENTRY_FORMAT, raw)

    def build_map(self) -> dict[int, tuple[int, int, int, int]]:
        result = {}
        for position, slot, subfile, _, _, param0, param1, _ in self.parsed_entries():
            result[position] = (slot, subfile, param0, param1)
        return result

    def update_entry(
        self, packed_position: int, slot: int, subfile: int, param0: int = 1, param1: int = 0
    ) -> list[tuple[int, int]]:
        "returns the (slot, subfile) pairs of the entries that were replaced"
        raw = struct.pack(
            INDEX_ENTRY_FORMAT,
            packed_position & 0xFFFFFFFF,
            slot & 0xFFFF,
            subfile & 0xFFFF,
            INDEX_ENTRY_CONSTANT0,
            INDEX_ENTRY_CONSTANT1,
            param0 & 0xFF,
            param1 & 0xFF,
            INDEX_ENTRY_CONSTANT2,
        )
        replaced = []
        existing = self._positions.get(packed_position)
        if existing:
            for i in existing:
                replaced.append(struct.unpack_from("<HH", self.entries[i], 4))
                self.entries[i] = raw
        else:
            self._positions[packed_position].append(len(self.entries))
            self.entries.append(raw)
        self._dirty = True

        if slot not in self.pointers:
            self.pointers = sorted(set(self.pointers + [slot]))
        return replaced

    def write(self) -> None:
        if self._dirty:
            # stable sort, so duplicate positions keep their relative order
            self.entries.sort(key=lambda raw: struct.unpack_from("<I", raw)[0])
            self._positions = defaultdict(list)
            for i, raw in enumerate(self.entries):
                self._positions[struct.unpack_from("<I", raw)[0]].append(i)
            self._dirty = False
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(temporary, "wb") as stream:
            stream.write(
                struct.pack(
                    INDEX_HEADER_FORMAT,
                    INDEX_CONSTANT0,
                    len(self.entries),
                    self.chunk_capacity,
                    INDEX_ENTRY_SIZE,
                    len(self.pointers),
                    self.constant1,
                )
            )
            if self.pointers:
                stream.write(struct.pack(f"<{len(self.pointers):d}I", *self.pointers))
            for raw in self.entries:
                stream.write(raw)
        temporary.replace(self.path)


def load_indexes(cdb_directory: Path) -> list[IndexFile]:
    """
    Loads ``index.cdb`` then ``newindex.cdb``. The game keeps both files, and
    every chunk is recorded in both of them; when they disagree, the later
    file (``newindex.cdb``) wins.
    """
    indexes = []
    for name in ("index.cdb", "newindex.cdb"):
        path = cdb_directory / name
        if path.exists():
            indexes.append(IndexFile(path).load())
    if not indexes:
        raise FileNotFoundError(f"no index.cdb or newindex.cdb in {cdb_directory}")
    return indexes


def merged_index_map(indexes: list[IndexFile]) -> dict[int, tuple[int, int, int, int]]:
    merged = {}
    for index in indexes:
        merged.update(index.build_map())
    return merged


class SlotAllocator:
    """
    Finds where new chunks go: the first unused subfile of the lowest slot that
    has one, otherwise a new slot numbered one past the highest used slot. A
    new slot file is a byte-for-byte copy of the lowest numbered slot file as
    it was before the conversion started, which gives it a valid header; its
    old subfiles are unreferenced and each one is fully overwritten when used.
    """

    def __init__(self, indexes: list[IndexFile], cdb_directory: Path, template: bytes) -> None:
        self._cdb_directory = cdb_directory
        self._template = template
        self._references: dict[tuple[int, int], int] = defaultdict(int)
        self._used: dict[int, set[int]] = defaultdict(set)
        for index in indexes:
            for _, slot, subfile, *_ in index.parsed_entries():
                self._add(slot, subfile)

    def _add(self, slot: int, subfile: int) -> None:
        self._references[(slot, subfile)] += 1
        self._used[slot].add(subfile)

    def _remove(self, slot: int, subfile: int) -> None:
        self._references[(slot, subfile)] -= 1
        if self._references[(slot, subfile)] <= 0:
            del self._references[(slot, subfile)]
            self._used[slot].discard(subfile)
            if not self._used[slot]:
                del self._used[slot]

    def record(self, slot: int, subfile: int, replaced: list[tuple[int, int]]) -> None:
        "tracks an index update so later allocations see it"
        for old in replaced:
            self._remove(*old)
        self._add(slot, subfile)

    def allocate(self) -> tuple[int, int, bool]:
        "returns (slot, subfile, created a new slot file)"
        for slot in sorted(self._used):
            used = self._used[slot]
            if len(used.intersection(range(SUBFILE_COUNT))) >= SUBFILE_COUNT:
                continue
            for subfile in range(SUBFILE_COUNT):
                if subfile not in used:
                    return slot, subfile, False
        new_slot = max(self._used) + 1 if self._used else 0
        path = slot_path(self._cdb_directory, new_slot)
        created = False
        if not path.exists():
            with open(path, "wb") as stream:
                stream.write(self._template)
            created = True
        return new_slot, 0, created
