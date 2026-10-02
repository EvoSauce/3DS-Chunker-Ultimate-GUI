import os
from abc import abstractmethod
from io import BytesIO
from typing import Any, Iterable
from pathlib import Path
import zlib
import re

from . import cdb
from .nbt import NBT
from .parser import parser

# payload of one subchunk inside the terrain section, without its version byte
SUBCHUNK_SIZE = cdb.SUBCHUNK_PAYLOAD_SIZE


def process_key(key: int, length: int | None = None) -> int:
    if length is not None and key > length - 1:
        raise IndexError("index out of range")
    if key < 0:
        if length is None:
            raise IndexError("cannot use negative indexes with unknown length")
        else:
            key = length + key
            # if it's still negative, it's out of range
            if key < 0:
                raise IndexError("index out of range")
    return key


def parse_position(position) -> tuple[int, int, int]:
    x, z, dimension = cdb.unpack_position_fields(
        int(position.x), int(position.z), int(position.dimension)
    )
    assert 0 <= dimension <= 2, f"invalid dimension {dimension:d}"
    return (x, z, dimension)


class BaseParser:
    def __init__(self, stream: BytesIO) -> None:
        self._stream = stream
        self._offset = stream.tell()
        self._reload_data()

    def _reload_data(self) -> None:
        pass

    def _seek(self, position: int) -> None:
        self._stream.seek(self._offset + position)


class Index(BaseParser):
    def _reload_data(self) -> None:
        self._seek(0)
        self._data = parser.Index(self._stream)
        assert self._data.constant0 == 0x2
        # assert self._data.constant1 == 0x80

    @property
    def pointers(self):
        return self._data.pointers

    @property
    def entries(self):
        return self._data.entries


class Subfile(BaseParser):
    def __init__(self, stream: BytesIO, subfile_size: int) -> None:
        self._size = subfile_size
        super().__init__(stream)

    # it's a property so it's read only
    @property
    def size(self) -> int:
        return self._size

    def __len__(self) -> int:
        return self.size

    def _reload_data(self) -> None:
        self._seek(0)
        self._header = parser.SubfileHeader(self._stream)

    @property
    def filler(self) -> bool:
        # some subfiles have a header of all zeroes, and this avoids an error when parsing
        return self._header.magic == 0

    @property
    def raw(self) -> bytes | None:
        if self.filler:
            return None
        self._seek(len(self._header))
        content = self._stream.read(self.size - self._header.size)
        return content

    @property
    def raw_with_header(self) -> bytes | None:
        if self.filler:
            return None
        self._seek(0)
        content = self._stream.read(self.size)
        return content


class IterDB:
    def __init__(self, db) -> None:
        self._db = db
        self.__index = 0

    def __next__(self) -> tuple[int, Any]:
        while True:
            if self.__index > len(self._db) - 1:
                raise StopIteration
            new_subfile = self._db[self.__index]
            self.__index += 1
            if not new_subfile.filler:
                break
        return self.__index - 1, new_subfile


class DBFile(BaseParser):
    def _reload_data(self) -> None:
        self._header = parser.FileHeader(self._stream)

    @property
    def something(self) -> tuple[int]:
        return (self._header.something0, self._header.something1)

    @property
    def unknown(self) -> tuple[int]:
        return (self._header.unknown0, self._header.unknown1)

    @property
    def subfile_count(self) -> int:
        return self._header.subfileCount

    @property
    def subfile_size(self) -> int:
        return self._header.subfileSize

    def __len__(self) -> int:
        return self.subfile_count

    def _parse(self, subfile: Subfile) -> Any:
        return subfile

    def __iter__(self) -> IterDB:
        return IterDB(self)

    @property
    def data_offset(self) -> int:
        return self._header.dataOffset

    def __getitem__(self, key: int) -> bytes | None:
        key = process_key(key, self.subfile_count)
        self._seek(self.data_offset + self.subfile_size * key)
        subfile = Subfile(self._stream, self.subfile_size)
        return self._parse(subfile)


