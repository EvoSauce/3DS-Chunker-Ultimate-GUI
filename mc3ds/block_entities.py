"""
Java tile entities -> MC3DS block-entity compounds.

Dump-backed field names / types (mc3ds_code_dump.bin, MC_BUILD=new):
  BlockEntity::create @ 0x172a80 reads id (string) then x/y/z via getInt
    (0x6a1038) — TAG_INT only; wrong type -> position (0,0,0).
  Base load @ 0x173640: isMovable (byte), CustomName (string).
  Chest @ 0x30c984: pairx/pairz (int), Items[{Slot:byte,id:short,Damage:short,Count:byte}]
  BrewingStand @ 0x459658: Items, CookTime (short)
  PistonArm @ 0x372d48: Progress/LastProgress (float), State/NewState (byte), Sticky (byte)
  Type mismatch destroy @ 0x6c812c / 0xa1076c when block vs BE type disagree.

Block entity ``id`` must be the Bedrock save name (\"Chest\"), not \"minecraft:chest\".
Idle piston bases (IDs 29/33) expect a PistonArm BE (type 0x12); omit → broken use.
"""
from __future__ import annotations

from typing import Any

from .items import ItemMapper, sign_line_to_plain

# Java id / namespaced id -> Bedrock save id string
BLOCK_ENTITY_IDS = {
    "minecraft:chest": "Chest",
    "chest": "Chest",
    "Chest": "Chest",
    "minecraft:trapped_chest": "Chest",
    "trapped_chest": "Chest",
    "minecraft:ender_chest": "EnderChest",
    "ender_chest": "EnderChest",
    "EnderChest": "EnderChest",
    "minecraft:furnace": "Furnace",
    "furnace": "Furnace",
    "Furnace": "Furnace",
    "minecraft:blast_furnace": "Furnace",
    "minecraft:smoker": "Furnace",
    "minecraft:hopper": "Hopper",
    "hopper": "Hopper",
    "Hopper": "Hopper",
    "minecraft:dropper": "Dropper",
    "dropper": "Dropper",
    "Dropper": "Dropper",
    "minecraft:dispenser": "Dispenser",
    "dispenser": "Dispenser",
    "Dispenser": "Dispenser",
    "minecraft:brewing_stand": "BrewingStand",
    "brewing_stand": "BrewingStand",
    "BrewingStand": "BrewingStand",
    "minecraft:sign": "Sign",
    "minecraft:wall_sign": "Sign",
    "minecraft:oak_sign": "Sign",
    "minecraft:oak_wall_sign": "Sign",
    "sign": "Sign",
    "Sign": "Sign",
    "minecraft:mob_spawner": "MobSpawner",
    "minecraft:spawner": "MobSpawner",
    "mob_spawner": "MobSpawner",
    "MobSpawner": "MobSpawner",
    "minecraft:item_frame": "ItemFrame",
    "item_frame": "ItemFrame",
    "ItemFrame": "ItemFrame",
    "minecraft:glow_item_frame": "ItemFrame",
    "minecraft:skull": "Skull",
    "minecraft:skeleton_skull": "Skull",
    "skull": "Skull",
    "Skull": "Skull",
    "minecraft:flower_pot": "FlowerPot",
    "flower_pot": "FlowerPot",
    "FlowerPot": "FlowerPot",
    "minecraft:enchanting_table": "EnchantTable",
    "enchanting_table": "EnchantTable",
    "EnchantTable": "EnchantTable",
    "minecraft:beacon": "Beacon",
    "beacon": "Beacon",
    "Beacon": "Beacon",
    "minecraft:daylight_detector": "DaylightDetector",
    "daylight_detector": "DaylightDetector",
    "minecraft:comparator": "Comparator",
    "comparator": "Comparator",
    "minecraft:note_block": "Music",
    "music": "Music",
    "Music": "Music",
    # New 3DS: PistonBlock sets Block+0x18 = 0x12 for both idle bases (IDs 29/33).
    # Emit PistonArm with idle Progress/State defaults — omitting the BE breaks use.
    "minecraft:piston": "PistonArm",
    "minecraft:sticky_piston": "PistonArm",
    "piston": "PistonArm",
    "sticky_piston": "PistonArm",
    "minecraft:moving_piston": "PistonArm",
    "minecraft:piston_head": "PistonArm",
    "PistonArm": "PistonArm",
    "MovingBlock": "MovingBlock",
    "minecraft:command_block": "CommandBlock",
    "minecraft:chain_command_block": "CommandBlock",
    "minecraft:repeating_command_block": "CommandBlock",
    "command_block": "CommandBlock",
    "CommandBlock": "CommandBlock",
    "minecraft:banner": "Banner",
    "banner": "Banner",
    "Banner": "Banner",
    "minecraft:structure_block": "StructureBlock",
    "structure_block": "StructureBlock",
    "minecraft:end_gateway": "EndGateway",
    "end_gateway": "EndGateway",
    "minecraft:end_portal": "EndPortal",
    "minecraft:jukebox": "Jukebox",
    "jukebox": "Jukebox",
    "minecraft:shulker_box": "ShulkerBox",
    "minecraft:undyed_shulker_box": "ShulkerBox",
    "ShulkerBox": "ShulkerBox",
    # Java-only containers → nearest PE block entity (block remapped via substitutions)
    "minecraft:barrel": "Chest",
    "barrel": "Chest",
    "minecraft:cauldron": "Cauldron",
    "cauldron": "Cauldron",
    "Cauldron": "Cauldron",
    "minecraft:bed": "Bed",
    "bed": "Bed",
    "Bed": "Bed",
}

