"""
Convert Java Edition NBT tags to plain Python (nested compounds/lists kept).

The stock ``nbt`` library exposes TAG_Compound with both ``.keys()`` and a
``.tags`` *list* of child tags. Prefer the dict interface so compounds become
``{name: value}`` rather than a list of anonymous values (which made
``as_dict`` return ``{}`` and wiped chest Items / sign TextN).
"""
from __future__ import annotations

from typing import Any


def nbt_to_python(value: Any) -> Any:
    if value is None:
        return None

    # Already plain
    if isinstance(value, (bool, int, float, str, bytes, bytearray)):
        return value
    if isinstance(value, dict):
        return {str(k): nbt_to_python(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [nbt_to_python(v) for v in value]

    # nbtlib typed scalars (String, Int, …) — prefer .unpack() / .value
    unpack = getattr(value, "unpack", None)
    if callable(unpack):
        try:
            return nbt_to_python(unpack())
        except Exception:
            pass

    # nbt.TAG_Compound is dict-like (keys/__getitem__) even though it also has
    # a .tags list of child TAG_* objects. Always prefer the mapping API.
    if hasattr(value, "keys") and hasattr(value, "__getitem__"):
        try:
            keys = list(value.keys())
        except Exception:
            keys = None
        if keys is not None:
            # TAG_List also has no useful keys(); real compounds have named keys
            # TAG_List in some libs may implement keys oddly — require that
            # indexed access by name works for at least one key.
            try:
                return {str(k): nbt_to_python(value[k]) for k in keys}
            except Exception:
                pass

    # nbt.TAG_List — .tags is a list of elements (not a name→tag map)
    tags = getattr(value, "tags", None)
    if isinstance(tags, list):
        # Compound that failed keys() still has named children in .tags
        if tags and all(hasattr(t, "name") for t in tags):
            names = [getattr(t, "name", None) for t in tags]
            if all(n is not None and n != "" for n in names):
                # Could be a compound stored only as tag list
                if len(set(names)) == len(names):
                    return {str(t.name): nbt_to_python(t) for t in tags}
        return [nbt_to_python(t) for t in tags]
    if isinstance(tags, dict):
        return {str(k): nbt_to_python(v) for k, v in tags.items()}

    # Scalar nbt tag with .value
    if hasattr(value, "value"):
        inner = value.value
        # Compound mistakenly exposing value=None
        if inner is None and hasattr(value, "keys"):
            try:
                return {str(k): nbt_to_python(value[k]) for k in value.keys()}
            except Exception:
                pass
        return nbt_to_python(inner)

    return str(value)


def as_dict(tag: Any) -> dict:
    result = nbt_to_python(tag)
    return result if isinstance(result, dict) else {}
