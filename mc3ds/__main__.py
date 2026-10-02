# released under MIT license

import sys
from pathlib import Path
import shutil
import json

if __package__ is None:
    print(
        'Please run this as a module, use "python -m mc3ds" instead of "python __main__.py"',
        file=sys.stderr,
    )
    sys.exit(1)

import click

from .classes import *
from .parser import parser
from .convert import convert


@click.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "-o",
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default="out",
    help="Directory for output",
)
@click.option(
    "-c",
    "--convert",
    "mode",
    flag_value="convert",
    default=True,
    help="Convert a 3DS world to a Java world",
)
@click.option(
    "-x",
    "--extract",
    "mode",
    flag_value="extract",
    help="Extract data into separate files for analysis (for developers)",
)
@click.option(
    "-j",
    "--java-to-3ds",
    "mode",
    flag_value="java",
    help="Convert a Java world or region folder (PATH) into a copy of an MC3DS world (--target)",
)
@click.option(
    "-t",
    "--target",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="MC3DS world to insert the Java chunks into (it is copied, not modified)",
)
@click.option(
    "--dimension",
    type=click.IntRange(0, 2),
    default=0,
    help="Dimension for a bare region folder (0 overworld, 1 nether, 2 end)",
)
@click.option(
    "--offset",
    type=(int, int),
    default=(0, 0),
    help="Block X/Z where Java (0,0) is placed in the MC3DS world (snapped down to a chunk)",
)
@click.option(
    "--chunk-offset",
    type=(int, int),
    default=None,
    help="Legacy: chunk X/Z offset (overrides --offset if set)",
)
@click.option(
    "-b",
    "--blank-world",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to a blank Minecraft Java world",
    default=Path(__file__).parent / "worlds" / "blankWorld",
)
@click.option(
    "-w",
    "--world-out",
    type=click.Path(file_okay=False, path_type=Path),
    help="Path to the output world",
    default=Path(__file__).parent.parent / "World",
)
@click.option(
    "--delete-out",
    is_flag=True,
    help="Permanently delete the output and world folders, and their contents (make sure they're the right folders!)",
)
def main(
    path: Path,
    out: Path,
    mode: str,
    blank_world: Path,
    world_out: Path,
    target: Path | None = None,
    dimension: int = 0,
    offset: tuple[int, int] = (0, 0),
    chunk_offset: tuple[int, int] | None = None,
    delete_out: bool = False,
) -> None:
    import time

    start_time = time.time()
    script_path = Path(__file__).parent
    if mode == "java":
        from .to3ds import convert as convert_to_3ds

        if target is None:
            raise click.UsageError("--target is required with --java-to-3ds")
        if chunk_offset is not None:
            start_chunk = chunk_offset
        else:
            start_chunk = (offset[0] // 16, offset[1] // 16)
        result = convert_to_3ds(
            path, target, out, region_dimension=dimension, start_chunk=start_chunk
        )
        total_time = time.time() - start_time
        print(
            f"wrote {result.chunks:d} chunks from {result.regions:d} regions to {result.output}"
            f" in {int(total_time // 60):d}:{total_time % 60:.2f}"
        )
        if result.errors:
            print(f"{result.errors:d} chunks could not be read", file=sys.stderr)
            sys.exit(2)
        return

    if out.exists() and not delete_out:
        print(
            'already extracted, please move or delete the "out" folder',
            file=sys.stderr,
        )
        sys.exit(1)

    world = World(path)
    print(f"World name: {world.name}")
    if mode == "convert":
        convert(world, blank_world, world_out, delete_out)
        total_time = time.time() - start_time
        minutes = int(total_time // 60)
        seconds = total_time % 60
        print(f"conversion time is {minutes:d}:{seconds:.2f}")
    elif mode == "extract":
        if out.exists() and delete_out:
            if (out / "3dschunker.txt").is_file():
                shutil.rmtree(out)
            else:
                raise ValueError(
                    "out directory doesn't seem to be valid, not deleting it"
                )
        out.mkdir()
        # make a blank file
        with open(out / "3dschunker.txt", "x") as marker:
            pass

        vdb_out = out / "vdb"
        vdb_out.mkdir()
        for number, vdb_file in world.vdb:
            region_path = vdb_out / f"region{number:d}"
            region_path.mkdir()
            for index, data in vdb_file:
                try:
                    name = data.name.decode().replace("\0", "")
                except UnicodeDecodeError:
                    name = "None"
                filename = name
                nbt_path = region_path / filename
                n = 1
                while nbt_path.exists():
                    filename = f"{filename}{n:d}"
                    nbt_path = region_path / filename
                    n += 1
                nbt_metadata_path = region_path / f"{filename}.json"
                with open(nbt_metadata_path, "x") as subfile_metadata_out:
                    json.dump(
                        {
                            "unknown0": f"0x{data.unknown0:X}",
                            "unknown1": f"0x{data.unknown1:X}",
                            "unknown2": f"0x{data.unknown2:X}",
                        },
                        subfile_metadata_out,
                    )
                with open(nbt_path, "xb") as subfile_out:
                    subfile_out.write(data.raw)

        cdb_out = out / "cdb"
        cdb_out.mkdir()
        for number, cdb_file in world.cdb:
            region_path = cdb_out / f"region{number:d}"
            region_path.mkdir()
            for index, chunk in cdb_file:
                chunk_path = region_path / f"chunk{index:d}"
                chunk_path.mkdir()

                for subchunk_index, subchunk in chunk:
                    subchunk_path = chunk_path / f"data{subchunk_index:d}"
                    if subchunk_index == 0:
                        block_data, heightmap, biomes = subchunk.data
                        for block_index, block in enumerate(block_data):
                            block_path = chunk_path / f"blocks{block_index:d}"
                            with open(block_path, "xb") as block_data_out:
                                block_data_out.write(block)
                        heightmap_path = chunk_path / "heightmap"
                        biomes_path = chunk_path / "biomes"
                        with open(heightmap_path, "xb") as heightmap_out:
                            heightmap_out.write(heightmap)
                        with open(biomes_path, "xb") as biomes_out:
                            biomes_out.write(bytes(biomes))
                    else:
                        with open(subchunk_path, "xb") as subchunk_out:
                            subchunk_out.write(subchunk.raw_decompressed)
            print(f"extracted region {number:d}!")


if __name__ == "__main__":
    main()

# Licensed under the MIT License
# Copyright (c) 2024 Anonymous941
# See the LICENSE file for more information.
