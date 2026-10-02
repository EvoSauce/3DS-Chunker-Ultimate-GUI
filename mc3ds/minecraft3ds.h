// See cdb.py for the canonical description of this format.
#define MAGIC_CDB 0xABCDEF98
#define MAGIC_VDB 0xABCDEF99

// the first subfile has a file header and the rest have garbage here
struct FileHeader {
    // both are always 1
    uint16 something0;
    uint16 something1;
    uint32 subfileCount; // total number of subfiles
    uint32 dataOffset; // offset of subfile 0, always 0x14 (the header size)
    uint32 subfileSize; // size of each subfile
    uint32 unknown0; // 0x4 for CDB, 0x100 for VDB
};

struct SubfileHeader {
    uint32 magic;
};

struct Position {
    uint32 x : 14; // offset binary, chunk X + 0x2000
    uint32 z : 14; // offset binary, chunk Z + 0x2000
    uint32 dimension : 4; // unsigned
};

struct ChunkParameters {
    int8 unknown0;
    int8 unknown1;
};

struct ChunkSection {
    int32 index; // -1 = empty
    int32 position; // -1 = empty; offset from the start of the subfile (including the magic)
    int32 compressedSize; // 0 = empty
    int32 decompressedSize; // 0 = empty
};

struct ChunkHeader {
    Position position;
    ChunkParameters parameters;
    uint16 unknown0;
    uint16 unknown1;
    uint16 unknown2;
    ChunkSection sections[6];
};

struct VDBHeader {
    uint8 parameters[8];
    uint32 magic;
    uint8 nameSize;
    uint8 list[7];

    uint32 unknown0;
    char name[nameSize];
    uint16 unknown1;
    uint16 unknown2; // 0x1 for map, 0x0 otherwise
}

struct IndexPointer {
    uint32 slot; // a slot (slt*.cdb) number that contains chunks
};

struct CDBEntry {
    Position position;
    uint16 slot; // slot (corresponds to a CDB file)
    uint16 subfile; // subfile within the slot
    uint16 constant0; // always 0x20FF
    uint16 constant1; // always 0xA
    ChunkParameters parameters; // also in the chunk; usually 0x1, sometimes 0x2 or 0x3, and on large worlds as high as 0x6e
    uint16 constant2; // always 0x8000, subfile count?
};

struct Index {
    uint32 constant0; // always 0x2
    uint32 entryCount;
    uint32 chunkCapacity;
    uint32 entrySize;
    uint32 pointerCount;
    uint32 constant1;
    IndexPointer pointers[pointerCount];
    CDBEntry entries[entryCount];
};

struct TerrainHeader {
    uint8 subchunks;
};
