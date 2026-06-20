#!/usr/bin/env python3
"""Export per-neighborhood ASCII maps + tile manifest for Unity Gotchiverse2D."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASCII_MAP_ROOT = REPO_ROOT / "tools" / "ascii-map"
sys.path.insert(0, str(ASCII_MAP_ROOT))

from ascii_map.district_canvas import ROAD_CHAR, parcel_type_char  # noqa: E402
from ascii_map.geometry import parcel_rect  # noqa: E402
from ascii_map.loader import load_district  # noqa: E402
from ascii_map.models import DistrictData, GridCell  # noqa: E402
from ascii_map.roads import get_district_road_rects  # noqa: E402

# Unity paints one 16px tile per on-chain coordinate unit (humble = 8×8 tiles).
UNITY_TILE_UNIT = 1

# Gotchiverse humble autotile IDs (see humble.aseprite / Rule Tile 1–9):
#   5=interior  4=N  3=E  1=S  2=W  9=NW  6=NE  8=SW  7=SE
# Tile 0 = earth (world ground). Internal parcel_border_index → gotchiverse digit.
PARCEL_BORDER_TO_GOTCHI = {
    0: "5",
    1: "4",
    2: "3",
    3: "1",
    4: "2",
    5: "9",
    6: "6",
    7: "8",
    8: "7",
}

# (col, row) on 80×32 parcel sheets — row0: S,W,E,N,interior; row1: NE,SE,SW,NW.
PARCEL_SHEET_TILE = {
    1: (0, 0),  # S
    2: (1, 0),  # W
    3: (2, 0),  # E
    4: (3, 0),  # N
    5: (4, 0),  # interior
    6: (0, 1),  # NE
    7: (1, 1),  # SE
    8: (2, 1),  # SW
    9: (3, 1),  # NW
}

EARTH_CHAR = "0"
ROAD_SYMBOLS = list("!@#$%^&*(")
REASONABLE_SYMBOLS = list("ijklmnopq")
SPACIOUS_SYMBOLS = list("rstuvwxyz")
PAARTNER_SYMBOLS = list("ABCDEFGHI")

TRAIL_FILES = {
    0: "trail0_1.png",
    1: "trailT.png",
    2: "trailR.png",
    3: "trailB.png",
    4: "trailL.png",
    5: "trailC1.png",
    6: "trailC2.png",
    7: "trailC3.png",
    8: "trailC4.png",
}

PARCEL_TYPES = ("humble", "reasonable", "spacious", "paartner")
PARCEL_CHARS = {"humble": "h", "reasonable": "o", "spacious": "s", "paartner": "p"}
PARCEL_TILESET_SOURCE = {
    "humble": "humble",
    "reasonable": "reasonable",
    "spacious": "spacious",
    "paartner": "spacious",
}
UNITY_TILE_SIZE = 16
EMPTY_CHAR = "."


def category_for_char(ch: str) -> str:
    if ch == ".":
        return "empty"
    if ch == "r":
        return "road"
    if ch == "p":
        return "spacious"
    for ptype, pchar in PARCEL_CHARS.items():
        if ch == pchar:
            return ptype
    return "empty"


def counts_as_road_for_trail_autotile(ch: str | None) -> bool:
    """Parcel neighbors are not trail edges — use trail_0 along parcel sides."""
    if ch is None:
        return False
    cat = category_for_char(ch)
    return cat == "road" or cat in PARCEL_TYPES


def road_autotile_index(
    north: str | None,
    east: str | None,
    south: str | None,
    west: str | None,
) -> int:
    """Autotile index for trail cells; index 0 = trail_0 (plain interior)."""

    def same(a: str | None) -> bool:
        return counts_as_road_for_trail_autotile(a)

    need_n = not same(north)
    need_e = not same(east)
    need_s = not same(south)
    need_w = not same(west)

    if need_n and need_w and not need_e and not need_s:
        return 5
    if need_n and need_e and not need_w and not need_s:
        return 6
    if need_s and need_w and not need_e and not need_n:
        return 7
    if need_s and need_e and not need_w and not need_n:
        return 8
    if need_n and not need_s:
        return 1
    if need_s and not need_n:
        return 3
    if need_w and not need_e:
        return 4
    if need_e and not need_w:
        return 2
    return 0


def autotile_index(
    ch: str,
    north: str | None,
    east: str | None,
    south: str | None,
    west: str | None,
) -> int:
    cat = category_for_char(ch)

    def same(a: str | None, b: str) -> bool:
        if a is None:
            return False
        return category_for_char(a) == cat

    need_n = not same(north, ch)
    need_e = not same(east, ch)
    need_s = not same(south, ch)
    need_w = not same(west, ch)

    if cat == "empty" and not (need_n or need_e or need_s or need_w):
        return 0

    if need_n and need_w and not need_e and not need_s:
        return 5
    if need_n and need_e and not need_w and not need_s:
        return 6
    if need_s and need_w and not need_e and not need_n:
        return 7
    if need_s and need_e and not need_w and not need_n:
        return 8
    if need_n and not need_s:
        return 1
    if need_s and not need_n:
        return 3
    if need_w and not need_e:
        return 4
    if need_e and not need_w:
        return 2
    return 0


def parcel_border_index(local_x: int, local_y: int, width: int, height: int) -> int:
    """Border autotile index inside a parcel footprint (8×8 humble, 16×16 reasonable, …)."""
    if width <= 0 or height <= 0:
        return 0
    if width == 1 and height == 1:
        return 0

    on_n = local_y == 0
    on_s = local_y == height - 1
    on_w = local_x == 0
    on_e = local_x == width - 1

    if on_n and on_w:
        return 5
    if on_n and on_e:
        return 6
    if on_s and on_w:
        return 7
    if on_s and on_e:
        return 8
    if on_n:
        return 1
    if on_s:
        return 3
    if on_w:
        return 4
    if on_e:
        return 2
    return 0


def chain_rect_to_cells(
    x: int,
    y: int,
    w: int,
    h: int,
    bounds_min_x: int,
    bounds_min_y: int,
    cols: int,
    rows: int,
    *,
    unit: int = UNITY_TILE_UNIT,
) -> tuple[int, int, int, int]:
    tx0 = max(0, math.floor((x - bounds_min_x) / unit))
    ty0 = max(0, math.floor((y - bounds_min_y) / unit))
    tx1 = min(cols, max(tx0 + 1, math.ceil((x + w - bounds_min_x) / unit)))
    ty1 = min(rows, max(ty0 + 1, math.ceil((y + h - bounds_min_y) / unit)))
    return tx0, ty0, tx1, ty1


def crop_cell_ascii(
    layers,
    cell: GridCell,
    bounds_min_x: int,
    bounds_min_y: int,
) -> tuple[list[list[str]], int, int]:
    cols, rows = layers.width, layers.height
    tx0, ty0, tx1, ty1 = chain_rect_to_cells(
        cell.x,
        cell.y,
        cell.w,
        cell.h,
        bounds_min_x,
        bounds_min_y,
        cols,
        rows,
    )

    logical: list[list[str]] = []
    for ty in range(ty0, ty1):
        row: list[str] = []
        for tx in range(tx0, tx1):
            if layers.hit[ty][tx] == cell.id:
                row.append(layers.chars[ty][tx])
            else:
                row.append(".")
        logical.append(row)

    return logical, tx1 - tx0, ty1 - ty0


def build_unity_tile_layers(data: DistrictData):
    """Logical tile grid at 1 chain unit per Unity tile (humble parcel = 8×8 tiles)."""
    bounds = data.map_config.bounds
    unit = UNITY_TILE_UNIT
    cols = max(1, math.ceil(bounds.width / unit))
    rows = max(1, math.ceil(bounds.height / unit))

    chars = [[EMPTY_CHAR for _ in range(cols)] for _ in range(rows)]
    hit: list[list[str | None]] = [[None for _ in range(cols)] for _ in range(rows)]

    _, road_rects = get_district_road_rects(data.district_id)
    for rect in road_rects:
        tx0, ty0, tx1, ty1 = chain_rect_to_cells(
            rect.x,
            rect.y,
            rect.w,
            rect.h,
            bounds.min_x,
            bounds.min_y,
            cols,
            rows,
            unit=unit,
        )
        for ty in range(ty0, ty1):
            for tx in range(tx0, tx1):
                chars[ty][tx] = ROAD_CHAR

    parcel_to_cell: dict[str, str] = {}
    for cell in data.grid_cells:
        for token_id in cell.parcel_token_ids:
            parcel_to_cell[token_id] = cell.id

    for parcel in data.parcels:
        rect = parcel_rect(
            parcel.coordinate_x,
            parcel.coordinate_y,
            size=parcel.size,
            parcel_type_name=parcel.parcel_type,
            display_x=parcel.display_x,
            display_y=parcel.display_y,
            display_w=parcel.display_w,
            display_h=parcel.display_h,
        )
        ch = parcel_type_char(parcel.parcel_type)
        cell_id = parcel_to_cell.get(parcel.token_id)
        tx0, ty0, tx1, ty1 = chain_rect_to_cells(
            rect.x,
            rect.y,
            rect.w,
            rect.h,
            bounds.min_x,
            bounds.min_y,
            cols,
            rows,
            unit=unit,
        )
        for ty in range(ty0, ty1):
            for tx in range(tx0, tx1):
                chars[ty][tx] = ch
                if cell_id is not None:
                    hit[ty][tx] = cell_id

    return type("TileLayers", (), {
        "width": cols,
        "height": rows,
        "chars": chars,
        "hit": hit,
        "min_x": bounds.min_x,
        "min_y": bounds.min_y,
    })()


def paint_unity_map(data: DistrictData, layers) -> list[list[str]]:
    """Paint parcel borders at full footprint size, then autotile roads."""
    symbols = build_symbol_lookup()
    width, height = layers.width, layers.height
    painted = [[EMPTY_CHAR for _ in range(width)] for _ in range(height)]

    for parcel in data.parcels:
        rect = parcel_rect(
            parcel.coordinate_x,
            parcel.coordinate_y,
            size=parcel.size,
            parcel_type_name=parcel.parcel_type,
            display_x=parcel.display_x,
            display_y=parcel.display_y,
            display_w=parcel.display_w,
            display_h=parcel.display_h,
        )
        ptype = parcel.parcel_type if parcel.parcel_type in PARCEL_TYPES else "humble"
        tx0, ty0, tx1, ty1 = chain_rect_to_cells(
            rect.x,
            rect.y,
            rect.w,
            rect.h,
            layers.min_x,
            layers.min_y,
            width,
            height,
        )
        tw, th = tx1 - tx0, ty1 - ty0
        for local_y in range(th):
            for local_x in range(tw):
                gx, gy = tx0 + local_x, ty0 + local_y
                idx = parcel_border_index(local_x, local_y, tw, th)
                if ptype == "humble":
                    painted[gy][gx] = PARCEL_BORDER_TO_GOTCHI[idx]
                else:
                    painted[gy][gx] = symbols[(ptype, idx)]

    for y in range(height):
        for x in range(width):
            if layers.chars[y][x] != ROAD_CHAR:
                continue
            north = layers.chars[y - 1][x] if y > 0 else None
            south = layers.chars[y + 1][x] if y + 1 < height else None
            west = layers.chars[y][x - 1] if x > 0 else None
            east = layers.chars[y][x + 1] if x + 1 < width else None
            idx = road_autotile_index(north, east, south, west)
            painted[y][x] = symbols[("road", idx)]

    for y in range(height):
        for x in range(width):
            if painted[y][x] == EMPTY_CHAR:
                painted[y][x] = EARTH_CHAR

    return painted


def build_symbol_lookup() -> dict[tuple[str, int], str]:
    lookup: dict[tuple[str, int], str] = {}
    for idx in range(9):
        lookup[("road", idx)] = ROAD_SYMBOLS[idx]
    for idx in range(9):
        lookup[("reasonable", idx)] = REASONABLE_SYMBOLS[idx]
        lookup[("spacious", idx)] = SPACIOUS_SYMBOLS[idx]
        lookup[("paartner", idx)] = PAARTNER_SYMBOLS[idx]
    return lookup


def symbol_order_text() -> str:
    """One ASCII symbol per exported tile PNG (tile_0000 …)."""
    lines: list[str] = [EARTH_CHAR]
    lines.extend(str(d) for d in range(1, 10))
    lines.extend(ROAD_SYMBOLS)
    lines.extend(REASONABLE_SYMBOLS)
    lines.extend(SPACIOUS_SYMBOLS)
    lines.extend(PAARTNER_SYMBOLS)
    return "\n".join(lines) + "\n"


def cell_id_to_slug(cell_id: str) -> str:
    return cell_id.replace(":", "_")


def find_aseprite() -> str | None:
    env_path = os.environ.get("ASEPRITE")
    if env_path and Path(env_path).is_file():
        return env_path

    found = shutil.which("aseprite")
    if found:
        return found

    home = Path.home()
    candidates = [
        home / ".local/bin/aseprite",
        Path("/Applications/Aseprite.app/Contents/MacOS/aseprite"),
        Path("/Applications/Aseprite/Aseprite.app/Contents/MacOS/aseprite"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def export_aseprite_sheet(name: str, sprites_dir: Path, tmp_dir: Path) -> Path:
    src = sprites_dir / f"{name}.aseprite"
    out = tmp_dir / f"{name}.png"
    if not src.exists():
        raise FileNotFoundError(f"Missing aseprite source: {src}")

    aseprite = find_aseprite()
    if not aseprite:
        raise FileNotFoundError(
            "Aseprite CLI not found. Install Aseprite or set ASEPRITE=/path/to/aseprite"
        )

    result = subprocess.run(
        [aseprite, "-b", str(src), "--save-as", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    stderr = (result.stderr or "").strip()
    if stderr and "attempt to index a nil value" not in stderr:
        print(stderr, file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(
            f"Aseprite export failed for {src} (exit {result.returncode}): {stderr}"
        )
    if not out.is_file():
        raise FileNotFoundError(
            f"Aseprite did not create {out}. Command: {aseprite} -b {src} --save-as {out}"
        )
    return out


def slice_tile(sheet: bytes, width: int, height: int, col: int, row: int, tile_size: int = 16):
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(sheet)).convert("RGBA")
    x = col * tile_size
    y = row * tile_size
    return img.crop((x, y, x + tile_size, y + tile_size))


def normalize_tile_image(tile, tile_size: int = UNITY_TILE_SIZE):
    from PIL import Image

    tile = tile.convert("RGBA")
    if tile.size == (tile_size, tile_size):
        return tile
    if tile.width >= tile_size and tile.height >= tile_size:
        if tile.width == tile_size * 2 and tile.height == tile_size * 2:
            return tile.resize((tile_size, tile_size), Image.Resampling.NEAREST)
        left = (tile.width - tile_size) // 2
        top = (tile.height - tile_size) // 2
        return tile.crop((left, top, left + tile_size, top + tile_size))
    return tile.resize((tile_size, tile_size), Image.Resampling.NEAREST)


def slice_parcel_game_tile(sheet: bytes, game_id: int):
    spec = PARCEL_SHEET_TILE[game_id]
    col, row = spec[0], spec[1]
    return slice_tile(sheet, 80, 32, col, row)


def write_tiles(output_tiles: Path, sprites_dir: Path, tmp_dir: Path) -> list[str]:
    from PIL import Image

    output_tiles.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    tile_index = 0

    earth_path = export_aseprite_sheet("earth", sprites_dir, tmp_dir)
    earth = normalize_tile_image(Image.open(earth_path))
    earth.save(output_tiles / f"tile_{tile_index:04d}.png")
    written.append(f"tile_{tile_index:04d}.png")
    tile_index += 1

    humble_sheet = export_aseprite_sheet("humble", sprites_dir, tmp_dir)
    humble_bytes = humble_sheet.read_bytes()
    for game_id in range(1, 10):
        tile = slice_parcel_game_tile(humble_bytes, game_id)
        out_name = f"tile_{tile_index:04d}.png"
        tile.save(output_tiles / out_name)
        written.append(out_name)
        tile_index += 1

    trail_dir = sprites_dir / "trails"
    for idx in range(9):
        src = trail_dir / TRAIL_FILES[idx]
        tile = normalize_tile_image(Image.open(src))
        out_name = f"tile_{tile_index:04d}.png"
        tile.save(output_tiles / out_name)
        written.append(out_name)
        tile_index += 1

    for ptype in ("reasonable", "spacious", "paartner"):
        source_name = PARCEL_TILESET_SOURCE[ptype]
        sheet_path = export_aseprite_sheet(source_name, sprites_dir, tmp_dir)
        if not sheet_path.exists():
            raise FileNotFoundError(f"Missing parcel tileset for {ptype}: {sheet_path}")

        sheet_bytes = sheet_path.read_bytes()
        for game_id in range(1, 10):
            tile = slice_parcel_game_tile(sheet_bytes, game_id)
            out_name = f"tile_{tile_index:04d}.png"
            tile.save(output_tiles / out_name)
            written.append(out_name)
            tile_index += 1

    return written


def write_ascii(path: Path, rows: list[list[str]]) -> None:
    lines = ["".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Unity neighborhood cell maps.")
    parser.add_argument("district_id", type=int, nargs="?", default=43)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output directory (default: ../Gotchiverse2D/Assets/Cells/d43)",
    )
    parser.add_argument(
        "--unity-root",
        type=Path,
        default=REPO_ROOT.parent / "Gotchiverse2D",
        help="Gotchiverse2D project root",
    )
    args = parser.parse_args()

    output_root = args.output or (args.unity_root / "Assets" / "Cells" / f"d{args.district_id}")
    sprites_dir = REPO_ROOT / "data" / "sprites"

    # Keep temp exports outside Unity Assets (Unity may import .tmp and GUI PATH lacks aseprite).
    legacy_tmp = output_root / ".tmp"
    if legacy_tmp.exists():
        shutil.rmtree(legacy_tmp, ignore_errors=True)

    data = load_district(args.district_id, root=REPO_ROOT, require_grid=True)
    layers = build_unity_tile_layers(data)
    bounds = data.map_config.bounds

    shared = output_root / "Shared"
    tiles_dir = shared / "Tiles"
    with tempfile.TemporaryDirectory(prefix="gotchiverse-tiles-") as tmp:
        tile_files = write_tiles(tiles_dir, sprites_dir, Path(tmp))
        (shared / "symbol_order.txt").write_text(symbol_order_text(), encoding="utf-8")

    manifest_cells: list[dict] = []
    painted_full = paint_unity_map(data, layers)
    for cell in data.grid_cells:
        tx0, ty0, tx1, ty1 = chain_rect_to_cells(
            cell.x,
            cell.y,
            cell.w,
            cell.h,
            bounds.min_x,
            bounds.min_y,
            layers.width,
            layers.height,
        )
        w, h = tx1 - tx0, ty1 - ty0
        if w == 0 or h == 0:
            continue
        painted = [row[tx0:tx1] for row in painted_full[ty0:ty1]]
        slug = cell_id_to_slug(cell.id)
        cell_dir = output_root / slug
        cell_dir.mkdir(parents=True, exist_ok=True)
        write_ascii(cell_dir / "map.ascii.txt", painted)
        manifest_cells.append(
            {
                "cellId": cell.id,
                "slug": slug,
                "width": w,
                "height": h,
                "parcelCount": len(cell.parcel_token_ids),
            }
        )

    manifest = {
        "districtId": args.district_id,
        "tilePixelSize": 16,
        "pixelsPerUnit": 16,
        "chainUnitsPerCell": UNITY_TILE_UNIT,
        "tileCount": len(tile_files),
        "cells": manifest_cells,
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Exported {len(manifest_cells)} cell maps to {output_root}")
    print(f"Tiles: {len(tile_files)} in {tiles_dir}")
    print(f"Manifest: {output_root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
