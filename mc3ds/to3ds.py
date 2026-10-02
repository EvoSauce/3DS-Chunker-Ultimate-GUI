"""
Java Edition to MC3DS conversion.

This is a port of the reference inserter (``refs/3dsleveledit.py``): Java data
is read with ``java.py`` and written in the MC3DS format defined by ``cdb.py``.
Like the reference, chunks are inserted into an existing MC3DS world, because
the game data around the chunk database (level.dat, db/vdb) cannot be
generated from a Java world.
"""

import json
import shutil
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import anvil
import numpy as np

from . import cdb
from .block_entities import BLOCK_ID_TO_BE, convert_block_entity, stub_block_entity
from .block_subs import SimilarBlockMapper
from .entities import EntityMapper, convert_entity
from .items import ItemMapper
from .java import (
    DIMENSION_NAMES,
    JavaChunkError,
    JavaWorldError,
    RegionReader,
    find_region_directories,
    list_region_files,
    read_level_data,
    region_coordinates,
)
from .nbt_java import as_dict
from .progress import Progress

BLOCKS_JSON = Path(__file__).parent / "data" / "blocks.json"
AIR_BLOCKS = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
REGION_CHUNKS = 32

# Stair shape is neighbour-derived in PE; connection bits are too. Ignoring them
# lets Java states match the facing/half/axis entries in blocks.json.
_IGNORE_PROPS = {
    "shape",
    "waterlogged",
    "distance",
    "persistent",
    "east",
    "west",
    "north",
    "south",
    "up",
    "down",
    "in_wall",
}
# Must agree when both sides define the property, or facing/axis is lost.
_CRITICAL_PROPS = {
    "facing",
    "axis",
    "half",
    "open",
    "hinge",
    "part",
    "rotation",
    "face",
    "attachment",
    "type",  # slab top/bottom/double; chests remapped below
    "lit",
    "extended",
    "mode",
    "short",
    "powered",  # observer / door / etc.
}


class ConversionError(Exception):
    pass


# -------------------------
# Block mapping
# -------------------------
def format_block_name(name: str, properties: dict) -> str:
    if properties:
        joined = ",".join(f"{key}={value}" for key, value in sorted(properties.items()))
        return f"{name}[{joined}]"
    return name


def normalize_block_value(value) -> tuple[str, dict]:
    if isinstance(value, dict):
        name = str(value.get("Name", "minecraft:air"))
        properties = value.get("Properties", {}) or {}
        return name, {str(k): str(v) for k, v in properties.items()}
    if isinstance(value, str):
        text = value.strip()
        if "[" in text and text.endswith("]"):
            name, rest = text.split("[", 1)
            properties = {}
            if rest[:-1]:
                for part in rest[:-1].split(","):
                    if "=" in part:
                        key, item = part.split("=", 1)
                        item = item.strip()
                        if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
                            item = item[1:-1]
                        properties[key.strip()] = item
                    else:
                        properties[part.strip()] = ""
            return name.strip(), properties
        return text, {}
    return str(value), {}


_BOOL_PROP_KEYS = frozenset(
    {
        "extended",
        "powered",
        "triggered",
        "enabled",
        "lit",
        "open",
        "occupied",
        "waterlogged",
        "short",
        "persistent",
        "signal_fire",
        "eye",
    }
)


def _prop_value(value, key: str | None = None) -> str:
    """Normalize a Java block-state property to the lowercase string used in blocks.json."""
    if hasattr(value, "value") and not isinstance(value, (str, bytes, int, float, bool)):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    sv = str(value).strip()
    if len(sv) >= 2 and sv[0] == sv[-1] and sv[0] in "\"'":
        sv = sv[1:-1]
    low = sv.lower()
    if low in ("true", "false"):
        return low
    # Only coerce 0/1 for known boolean props — not sign rotation etc.
    if key in _BOOL_PROP_KEYS and (sv in ("0", "1") or low in ("yes", "no")):
        return "true" if sv == "1" or low == "yes" else "false"
    if key == "facing" or key == "axis":
        return low
    return sv


