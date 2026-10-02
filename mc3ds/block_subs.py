"""
Map Java blocks missing from MC3DS to similar blocks that exist in blocks.json.

Pipeline:
  1. Stock BlockMapper (blocks.json + optional PyMCTranslate)
  2. If still air: explicit substitution table
  3. Heuristics (waxed_/stripped_/deepslate_ prefixes, wood families, etc.)
  4. Last resort: stone (never silent air for non-air sources)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent / "data"

AIR_NAMES = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air", "air", "cave_air", "void_air"}

# Suffix → oak/cobble equivalents for unknown wood/stone variants
_SUFFIX_FALLBACKS = (
    ("_planks", "oak_planks"),
    ("_log", "oak_log"),
    ("_wood", "oak_wood"),
    ("_leaves", "oak_leaves"),
    ("_sapling", "oak_sapling"),
    ("_stairs", "oak_stairs"),
    ("_slab", "oak_slab"),
    ("_fence_gate", "oak_fence_gate"),
    ("_fence", "oak_fence"),
    ("_door", "oak_door"),
    ("_trapdoor", "oak_trapdoor"),
    ("_button", "oak_button"),
    ("_pressure_plate", "oak_pressure_plate"),
    ("_sign", "sign"),
    ("_wall_sign", "wall_sign"),
    ("_wall_hanging_sign", "wall_sign"),
    ("_hanging_sign", "sign"),
    ("_wall", "cobblestone_wall"),
    ("_carpet", "white_carpet"),
    ("_concrete_powder", "white_concrete_powder"),
    ("_concrete", "white_concrete"),
    ("_terracotta", "terracotta"),
    ("_glazed_terracotta", "white_glazed_terracotta"),
    ("_stained_glass_pane", "glass_pane"),
    ("_stained_glass", "glass"),
    ("_wool", "white_wool"),
    ("_shulker_box", "purple_shulker_box"),
    ("_bed", "red_bed"),
    ("_banner", "white_banner"),
    ("_wall_banner", "white_wall_banner"),
    ("_candle_cake", "cake"),
    ("_candle", "torch"),
    ("_ore", "stone"),
)


def _format_block_name(name: str, properties: dict) -> str:
    if properties:
        joined = ",".join(f"{key}={value}" for key, value in sorted(properties.items()))
        return f"{name}[{joined}]"
    return name


class SimilarBlockMapper:
    """Wraps chunker BlockMapper; remaps unknowns to similar MC3DS blocks."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.substituted: Counter = Counter()
        self.still_missing: Counter = Counter()
        path = DATA / "block_substitutions.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        self._subs = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}
        # Valid bare names present in the inverse map
        self._known: set[str] = set()
        for key in getattr(inner, "inverse", {}):
            bare = key.split("[", 1)[0]
            if bare.startswith("minecraft:"):
                bare = bare[10:]
            self._known.add(bare)

    @property
    def unmapped(self) -> Counter:
        # Expose remaining hard misses (after substitution)
        return self.still_missing

    @property
    def legacy(self):
        return self._inner.legacy

    def _resolve_name(self, bare: str) -> str | None:
        bare = bare.lower().strip()
        if bare in AIR_NAMES or bare in ("air",):
            return "air"
        if bare in self._subs:
            return self._subs[bare]
        # strip waxed_
        if bare.startswith("waxed_"):
            return self._resolve_name(bare[6:])
        # strip stripped_
        if bare.startswith("stripped_"):
            return self._resolve_name(bare[9:])
        # deepslate_X → try X
        if bare.startswith("deepslate_"):
            return self._resolve_name(bare[10:])
        # potted_X → flower_pot
        if bare.startswith("potted_"):
            return "flower_pot"
        # hanging_X_sign
        if "hanging_sign" in bare:
            return "sign" if "wall" not in bare else "wall_sign"
        # wall_torch variants
        if bare.endswith("_wall_torch"):
            return "wall_torch"
        if bare.endswith("_torch") and bare != "redstone_torch":
            return "torch"
        # coloured candles already in table; generic
        if bare.endswith("_candle"):
            return "torch"
        # family suffix fallbacks
        for suffix, target in _SUFFIX_FALLBACKS:
            if bare.endswith(suffix) and bare != target:
                if target in self._known or target in self._subs.values():
                    return target
        # coral / dead coral leftovers
        if "coral" in bare:
            return "prismarine" if "dead" not in bare else "stone"
        if bare.endswith("_cauldron"):
            return "cauldron"
        if bare.endswith("_head") or bare.endswith("_skull"):
            return "skeleton_skull" if "wall" not in bare else "skeleton_wall_skull"
        return None

    def _lookup_substituted(self, name: str, properties: dict) -> tuple[int, int] | None:
        """Find (id,data) for a substituted bare name, preserving facing/axis/half."""
        full = f"minecraft:{name}" if not name.startswith("minecraft:") else name
        bare = full.split(":", 1)[-1]
        if bare in ("air", "cave_air", "void_air"):
            return (0, 0)
        lookup = getattr(self._inner, "lookup_state", None)
        if callable(lookup):
            hit = lookup(full, properties)
            if hit is not None:
                return hit
            return None
        # Fallback if inner has no fuzzy lookup
        if properties:
            key = _format_block_name(full, properties)
            hit = self._inner.inverse.get(key)
            if hit is not None:
                return hit
        for candidate in (full, f"minecraft:{bare}", bare):
            hit = self._inner.inverse.get(candidate)
            if hit is not None:
                return hit
        return None

    def map(self, block, data_version: int) -> tuple[int, int]:
        result = self._inner.map(block, data_version)
        name = f"{block.namespace}:{block.id}"
        if name in AIR_NAMES or block.name() in AIR_NAMES:
            return (0, 0)
        if result != (0, 0):
            return result

        properties = {str(k): str(v) for k, v in block.properties.items()}
        bare = block.id
        alt = self._resolve_name(bare)
        if alt:
            hit = self._lookup_substituted(alt, properties)
            if hit is not None:
                self.substituted[f"{bare}→{alt}"] += 1
                return hit

        # last resort: stone instead of air
        hit = self._lookup_substituted("stone", {})
        if hit is not None:
            self.substituted[f"{bare}→stone"] += 1
            return hit

        self.still_missing[name] += 1
        return (0, 0)

    def lookup_tables(self, palette: list, data_version: int):
        import numpy as np

        ids = np.zeros(len(palette), dtype=np.uint8)
        data = np.zeros(len(palette), dtype=np.uint8)
        for i, block in enumerate(palette):
            bid, meta = self.map(block, data_version)
            ids[i] = bid & 0xFF
            data[i] = meta & 0xF
        return ids, data