class Subchunk:
    def __init__(self, header, compressed: bytes) -> None:
        self._header = header
        self._compressed = compressed
        self.__header_cache = None

    @property
    def compressed(self) -> bytes:
        return self._compressed

    @property
    def raw_decompressed(self) -> bytes:
        decompress_object = zlib.decompressobj()
        decompressed = decompress_object.decompress(self._compressed)
        compressed_size = len(self._compressed) - len(decompress_object.unused_data)
        assert compressed_size == self._header.compressedSize, (
            f"compressed size {compressed_size:d} "
            f"is not expected size {self._header.compressedSize:d}"
        )
        assert len(decompressed) == self._header.decompressedSize, (
            f"decompressed size {len(decompressed):d} "
            f"is not expected size {self._header.decompressedSize:d}"
        )
        return decompressed

    @property
    def data(self) -> tuple[tuple[bytes, ...], bytes, tuple[int]]:
        """
        Only valid for the terrain section: returns the subchunk payloads
        (block ids, data, sky light, block light), the heightmap and biomes.
        """
        # https://minecraft.wiki/w/Bedrock_Edition_level_format/History
        block_split, heightmap, biomes = cdb.parse_terrain_section(self.raw_decompressed)
        assert len(block_split) == self._data_header.subchunks
        return block_split, heightmap, tuple(biomes)

    @property
    def _data_header(self):
        if self.__header_cache is None:
            self.__header_cache = parser.TerrainHeader(self.raw_decompressed)
        return self.__header_cache

    @property
    def size(self) -> int:
        return self._data_header.subchunks


class IterChunk:
    def __init__(self, chunk) -> None:
        self._chunk = chunk
        self.__index = 0

    def __next__(self) -> tuple[int, Subchunk]:
        while True:
            if self.__index > len(self._chunk) - 1:
                raise StopIteration
            new_subchunk = self._chunk[self.__index]
            self.__index += 1
            if new_subchunk is not None:
                break
        return self.__index - 1, new_subchunk


class Chunk:
    def __init__(self, subfile: Subfile) -> None:
        self.__section = 0
        self._subfile = subfile
        self._reload_data()

    def _reload_data(self) -> None:
        self._raw = self._subfile.raw
        self._header = parser.ChunkHeader(self._raw)

    @property
    def position(self) -> tuple[int, int, int]:
        return parse_position(self._header.position)

    @property
    def unknown_parameter_0(self) -> int:
        return self._header.parameters.unknown0

    @property
    def unknown_parameter_1(self) -> int:
        return self._header.parameters.unknown1

    @property
    def unknown0(self) -> int:
        return self._header.unknown0

    @property
    def unknown1(self) -> int:
        return self._header.unknown1

    @property
    def filler(self) -> bool:
        return self._subfile.filler

    @property
    def sections(self) -> int:
        return 0 if self.filler else len(self._header.sections)

    def __len__(self) -> int:
        return self.sections

    def __iter__(self):
        return IterChunk(self)

    def __getitem__(self, key: int) -> Subchunk:
        if self.filler:
            raise ValueError("cannot read filler")
        key = process_key(key, self.sections)

        for section in self._header.sections:
            if section.index == key:
                break
        else:
            # doesn't exist
            return None

        # section positions are relative to the start of the subfile, but the
        # raw data starts after the magic
        start = section.position - len(parser.SubfileHeader)
        if start < self._header.size or start + section.compressedSize > len(self._raw):
            raise ValueError("invalid position")
        subchunk = Subchunk(section, self._raw[start:])
        return subchunk


class VDBData:
    def __init__(self, subfile) -> None:
        self._subfile = subfile
        self._reload_data()

    def _reload_data(self) -> None:
        self._raw = self._subfile.raw_with_header
        self._header = parser.VDBHeader(self._raw)

    @property
    def name(self) -> str:
        return self._header.name

    @property
    def filler(self) -> bool:
        return self._subfile.filler

    @property
    def unknown0(self) -> int:
        return self._header.unknown0

    @property
    def unknown1(self) -> int:
        return self._header.unknown1

    @property
    def unknown2(self) -> int:
        return self._header.unknown2

    @property
    def raw(self) -> bytes:
        if self.filler:
            return None
        return self._raw[len(self._header) :]


class VDBFile(DBFile):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        assert self._header.unknown0 == 0x100

    def _parse(self, subfile: Subfile) -> VDBData:
        return VDBData(subfile)


class CDBFile(DBFile):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        assert self._header.unknown0 == 0x4

    def _parse(self, subfile: Subfile) -> Chunk:
        return Chunk(subfile)