def _match_props(name: str, properties: dict) -> dict:
    """Drop PE-irrelevant props and normalize chest double-types to single."""
    props = {}
    for k, v in properties.items():
        key = str(k)
        if key in _IGNORE_PROPS:
            continue
        props[key] = _prop_value(v, key)
    bare = name.split(":", 1)[-1]
    if bare in ("chest", "trapped_chest", "barrel") and props.get("type") in ("left", "right"):
        props = dict(props)
        props["type"] = "single"
    # Trapdoors in blocks.json couple powered to open; Java keeps them independent.
    if bare.endswith("trapdoor") and "open" in props:
        props.pop("powered", None)
    # Facing blocks: PE metadata is facing + one status bit only.
    # Extra Java props (or True/False vs true/false) made fuzzy match fall through
    # to the bare ``id:0`` legacy entry → everything faced down.
    if bare in ("piston", "sticky_piston"):
        props = {k: props[k] for k in ("facing", "extended") if k in props}
        props.setdefault("extended", "false")
    if bare == "observer":
        props = {k: props[k] for k in ("facing", "powered") if k in props}
        props.setdefault("powered", "false")
    if bare == "piston_head":
        props = {k: props[k] for k in ("facing", "type", "short") if k in props}
        props.setdefault("short", "false")
        props.setdefault("type", "normal")
    if bare in ("dropper", "dispenser"):
        props = {k: props[k] for k in ("facing", "triggered") if k in props}
        props.setdefault("triggered", "false")
    if bare == "hopper":
        props = {k: props[k] for k in ("facing", "enabled") if k in props}
        props.setdefault("enabled", "true")
    if bare.endswith("shulker_box") or bare == "shulker_box":
        props = {k: props[k] for k in ("facing",) if k in props}
    if bare in ("furnace", "lit_furnace", "blast_furnace", "smoker"):
        keep = {k: props[k] for k in ("facing", "lit") if k in props}
        if bare in ("blast_furnace", "smoker"):
            keep.setdefault("lit", "false")
        props = keep
    return props


def _score_props(wanted: dict, candidate: dict) -> int:
    """
    Higher is better. Returns -1 when a critical orientation property conflicts.
    """
    score = 0
    for key, value in wanted.items():
        if key not in candidate:
            continue
        if candidate[key] == value:
            score += 10 if key in _CRITICAL_PROPS else 2
        elif key in _CRITICAL_PROPS:
            return -1
        else:
            score -= 1
    # Prefer the candidate that doesn't carry extra critical defaults we didn't ask for
    # (kept neutral — extras are fine).
    return score


