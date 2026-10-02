"""
Reading Java Edition worlds, using the same anvil (anvil-new) and nbt stack
that the 3DS to Java converter writes with.

anvil-new only understands 1.18+ chunks and treats single-block sections as
air, so the older chunk layouts and the block state unpacking are handled here.
"""

import re
from pathlib import Path

import anvil
import numpy as np
from nbt import nbt

REGION_NAME = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca$")
REGION_HEADER_SIZE = 0x2000

# https://minecraft.wiki/w/Data_version
VERSION_FLATTENING = 1451  # 17w47a, numeric ids became block states
VERSION_NON_SPANNING = 2529  # 20w17a, block states stopped spanning longs
VERSION_NO_LEVEL = 2844  # 21w43a, the "Level" compound was removed

COMPRESSION_NAMES = {1: "GZip", 3: "uncompressed", 4: "LZ4"}

DIMENSION_FOLDERS = {0: None, 1: "DIM-1", 2: "DIM1"}
DIMENSION_NAMES = {0: "Overworld", 1: "Nether", 2: "End"}


class JavaWorldError(Exception):
    pass


class JavaChunkError(Exception):
    pass


def region_coordinates(path: Path) -> tuple[int, int]:
    matched = REGION_NAME.match(path.name)
    if matched is None:
        return 0, 0
    return int(matched[1]), int(matched[2])


def list_region_files(region_directory: Path) -> list[Path]:
    files = sorted(region_directory.glob("r.*.*.mca"))
    if not files:
        files = sorted(region_directory.glob("*.mca"))
    return files


def find_region_directories(path: Path) -> dict[int | None, Path]:
    """
    Returns the region folders of a world keyed by dimension, or
    ``{None: path}`` if ``path`` is itself a region folder.
    """
    path = Path(path)
    if not path.is_dir():
        raise JavaWorldError(f"{path} is not a folder")
    if list_region_files(path):
        return {None: path}

    found = {}
    for dimension, folder in DIMENSION_FOLDERS.items():
        dimension_path = path if folder is None else path / folder
        region_directory = dimension_path / "region"
        if region_directory.is_dir() and list_region_files(region_directory):
            found[dimension] = region_directory
    if found:
        return found

    if list(path.glob("*.mcr")) or list(path.glob("region/*.mcr")):
        raise JavaWorldError(
            "this world uses the McRegion (.mcr) format from before Java 1.2, "
            "open it in Java Edition 1.2 or newer first to convert it to Anvil (.mca)"
        )
    if (path / "db").is_dir():
        raise JavaWorldError(
            "this looks like a Bedrock or MC3DS world, not a Java Edition world"
        )
    raise JavaWorldError(f"no Java Edition region files (.mca) found in {path}")


def read_level_data(world_path: Path) -> dict | None:
    "returns the world's name and version, or None for a bare region folder"
    level_path = Path(world_path) / "level.dat"
    if not level_path.is_file():
        return None
    import nbtlib

    try:
        data = nbtlib.load(level_path)["Data"]
    except Exception as error:
        raise JavaWorldError(f"level.dat is not a valid Java Edition level.dat: {error}")
    version = data.get("Version")
    return {
        "name": str(data.get("LevelName", "")),
        "data_version": int(data["DataVersion"]) if "DataVersion" in data else None,
        "version_name": str(version["Name"]) if version and "Name" in version else None,
    }