# Colored shulker boxes / beds
for _color in (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
):
    BLOCK_ENTITY_IDS[f"minecraft:{_color}_shulker_box"] = "ShulkerBox"
    BLOCK_ENTITY_IDS[f"minecraft:{_color}_bed"] = "Bed"

CONTAINER_IDS = {
    "Chest", "EnderChest", "Hopper", "Dropper", "Dispenser", "Furnace",
    "BrewingStand", "ShulkerBox", "Jukebox",
}


def _be_id(raw: Any) -> str | None:
    """
    Map a Java tile-entity id to a known Bedrock save name.
    Unknown ids return None — inventing PascalCase names (e.g. Observer, Barrel)
    writes BEs the game rejects (type mismatch / no GUI), same class of bug as
    chests that would not open when x/y/z tag types were wrong.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    if s in BLOCK_ENTITY_IDS:
        return BLOCK_ENTITY_IDS[s]
    bare = s[10:] if s.startswith("minecraft:") else s
    if bare in BLOCK_ENTITY_IDS:
        return BLOCK_ENTITY_IDS[bare]
    pascal = "".join(p.title() for p in bare.split("_"))
    if pascal in BLOCK_ENTITY_IDS:
        return BLOCK_ENTITY_IDS[pascal]
    # Colored beds etc.: minecraft:red_bed → Bed
    if bare.endswith("_bed") or bare == "bed":
        return "Bed"
    if bare.endswith("_banner") or bare.endswith("_wall_banner"):
        return "Banner"
    if bare.endswith("_shulker_box"):
        return "ShulkerBox"
    if bare.endswith("_sign") or bare.endswith("_wall_sign"):
        return "Sign"
    if bare.endswith("_skull") or bare.endswith("_head"):
        return "Skull"
    return None


def _items_list(entity: dict, mapper: ItemMapper) -> list[dict]:
    items = entity.get("Items") or entity.get("items") or []
    if not isinstance(items, list):
        return []
    out = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("Slot", entry.get("slot"))
        converted = mapper.convert_stack(entry, int(slot) if slot is not None else None)
        if converted:
            out.append(converted)
    return out


def convert_block_entity(entity: dict, x: int, y: int, z: int, mapper: ItemMapper) -> dict | None:
    """
    Full conversion including nested Items. Returns None if the type is unknown
    or must not be written (e.g. idle piston).
    """
    raw_id = entity.get("id") or entity.get("Id") or entity.get("identifier") or ""
    be_id = _be_id(raw_id)
    if not be_id:
        return None

    result: dict[str, Any] = {
        "id": be_id,
        "x": int(x),
        "y": int(y),
        "z": int(z),
        "isMovable": 1,
    }

    if be_id == "Sign":
        front = entity.get("front_text") or {}
        messages = front.get("messages") if isinstance(front, dict) else None
        if isinstance(messages, list) and len(messages) >= 4:
            for i in range(4):
                result[f"Text{i + 1}"] = sign_line_to_plain(messages[i])
        else:
            for i in range(1, 5):
                result[f"Text{i}"] = sign_line_to_plain(entity.get(f"Text{i}", ""))
        return result

    if be_id == "ItemFrame":
        # BlockEntityType 0x11 on 3DS (0x10 is Cauldron)
        item = entity.get("Item") or entity.get("item")
        if isinstance(item, dict):
            converted = mapper.convert_stack(item)
            if converted:
                result["Item"] = converted
        if "ItemRotation" in entity:
            result["ItemRotation"] = int(entity["ItemRotation"])
        if "ItemDropChance" in entity:
            result["ItemDropChance"] = float(entity["ItemDropChance"])
        return result

    if be_id in ("PistonArm", "MovingBlock"):
        # Facing lives in block metadata only — not NBT (dump: sub_372d48).
        if "Progress" in entity:
            result["Progress"] = float(entity["Progress"])
        else:
            result["Progress"] = 0.0
        if "LastProgress" in entity:
            result["LastProgress"] = float(entity["LastProgress"])
        else:
            result["LastProgress"] = float(result["Progress"])
        result["State"] = int(entity.get("State", entity.get("state", 0)))
        result["NewState"] = int(
            entity.get("NewState", entity.get("newState", result["State"]))
        )
        sticky = entity.get("Sticky", entity.get("sticky", False))
        if isinstance(sticky, str):
            sticky = sticky.lower() in ("1", "true", "sticky")
        # Infer sticky from id when Java TE omitted Sticky
        raw = str(
            entity.get("id") or entity.get("Id") or entity.get("identifier") or ""
        )
        if not sticky and "sticky" in raw.lower():
            sticky = True
        result["Sticky"] = 1 if sticky else 0
        return result

    if be_id in CONTAINER_IDS or "Items" in entity:
        items = _items_list(entity, mapper)
        if items:
            result["Items"] = items

    if be_id == "Furnace":
        if "BurnTime" in entity:
            result["BurnTime"] = int(entity["BurnTime"])
        if "CookTime" in entity:
            result["CookTime"] = int(entity["CookTime"])
        if "CookTimeTotal" in entity and "CookTime" not in result:
            result["CookTime"] = int(entity.get("CookTime", 0))

    if be_id == "Hopper" and "TransferCooldown" in entity:
        result["TransferCooldown"] = int(entity["TransferCooldown"])

    if be_id == "BrewingStand":
        # PE reads CookTime (short) only — no Fuel getter in this build.
        if "BrewTime" in entity:
            result["CookTime"] = int(entity["BrewTime"])
        elif "CookTime" in entity:
            result["CookTime"] = int(entity["CookTime"])
        else:
            result["CookTime"] = 0

    if be_id == "EnchantTable":
        return result

    if be_id == "CommandBlock":
        cmd = entity.get("Command") or entity.get("command") or ""
        result["Command"] = str(cmd)
        if "CustomName" in entity:
            result["CustomName"] = sign_line_to_plain(entity["CustomName"])
        if "SuccessCount" in entity:
            result["SuccessCount"] = int(entity["SuccessCount"])
        if "TrackOutput" in entity:
            result["TrackOutput"] = int(bool(entity["TrackOutput"]))
        if "powered" in entity or "auto" in entity:
            result["auto"] = int(bool(entity.get("auto", False)))
            result["powered"] = int(bool(entity.get("powered", False)))

    if be_id == "MobSpawner":
        spawn = entity.get("SpawnData") or {}
        if isinstance(spawn, dict):
            entity_tag = spawn.get("entity") or spawn
            eid = entity_tag.get("id") or entity_tag.get("Id")
            if eid:
                result["EntityIdentifier"] = str(eid)
        if "Delay" in entity:
            result["Delay"] = int(entity["Delay"])
        if "MinSpawnDelay" in entity:
            result["MinSpawnDelay"] = int(entity["MinSpawnDelay"])
        if "MaxSpawnDelay" in entity:
            result["MaxSpawnDelay"] = int(entity["MaxSpawnDelay"])
        if "SpawnCount" in entity:
            result["SpawnCount"] = int(entity["SpawnCount"])

    if be_id == "Skull":
        if "SkullType" in entity:
            result["SkullType"] = int(entity["SkullType"])
        if "Rot" in entity:
            result["Rot"] = int(entity["Rot"])

    if be_id == "FlowerPot":
        if "Item" in entity:
            pot_item = entity["Item"]
            if isinstance(pot_item, str):
                _iid, meta = mapper.lookup(pot_item)
                result["item"] = str(pot_item)
                result["mData"] = int(entity.get("Data", meta))
            elif isinstance(pot_item, (int, float)):
                result["item"] = int(pot_item)
                result["mData"] = int(entity.get("Data", 0))

    if be_id == "Beacon":
        for key in ("Levels", "Primary", "Secondary"):
            if key in entity:
                result[key] = int(entity[key])

    if be_id == "Banner":
        if "Base" in entity:
            result["Base"] = int(entity["Base"])
        if "Patterns" in entity and isinstance(entity["Patterns"], list):
            result["Patterns"] = entity["Patterns"]

    if be_id == "Jukebox" and "RecordItem" in entity and isinstance(entity["RecordItem"], dict):
        rec = mapper.convert_stack(entity["RecordItem"])
        if rec:
            result["RecordItem"] = rec

    if be_id == "Comparator" and "OutputSignal" in entity:
        result["OutputSignal"] = int(entity["OutputSignal"])

    if "CustomName" in entity and be_id != "CommandBlock":
        result["CustomName"] = sign_line_to_plain(entity["CustomName"])

    for key in ("pairx", "pairz", "Findable"):
        if key in entity and isinstance(entity[key], (int, bool)):
            result[key] = int(entity[key])

    return result


def stub_block_entity(be_id: str, x: int, y: int, z: int, *, sticky: bool = False) -> dict:
    """Minimal PE block entity when Java had the block but no TileEntity NBT."""
    out: dict[str, Any] = {
        "id": be_id,
        "x": int(x),
        "y": int(y),
        "z": int(z),
        "isMovable": 1,
    }
    if be_id == "BrewingStand":
        out["CookTime"] = 0
    if be_id == "Furnace":
        out["BurnTime"] = 0
        out["CookTime"] = 0
    if be_id == "Hopper":
        out["TransferCooldown"] = 0
    if be_id == "Sign":
        for i in range(1, 5):
            out[f"Text{i}"] = ""
    if be_id == "PistonArm":
        out["Progress"] = 0.0
        out["LastProgress"] = 0.0
        out["State"] = 0
        out["NewState"] = 0
        out["Sticky"] = 1 if sticky else 0
    if be_id == "Comparator":
        out["OutputSignal"] = 0
    if be_id == "Music":
        out["note"] = 0
    if be_id in CONTAINER_IDS and be_id not in ("Jukebox", "EnderChest"):
        # Empty Items list is fine — UI still opens with a real BE type
        pass
    return out


# PE numeric block id → block-entity save name.
# ONLY blocks with hasBlockEntity() (Block+0x18 != 0). Observer is 218 and has
# NO BE — never map it to ShulkerBox (shulkers are 219–234).
BLOCK_ID_TO_BE: dict[int, str] = {
    23: "Dispenser",
    25: "Music",
    26: "Bed",
    29: "PistonArm",  # sticky_piston
    33: "PistonArm",  # piston
    52: "MobSpawner",
    54: "Chest",
    61: "Furnace",
    62: "Furnace",
    63: "Sign",
    68: "Sign",
    84: "Jukebox",
    116: "EnchantTable",
    117: "BrewingStand",
    118: "Cauldron",
    130: "EnderChest",
    137: "CommandBlock",
    138: "Beacon",
    140: "FlowerPot",
    144: "Skull",
    146: "Chest",  # trapped
    149: "Comparator",
    150: "Comparator",
    151: "DaylightDetector",
    154: "Hopper",
    158: "Dropper",
    176: "Banner",
    177: "Banner",
    178: "DaylightDetector",  # inverted
    255: "StructureBlock",
}
for _shulker_id in range(219, 235):
    BLOCK_ID_TO_BE[_shulker_id] = "ShulkerBox"