class BlockMapper:
    """
    Maps Java block states to MC3DS ``(id, data)`` pairs using the inverse of
    ``blocks.json``. Unknown states are fuzzy-matched on facing/axis/half/etc
    (stair ``shape`` and similar PE-irrelevant props are ignored), then optionally
    translated with PyMCTranslate, then become air.
    """

    def __init__(self, blocks_json: Path = BLOCKS_JSON, progress: Progress | None = None) -> None:
        with open(blocks_json, "r", encoding="utf-8") as stream:
            raw_blocks = json.load(stream).get("blocks")
        if not isinstance(raw_blocks, dict):
            raise ValueError("blocks.json: missing top-level 'blocks' object")

        self.inverse = {}
        self.legacy = {}
        self._by_name: dict[str, list[tuple[dict, tuple[int, int]]]] = defaultdict(list)
        for key, value in raw_blocks.items():
            try:
                numeric = tuple(int(part) for part in key.split(":"))
            except ValueError:
                continue
            if len(numeric) != 2:
                continue
            name, properties = normalize_block_value(value)
            self.inverse[format_block_name(name, properties)] = numeric
            namespace, _, block_id = name.partition(":")
            self.legacy[numeric] = anvil.Block(namespace, block_id, properties)
            self._by_name[name].append((_match_props(name, properties), numeric))
            # also index without namespace for bare lookups
            bare = name.split(":", 1)[-1]
            if bare != name:
                self._by_name.setdefault(f"minecraft:{bare}", self._by_name[name])

        self.unmapped = Counter()
        self._cache = {}
        self._translator = _PyMCTranslate.create(progress)

    def lookup_state(self, name: str, properties: dict | None = None) -> tuple[int, int] | None:
        """Exact then fuzzy ``(id, data)`` for a namespaced block + props."""
        properties = {str(k): _prop_value(v, str(k)) for k, v in (properties or {}).items()}
        canonical = format_block_name(name, properties)
        hit = self.inverse.get(canonical)
        if hit is not None:
            return hit

        if not name.startswith("minecraft:"):
            name = f"minecraft:{name}"
        wanted = _match_props(name, properties)
        candidates = self._by_name.get(name) or self._by_name.get(f"minecraft:{name.split(':', 1)[-1]}")
        if not candidates:
            # bare name with no props in the json
            bare_hit = self.inverse.get(name) or self.inverse.get(name.split(":", 1)[-1])
            return bare_hit

        best: tuple[int, int] | None = None
        best_score = -1
        best_detail = -1
        for cand_props, numeric in candidates:
            score = _score_props(wanted, cand_props)
            detail = len(cand_props)
            if score > best_score or (score == best_score and score >= 0 and detail > best_detail):
                best_score = score
                best_detail = detail
                best = numeric
        # If every candidate critically conflicted, fall back to the first entry
        # only when the caller supplied no orientation props at all.
        if best is None and not any(k in wanted for k in _CRITICAL_PROPS):
            return candidates[0][1]
        return best if best_score >= 0 else None

    def map(self, block, data_version: int) -> tuple[int, int]:
        properties = {str(k): _prop_value(v, str(k)) for k, v in block.properties.items()}
        name = f"{block.namespace}:{block.id}"
        canonical = format_block_name(name, properties)
        key = (canonical, data_version)
        try:
            return self._cache[key]
        except KeyError:
            pass

        result = self.lookup_state(name, properties) or (0, 0)
        if result == (0, 0) and self._translator is not None:
            translated = self._translator.translate(canonical, data_version)
            if translated:
                t_name, t_props = normalize_block_value(translated)
                result = self.lookup_state(t_name, t_props) or (0, 0)
        if result == (0, 0) and block.name() not in AIR_BLOCKS:
            self.unmapped[canonical] += 1
        self._cache[key] = result
        return result

    def lookup_tables(self, palette: list, data_version: int) -> tuple[np.ndarray, np.ndarray]:
        mapped = [self.map(block, data_version) for block in palette]
        ids = np.array([block_id & 0xFF for block_id, _ in mapped], dtype=np.uint8)
        data = np.array([block_data & 0xF for _, block_data in mapped], dtype=np.uint8)
        return ids, data


class _PyMCTranslate:
    "optional Java block state translation, used exactly like the reference"

    def __init__(self, module, block_class, progress) -> None:
        self._manager = module.new_translation_manager()
        self._destination = self._manager.get_version("java", (1, 16, 5))
        self._block_class = block_class
        self._sources = {}
        self._cache = {}

    @classmethod
    def create(cls, progress):
        try:
            import PyMCTranslate
            from PyMCTranslate.py3.api import Block
        except Exception:
            if progress is not None:
                progress.status(
                    "PyMCTranslate is not installed; block states missing from "
                    "blocks.json will become air"
                )
            return None
        return cls(PyMCTranslate, Block, progress)

    def translate(self, blockstate: str, data_version: int) -> str | None:
        key = (blockstate, data_version)
        if key in self._cache:
            return self._cache[key]
        result = None
        try:
            if data_version not in self._sources:
                try:
                    source = self._manager.get_version("java", data_version)
                except Exception:
                    source = self._manager.get_version("java", (1, 16, 5))
                self._sources[data_version] = source
            source = self._sources[data_version]
            block = self._block_class.from_string_blockstate(blockstate)
            universal, universal_entity, _ = source.block.to_universal(
                block, block_entity=None, force_blockstate=True
            )
            output, _, _ = self._destination.block.from_universal(
                universal, block_entity=universal_entity, force_blockstate=True
            )
            if hasattr(output, "blockstate"):
                result = str(output.blockstate)
        except Exception:
            result = None
        self._cache[key] = result
        return result