def unpack_block_states(longs, palette_size: int, spanning: bool) -> np.ndarray:
    """Unpacks 4096 palette indices (YZX order) from a block state long array.

    Post-20w17a worlds normally use non-spanning longs, but upgraded maps often
    still have compact (bit-spanning) arrays — e.g. 5-bit palettes stored as 320
    longs (4096*5/64) instead of 342. Detect by length and fall back.
    """
    bits = max((palette_size - 1).bit_length(), 4)
    # the nbt library reads long arrays as unsigned, other writers use signed
    states = np.array([value & 0xFFFFFFFFFFFFFFFF for value in longs], dtype=np.uint64)
    per_long = 64 // bits
    needed_nons = -(-4096 // per_long)
    needed_span = -(-(4096 * bits) // 64)

    use_span = spanning
    if not use_span and len(states) < needed_nons:
        # Upgraded 1.13–1.16 chunks: DataVersion says non-spanning but data is compact
        use_span = True

    if use_span:
        needed_bits = 4096 * bits
        bitstream = np.unpackbits(states.astype("<u8").view(np.uint8), bitorder="little")
        if len(bitstream) < needed_bits:
            # Pad truncated arrays rather than aborting the whole region
            bitstream = np.pad(bitstream, (0, needed_bits - len(bitstream)))
        weights = (1 << np.arange(bits, dtype=np.uint32)).astype(np.uint32)
        indices = bitstream[:needed_bits].reshape(4096, bits).astype(np.uint32) @ weights
    else:
        if len(states) < needed_nons:
            raise JavaChunkError(
                f"block state array is too short ({len(states):d} longs, "
                f"need {needed_nons:d} non-span or {needed_span:d} span for {bits:d}-bit)"
            )
        shifts = np.arange(per_long, dtype=np.uint64) * np.uint64(bits)
        mask = np.uint64((1 << bits) - 1)
        indices = ((states[:needed_nons, None] >> shifts) & mask).reshape(-1)[:4096]
    indices = indices.astype(np.uint32)
    if indices.max() >= palette_size:
        raise JavaChunkError("block state refers to a missing palette entry")
    return indices


def _nibbles(raw) -> np.ndarray:
    packed = np.frombuffer(bytes(raw), dtype=np.uint8)
    result = np.empty(packed.size * 2, dtype=np.uint8)
    result[0::2] = packed & 0xF
    result[1::2] = packed >> 4
    return result


class JavaChunk:
    def __init__(self, nbt_data: nbt.NBTFile) -> None:
        self.data_version = (
            nbt_data["DataVersion"].value if "DataVersion" in nbt_data else -1
        )
        if "Level" in nbt_data:
            level = nbt_data["Level"]
            self.x = level["xPos"].value
            self.z = level["zPos"].value
            self._sections = level["Sections"] if "Sections" in level else []
            self.tile_entities = level["TileEntities"] if "TileEntities" in level else []
            self.entities = level["Entities"] if "Entities" in level else []
            self.lowest_section = 0
        else:
            chunk = anvil.Chunk(nbt_data)
            self.x, self.z = chunk.x, chunk.z
            self._sections = nbt_data["sections"] if "sections" in nbt_data else []
            self.tile_entities = chunk.tile_entities
            # 1.17+ moved entities out of the chunk; keep empty when absent
            if "entities" in nbt_data:
                self.entities = nbt_data["entities"]
            elif "Entities" in nbt_data:
                self.entities = nbt_data["Entities"]
            else:
                self.entities = getattr(chunk, "entities", []) or []
            # 1.18+ worlds start at y=-64
            self.lowest_section = -4

    def section(self, y: int) -> nbt.TAG_Compound | None:
        for section in self._sections:
            if section["Y"].value == y:
                return section
        return None

    def blocks(self, y: int, legacy_blocks: dict) -> tuple[list, np.ndarray] | None:
        """
        Returns ``(palette, indices)`` for a section, where palette entries are
        ``anvil.Block`` objects and ``indices`` are 4096 palette indices in YZX
        order, or None if the section has no blocks.
        """
        section = self.section(y)
        if section is None:
            return None
        if "block_states" in section:
            states = section["block_states"]
            palette_tag = states["palette"]
            longs = states["data"].value if "data" in states else None
        elif "Palette" in section:
            palette_tag = section["Palette"]
            longs = section["BlockStates"].value if "BlockStates" in section else None
        elif "Blocks" in section:
            return self._legacy_blocks(section, legacy_blocks)
        else:
            return None

        palette = [anvil.Block.from_palette(tag) for tag in palette_tag]
        if not palette:
            return None
        if longs is None or len(palette) == 1:
            # a section made of a single block has no data array
            return palette, np.zeros(4096, dtype=np.uint32)
        spanning = self.data_version < VERSION_NON_SPANNING
        return palette, unpack_block_states(longs, len(palette), spanning)

    @staticmethod
    def _legacy_blocks(section, legacy_blocks: dict) -> tuple[list, np.ndarray]:
        ids = np.frombuffer(bytes(section["Blocks"].value), dtype=np.uint8).astype(np.uint32)
        if "Add" in section:
            ids |= _nibbles(section["Add"].value).astype(np.uint32) << 8
        data = _nibbles(section["Data"].value).astype(np.uint32)
        keys = (ids << 4) | data
        unique, indices = np.unique(keys, return_inverse=True)
        palette = []
        for key in unique.tolist():
            block_id, block_data = key >> 4, key & 0xF
            block = legacy_blocks.get((block_id, block_data))
            if block is None:
                block = anvil.Block("legacy", f"{block_id:d}:{block_data:d}")
            palette.append(block)
        return palette, indices.astype(np.uint32)


class RegionReader:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data = self.path.read_bytes()
        self._region = anvil.Region(self._data)

    @property
    def empty(self) -> bool:
        return len(self._data) == 0

    @property
    def truncated(self) -> bool:
        return 0 < len(self._data) < REGION_HEADER_SIZE

    def read(self, local_x: int, local_z: int) -> JavaChunk | None:
        "returns None for chunks that were never generated"
        if len(self._data) < REGION_HEADER_SIZE:
            return None
        sector, sector_count = self._region.chunk_location(local_x, local_z)
        if (sector, sector_count) == (0, 0):
            return None
        offset = sector * 4096
        if offset + 5 > len(self._data):
            raise JavaChunkError("chunk data is past the end of the region file")
        compression = self._data[offset + 4]
        if compression & 0x80:
            raise JavaChunkError(
                "chunk is stored in an external .mcc file, which is not supported"
            )
        if compression != 2:
            name = COMPRESSION_NAMES.get(compression, f"type {compression:d}")
            raise JavaChunkError(f"{name} chunk compression is not supported")
        try:
            nbt_data = self._region.chunk_data(local_x, local_z)
            return JavaChunk(nbt_data)
        except JavaChunkError:
            raise
        except Exception as error:
            raise JavaChunkError(f"corrupt chunk data ({type(error).__name__}: {error})")