class IterDBDirectory:
    def __init__(self, db_directory):
        self._db_directory = db_directory
        self._keys = list(sorted(db_directory.keys()))

    def __next__(self) -> tuple[int, DBFile]:
        try:
            new_key = self._keys.pop(0)
        except IndexError:
            raise StopIteration
        return new_key, self._db_directory[new_key]


class DBDirectory:
    @property
    @abstractmethod
    def file_expression(self):
        pass

    def __init__(self, path: str | bytes | os.PathLike) -> None:
        self._path = Path(path)
        self._reload_data()

    def __iter__(self) -> IterDBDirectory:
        return IterDBDirectory(self)

    def _reload_data(self) -> None:
        files = filter(lambda path: path.is_file(), self._path.iterdir())
        self._files = {}
        for db_file in files:
            filename = db_file.name
            matched = self.file_expression.fullmatch(filename)
            if matched is None:
                continue
            cdb_number = int(matched[1])
            self._files[cdb_number] = db_file

    @property
    def path(self) -> str | bytes | os.PathLike:
        return self._path

    @path.setter
    def path(self, value: str | bytes | os.PathLike) -> None:
        self._path = Path(value)
        self._reload_data()

    def keys(self) -> tuple[int]:
        return tuple(self._files.keys())

    def get_file(self, key: int) -> Path:
        return self._files[key]

    @abstractmethod
    def _process(self, stream: BytesIO) -> Any:
        pass

    def __getitem__(self, key: int) -> Any:
        path = self._files[key]
        return self._process(open(path, "rb"))


class CDBDirectory(DBDirectory):
    @property
    def file_expression(self):
        return re.compile(r"slt(0|(?:[1-9]\d*))\.cdb")

    def _process(self, stream: BytesIO) -> CDBFile:
        return CDBFile(stream)


class VDBDirectory(DBDirectory):
    @property
    def file_expression(self):
        return re.compile(r"slt(0|(?:[1-9]\d*))\.vdb")

    def _process(self, stream: BytesIO) -> VDBFile:
        return VDBFile(stream)


class Entry:
    def __init__(self, header, chunk: Chunk, debug=None) -> None:
        terrain = chunk[cdb.SECTION_TERRAIN]
        if terrain is None:
            raise ValueError(f"chunk {chunk.position} has no terrain section")
        self.blocks, self.heightmap, self.biomes = terrain.data
        self._chunk = chunk
        self.debug = debug

    @property
    def block_entities(self) -> list[dict]:
        section = self._chunk[cdb.SECTION_BLOCK_ENTITIES]
        if section is None:
            return []
        return cdb.read_nbt_stream(section.raw_decompressed)

    def __getitem__(self, position: tuple[int, int, int]) -> int:
        x, y, z = position
        subchunk_index, subchunk_y = y // 16, y % 16
        if subchunk_index > 8:
            raise KeyError("position out of range")

        try:
            subchunk = self.blocks[subchunk_index]
        except KeyError:
            return (0, 0)
        if len(subchunk) != SUBCHUNK_SIZE:
            print(f"bad subchunk length {len(subchunk):d}!")
            return (0, 0)
        position = x * (16 * 16) + subchunk_y + z * 16
        assert position < 0x1000
        # the block data is stored as nibbles
        data_position = 0x1000 + position // 2
        assert data_position < 0x1800
        try:
            block_id = subchunk[position]
            block_raw_data = subchunk[data_position]
        except IndexError:
            raise KeyError("position out of range") from None

        # extract the correct nibble
        if position % 2 == 0:
            block_data = block_raw_data & 0xF
        else:
            block_data = block_raw_data >> 4
        return (block_id, block_data)

    @property
    def subchunk_count(self) -> int:
        return len(self.blocks)


class CDBIndex(Index):
    @property
    def chunks(self) -> tuple:
        pass