# -------------------------
# Block / entity helpers
# -------------------------
def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _block_entity_position(entity: dict) -> tuple[int, int, int] | None:
    if "x" in entity and "y" in entity and "z" in entity:
        return _safe_int(entity["x"]), _safe_int(entity["y"]), _safe_int(entity["z"])
    if "pos" in entity:
        position = entity["pos"]
        return _safe_int(position[0]), _safe_int(position[1]), _safe_int(position[2])
    return None


# PE block id → block-entity save name is defined in block_entities.BLOCK_ID_TO_BE


def _ensure_block_entity_stubs(
    be_list: list[dict] | None,
    terrain_compressed: bytes,
    chunk_x: int,
    chunk_z: int,
) -> list[dict] | None:
    """Add minimal BEs for blocks that require them but have no TileEntity yet."""
    try:
        raw = zlib.decompress(terrain_compressed)
        subchunks, _heightmap, _biomes = cdb.parse_terrain_section(raw)
    except Exception:
        return be_list
    existing = {
        (be.get("x"), be.get("y"), be.get("z"))
        for be in (be_list or [])
        if isinstance(be, dict)
    }
    out = list(be_list or [])
    base_x = chunk_x * 16
    base_z = chunk_z * 16
    for sub_y, payload in enumerate(subchunks):
        # payload = block ids (4096) + data + sky + block light
        if len(payload) < 4096:
            continue
        blocks = payload[:4096]
        for i, block_id in enumerate(blocks):
            be_id = BLOCK_ID_TO_BE.get(block_id)
            if not be_id:
                continue
            # ids array is shaped (x, z, y); C-order tobytes => y fastest
            ly = i & 15
            lz = (i >> 4) & 15
            lx = (i >> 8) & 15
            x = base_x + lx
            y = sub_y * 16 + ly
            z = base_z + lz
            if (x, y, z) in existing:
                continue
            sticky = block_id == 29
            out.append(stub_block_entity(be_id, x, y, z, sticky=sticky))
            existing.add((x, y, z))
    return out or None


# -------------------------
# Regions
# -------------------------
class JavaRegion:
    """
    A Java region prepared for MC3DS. MC3DS worlds are 128 blocks tall, so the
    bottom 8 sections of the region's chunks are used. 1.18+ chunks start at
    y=-64; if any chunk in the region does, the whole region is shifted up by
    64 blocks so Java y=-64..63 becomes MC3DS y=0..127.
    """

    def __init__(
        self,
        path: Path,
        mapper: BlockMapper,
        progress: Progress,
        item_mapper: ItemMapper | None = None,
        entity_mapper: EntityMapper | None = None,
    ) -> None:
        self.path = Path(path)
        self.region_x, self.region_z = region_coordinates(self.path)
        self.errors = 0
        self._mapper = mapper
        self._item_mapper = item_mapper or ItemMapper()
        self._entity_mapper = entity_mapper or EntityMapper()
        self._chunks = defaultdict(list)
        self._block_entities = []
        self._entities = []

        reader = RegionReader(self.path)
        if reader.empty:
            progress.status(
                f"{self.path.name} is empty; its chunks will be written as air"
            )
        elif reader.truncated:
            progress.warning(
                f"{self.path.name} is truncated; its chunks will be written as air"
            )
            self.errors += 1

        base_chunk_x = self.region_x * REGION_CHUNKS
        base_chunk_z = self.region_z * REGION_CHUNKS
        lowest = None
        for local_z in range(REGION_CHUNKS):
            for local_x in range(REGION_CHUNKS):
                try:
                    chunk = reader.read(local_x, local_z)
                except JavaChunkError as error:
                    progress.warning(
                        f"{self.path.name}: chunk {base_chunk_x + local_x:d}, "
                        f"{base_chunk_z + local_z:d} was written as air: {error}"
                    )
                    self.errors += 1
                    continue
                if chunk is None:
                    continue

                for entity in chunk.tile_entities or []:
                    entity = as_dict(entity)
                    position = _block_entity_position(entity)
                    if position is None:
                        continue
                    x, y, z = position
                    self._block_entities.append(
                        (entity, x - base_chunk_x * 16, y, z - base_chunk_z * 16)
                    )

                for entity in getattr(chunk, "entities", None) or []:
                    entity = as_dict(entity)
                    self._entities.append(entity)

                relative_x = chunk.x - base_chunk_x
                relative_z = chunk.z - base_chunk_z
                if not (0 <= relative_x < REGION_CHUNKS and 0 <= relative_z < REGION_CHUNKS):
                    progress.warning(
                        f"{self.path.name}: chunk claims to be at {chunk.x:d}, {chunk.z:d} "
                        "which is outside this region; its blocks were skipped"
                    )
                    self.errors += 1
                    continue
                if lowest is None or chunk.lowest_section < lowest:
                    lowest = chunk.lowest_section
                self._chunks[(relative_x, relative_z)].append(chunk)

        self.lowest_section = lowest if lowest is not None else 0
        self.y_shift = -self.lowest_section * 16 if self.lowest_section < 0 else 0
        # a region without a single readable chunk still gets (air) chunks,
        # but with only one subchunk each
        self.height = cdb.WORLD_HEIGHT if lowest is not None else 1
        self.subchunk_count = (self.height + 15) // 16

    def terrain(self, local_x: int, local_z: int) -> tuple[bytes, int]:
        height = self.subchunk_count * 16
        ids = np.zeros((16, 16, height), dtype=np.uint8)
        data = np.zeros((16, 16, height), dtype=np.uint8)
        for chunk in self._chunks.get((local_x, local_z), []):
            for subchunk in range(self.subchunk_count):
                section_y = subchunk + self.lowest_section
                if not (chunk.lowest_section <= section_y < chunk.lowest_section + cdb.MAX_SUBCHUNKS):
                    continue
                try:
                    section = chunk.blocks(section_y, self._mapper.legacy)
                except JavaChunkError:
                    # Skip a corrupt / unreadable section rather than aborting the region
                    self.errors += 1
                    continue
                if section is None:
                    continue
                palette, indices = section
                id_table, data_table = self._mapper.lookup_tables(palette, chunk.data_version)
                # Java stores sections in YZX order, MC3DS subchunks in XZY order
                section_ids = id_table[indices].reshape(16, 16, 16).transpose(2, 1, 0)
                section_data = data_table[indices].reshape(16, 16, 16).transpose(2, 1, 0)
                solid = (section_ids != 0) | (section_data != 0)
                y_slice = slice(subchunk * 16, subchunk * 16 + 16)
                ids[:, :, y_slice][solid] = section_ids[solid]
                data[:, :, y_slice][solid] = section_data[solid]

        subchunks = []
        for subchunk in range(self.subchunk_count):
            y_slice = slice(subchunk * 16, subchunk * 16 + 16)
            blocks = ids[:, :, y_slice].tobytes()
            flat = data[:, :, y_slice].reshape(-1)
            nibbles = (flat[0::2] & 0xF) | ((flat[1::2] & 0xF) << 4)
            subchunks.append((blocks, nibbles.astype(np.uint8).tobytes()))
        return cdb.build_terrain_section(subchunks)

    def block_entities(self, base_block_x: int, base_block_z: int) -> dict:
        "groups the region's block entities by the MC3DS chunk they end up in"
        grouped = defaultdict(list)
        for entity, x, y, z in self._block_entities:
            world_x = base_block_x + x
            world_z = base_block_z + z
            y2 = y + self.y_shift
            if not (0 <= y2 < cdb.WORLD_HEIGHT):
                continue
            converted = convert_block_entity(
                entity, world_x, y2, world_z, self._item_mapper
            )
            if converted:
                grouped[(world_x // 16, world_z // 16)].append(converted)
        return grouped


@dataclass
class InsertResult:
    regions: int = 0
    chunks: int = 0
    errors: int = 0
    new_slots: list = field(default_factory=list)


def insert_region_directory(
    cdb_directory: Path,
    region_directory: Path,
    mapper: BlockMapper,
    start_chunk_x: int = 0,
    start_chunk_z: int = 0,
    dimension: int = 0,
    progress: Progress | None = None,
    item_mapper: ItemMapper | None = None,
    entity_mapper: EntityMapper | None = None,
) -> InsertResult:
    """
    Inserts every region file of a Java region folder into an MC3DS chunk
    database. Every region file becomes a full 32x32 block of MC3DS chunks:
    chunks that don't exist in the region file are written as air, so the
    game never generates its own terrain inside the converted area.
    """
    progress = progress or Progress()
    item_mapper = item_mapper or ItemMapper()
    entity_mapper = entity_mapper or EntityMapper()
    cdb_directory = Path(cdb_directory)
    indexes = cdb.load_indexes(cdb_directory)
    for index in indexes:
        index.chunk_capacity = indexes[0].chunk_capacity
        index.constant1 = indexes[0].constant1

    slot_files = cdb.list_slot_files(cdb_directory)
    if not slot_files:
        raise ConversionError(f"no slt*.cdb files in {cdb_directory}")
    template = slot_files[0].read_bytes()
    merged = cdb.merged_index_map(indexes)
    allocator = cdb.SlotAllocator(indexes, cdb_directory, template)

    region_files = list_region_files(Path(region_directory))
    if not region_files:
        raise JavaWorldError(f"no .mca files found in {region_directory}")

    result = InsertResult()
    total_chunks = len(region_files) * REGION_CHUNKS * REGION_CHUNKS
    for region_number, region_path in enumerate(region_files, 1):
        progress.status(f"Reading region {region_path.name}...")
        region = JavaRegion(
            region_path, mapper, progress, item_mapper, entity_mapper
        )
        result.errors += region.errors

        region_start_chunk_x = start_chunk_x + region.region_x * REGION_CHUNKS
        region_start_chunk_z = start_chunk_z + region.region_z * REGION_CHUNKS
        block_entities = region.block_entities(
            region_start_chunk_x * 16, region_start_chunk_z * 16
        )
        # Entity Pos stays in Java world coords; apply the same chunk offset
        # used for terrain by shifting X/Z after conversion.
        ox = start_chunk_x * 16
        oz = start_chunk_z * 16
        entities_by_chunk = defaultdict(list)
        for entity in region._entities:
            converted = convert_entity(
                entity, entity_mapper, item_mapper, y_shift=region.y_shift
            )
            if not converted:
                continue
            pos = converted.get("Pos")
            if not isinstance(pos, list) or len(pos) < 3:
                continue
            pos[0] = float(pos[0]) + ox
            pos[2] = float(pos[2]) + oz
            converted["Pos"] = pos
            try:
                cx = int(pos[0]) // 16
                cz = int(pos[2]) // 16
            except Exception:
                continue
            entities_by_chunk[(cx, cz)].append(converted)

        progress.status(
            f"Writing MC3DS chunks {region_start_chunk_x:d}, {region_start_chunk_z:d} to "
            f"{region_start_chunk_x + REGION_CHUNKS - 1:d}, "
            f"{region_start_chunk_z + REGION_CHUNKS - 1:d} "
            f"({DIMENSION_NAMES.get(dimension, dimension)})..."
        )

        # x-major order decides which chunk gets which subfile
        for local_x in range(REGION_CHUNKS):
            for local_z in range(REGION_CHUNKS):
                chunk_x = region_start_chunk_x + local_x
                chunk_z = region_start_chunk_z + local_z
                try:
                    packed = cdb.pack_position(chunk_x, chunk_z, dimension)
                except ValueError as error:
                    raise ConversionError(
                        f"{region_path.name}: {error}; use a chunk offset that keeps "
                        "the world inside the MC3DS range"
                    )

                if packed in merged:
                    slot, subfile = merged[packed][:2]
                else:
                    slot, subfile, created = allocator.allocate()
                    if created:
                        result.new_slots.append(slot)
                        progress.status(f"Created slt{slot:d}.cdb (copy of {slot_files[0].name})")

                terrain, terrain_size = region.terrain(local_x, local_z)
                be_list = block_entities.get((chunk_x, chunk_z))
                be_list = _ensure_block_entity_stubs(
                    be_list, terrain, chunk_x, chunk_z
                )
                if be_list:
                    raw_be = cdb.write_nbt_stream(be_list)
                    be_compressed = zlib.compress(raw_be)
                    be_size = len(raw_be)
                else:
                    be_compressed, be_size = None, 0

                ent_list = entities_by_chunk.get((chunk_x, chunk_z))
                if ent_list:
                    raw_ent = cdb.write_nbt_stream(ent_list)
                    ent_compressed = zlib.compress(raw_ent)
                    ent_size = len(raw_ent)
                else:
                    ent_compressed, ent_size = None, 0

                def blob(page_magic: int) -> bytes:
                    return cdb.build_chunk_blob(
                        packed,
                        terrain,
                        terrain_size,
                        be_compressed,
                        be_size,
                        page_magic,
                        ent_compressed,
                        ent_size,
                    )

                try:
                    cdb.write_chunk_to_slot(cdb_directory, slot, subfile, blob)
                except ValueError as error:
                    raise ConversionError(f"chunk {chunk_x:d}, {chunk_z:d}: {error}")

                for index in indexes:
                    replaced = index.update_entry(packed, slot, subfile, 1, 0)
                    allocator.record(slot, subfile, replaced)
                merged[packed] = (slot, subfile, 1, 0)
                result.chunks += 1

            progress.status(
                f"Converting chunk {chunk_x:d}, {region_start_chunk_z:d}..{chunk_z:d}..."
            )
            progress.update(result.chunks, total_chunks)
        result.regions += 1
        progress.status(f"Completed region {region_number:d}/{len(region_files):d}")

    progress.status("Writing index.cdb and newindex.cdb...")
    for index in indexes:
        index.write()
    return result


# -------------------------
# Worlds
# -------------------------
@dataclass
class ConversionResult:
    output: Path
    regions: int = 0
    chunks: int = 0
    errors: int = 0
    unmapped: Counter = field(default_factory=Counter)
    substituted: Counter = field(default_factory=Counter)
    unmapped_items: Counter = field(default_factory=Counter)
    unmapped_entities: dict = field(default_factory=dict)
    dimensions: list = field(default_factory=list)


def validate_3ds_world(path: Path) -> Path:
    path = Path(path)
    cdb_directory = path / "db" / "cdb"
    if not (path / "level.dat").is_file() or not cdb_directory.is_dir():
        raise ConversionError(
            f"{path} is not an MC3DS world (it needs level.dat and db/cdb)"
        )
    cdb.load_indexes(cdb_directory)
    if not cdb.list_slot_files(cdb_directory):
        raise ConversionError(f"{cdb_directory} has no slt*.cdb files")
    vdb_directory = path / "db" / "vdb"
    if not any((vdb_directory / name).is_file() for name in ("index.vdb", "newindex.vdb")):
        raise ConversionError(
            f"{path} has no db/vdb index, so the game cannot load it. This happens "
            "when a world is copied while Minecraft still has it open; exit the "
            "world to the title screen (or close the game) and copy it again"
        )
    return cdb_directory


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def convert(
    java_path: Path,
    template_world: Path,
    output_directory: Path,
    dimensions: list[int] | None = None,
    region_dimension: int = 0,
    start_chunk: tuple[int, int] = (0, 0),
    progress: Progress | None = None,
    blocks_json: Path = BLOCKS_JSON,
) -> ConversionResult:
    """
    Converts a Java world (or a single region folder) by inserting it into a
    copy of an existing MC3DS world, saved in ``output_directory``.
    """
    progress = progress or Progress()
    java_path = Path(java_path)
    template_world = Path(template_world)
    output_directory = Path(output_directory)

    validate_3ds_world(template_world)
    region_directories = find_region_directories(java_path)
    if None in region_directories:
        region_directories = {region_dimension: region_directories[None]}
    else:
        level = read_level_data(java_path)
        if level is not None:
            version = level["version_name"] or "unknown version"
            progress.status(
                f"Java world \"{level['name']}\", {version} (DataVersion {level['data_version']})"
            )
    if dimensions is not None:
        region_directories = {
            dimension: directory
            for dimension, directory in region_directories.items()
            if dimension in dimensions
        }
    if not region_directories:
        raise JavaWorldError("the selected dimensions have no region files")

    world_out = output_directory / template_world.name
    if world_out.exists():
        raise ConversionError(f"{world_out} already exists, move or delete it first")
    if world_out.resolve() == template_world.resolve():
        raise ConversionError("the output folder cannot be the MC3DS world itself")

    region_count = sum(len(list_region_files(d)) for d in region_directories.values())
    slot_size = cdb.list_slot_files(template_world / "db" / "cdb")[0].stat().st_size
    slots_per_region = -(-REGION_CHUNKS * REGION_CHUNKS // cdb.SUBFILE_COUNT)
    needed = _directory_size(template_world) + region_count * slots_per_region * slot_size
    output_directory.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output_directory).free
    if free < needed:
        raise ConversionError(
            f"not enough disk space: about {needed / 2**20:.0f} MiB needed, "
            f"{free / 2**20:.0f} MiB free"
        )

    base_mapper = BlockMapper(blocks_json, progress)
    mapper = SimilarBlockMapper(base_mapper)
    item_mapper = ItemMapper(blocks_json)
    entity_mapper = EntityMapper()
    result = ConversionResult(world_out)
    progress.status(f"Copying MC3DS world {template_world.name}...")
    shutil.copytree(template_world, world_out)
    try:
        for dimension, region_directory in sorted(region_directories.items()):
            progress.status(
                f"Converting {DIMENSION_NAMES.get(dimension, dimension)} from {region_directory}"
            )
            inserted = insert_region_directory(
                world_out / "db" / "cdb",
                region_directory,
                mapper,
                start_chunk[0],
                start_chunk[1],
                dimension,
                progress,
                item_mapper,
                entity_mapper,
            )
            result.regions += inserted.regions
            result.chunks += inserted.chunks
            result.errors += inserted.errors
            result.dimensions.append(dimension)
    except BaseException:
        shutil.rmtree(world_out, ignore_errors=True)
        raise

    result.unmapped = mapper.unmapped
    result.substituted = getattr(mapper, "substituted", Counter())
    result.unmapped_items = Counter(item_mapper.unmapped)
    result.unmapped_entities = dict(entity_mapper.unmapped)
    if result.substituted:
        progress.status(
            f"Substituted {len(result.substituted):d} missing block types: "
            + ", ".join(f"{k}×{v}" for k, v in result.substituted.most_common(12))
            + (" ..." if len(result.substituted) > 12 else "")
        )
    if mapper.unmapped:
        progress.warning(
            f"{len(mapper.unmapped):d} Java block states still have no MC3DS equivalent: "
            + ", ".join(name for name, _ in mapper.unmapped.most_common(20))
            + (" ..." if len(mapper.unmapped) > 20 else "")
        )
    if result.unmapped_entities:
        top = sorted(result.unmapped_entities.items(), key=lambda kv: -kv[1])[:12]
        progress.warning(
            "Unmapped entities skipped: " + ", ".join(f"{k}×{v}" for k, v in top)
        )
    if result.unmapped_items:
        top = result.unmapped_items.most_common(12)
        progress.warning(
            "Unmapped chest/entity items skipped: "
            + ", ".join(f"{k}×{v}" for k, v in top)
        )
    return result