class World:
    def __init__(self, path: str | bytes | os.PathLike) -> None:
        self._path = Path(path)
        self._reload_data()

    def _reload_data(self) -> None:
        self._db_path = self._path / "db"
        self._cdb_path = self._db_path / "cdb"
        self._vdb_path = self._db_path / "vdb"
        self.cdb = CDBDirectory(self._cdb_path)
        self.vdb = VDBDirectory(self._vdb_path)

        self._level_path = self._path / "level.dat"
        self._level_old_path = self._path / "level.dat_old"
        with open(self._level_path, "rb") as level_file:
            buffer = level_file.read()
        self.metadata = NBT(buffer)
        if self._level_old_path.exists():
            with open(self._level_old_path, "rb") as level_file:
                buffer = level_file.read()
            self.old_metadata = NBT(buffer)
        else:
            self.old_metadata = None
        # both index files list every chunk; when they disagree, the entry in
        # newindex.cdb wins (see cdb.load_indexes)
        self._indexes = []
        index_entries = {}
        for index_name in ("index.cdb", "newindex.cdb"):
            index_path = self._cdb_path / index_name
            if not index_path.exists():
                continue
            with open(index_path, "rb") as index_file:
                index = Index(index_file)
            if index._data.entrySize != cdb.INDEX_ENTRY_SIZE:
                raise ValueError(
                    f"{index_name} has unsupported entry size {index._data.entrySize:d}"
                )
            self._indexes.append(index)
            for entry in index.entries:
                index_entries[parse_position(entry.position)] = entry
        if not self._indexes:
            raise FileNotFoundError(f"no index.cdb or newindex.cdb in {self._cdb_path}")
        self._index = self._indexes[-1]

        self.entries = {}
        slot_files = {}
        try:
            self._load_entries(index_entries, slot_files)
        finally:
            for slot_file in slot_files.values():
                slot_file._stream.close()

    def _load_entries(self, index_entries: dict, slot_files: dict) -> None:
        for position, entry in index_entries.items():
            slot = entry.slot
            assert entry.constant0 == cdb.INDEX_ENTRY_CONSTANT0
            if entry.constant1 != cdb.INDEX_ENTRY_CONSTANT1:
                print(f"!!! not constant 0x{entry.constant1:X} !!!")
            assert entry.constant2 == cdb.INDEX_ENTRY_CONSTANT2
            if slot not in self.cdb.keys():
                pass  # print(f"N {entry}")
            else:
                if slot not in slot_files:
                    slot_files[slot] = self.cdb[slot]
                chunk = slot_files[slot][entry.subfile]
                assert position == chunk.position
                debug = None
                self.entries[position] = Entry(entry, chunk, debug)

    def __iter__(self):
        return IterWorld(self)

    def __getitem__(self, position: tuple[int, int, int]) -> int:
        x, y, z = position
        chunk_x = x % 0x10
        chunk_y = y % 0x10
        chunk_z = z % 0x10
        entry = self.get_entry((x, y, z))
        if entry is None:
            return 0  # air
        try:
            block_id = entry[(chunk_x, chunk_y, chunk_z)]
        except KeyError:
            return 0  # air
        return block_id

    def get_entry(self, position: tuple[int, int, int]) -> int:
        x, y, z = position
        world_x = x // 0x10
        world_z = z // 0x10
        try:
            return self.entries[(world_x, world_z)]
        except KeyError:
            return None

    @property
    def index(self) -> Index:
        return self._index

    @property
    def path(self) -> Path:
        return self._path

    @path.setter
    def path(self, value: str | bytes | os.PathLike) -> None:
        self._path = Path(value)
        self._reload_data()

    @property
    def name(self) -> str:
        return self.metadata.get("LevelName")


class IterWorld:
    def __init__(self, world: World) -> None:
        self._world = world
        self.__entries_left = list(world.entries.items())
        self.__blocks_left = []
        self.__entry = None
        self.__position = None

    def __next__(self) -> tuple[tuple[int, int, int], int]:
        while not self.__blocks_left:
            if not self.__entries_left:
                raise StopIteration
            self.__blocks_left = []
            position, self.__entry = self.__entries_left.pop(0)
            self.__position = (position[0] * 0x10, position[1] * 0x10, position[2])
            for x in range(0x10):
                for z in range(0x10):
                    for y in range(0x10 * self.__entry.subchunk_count):
                        self.__blocks_left.append((x, y, z))
        coordinates = self.__blocks_left.pop(0)
        offset_x, offset_z, dimension = self.__position
        return (
            (
                coordinates[0] + offset_x,
                coordinates[1],
                coordinates[2] + offset_z,
            ),
            dimension,
            self.__entry,
            self.__entry[coordinates],
        )
