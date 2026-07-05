# OSM2Tiled

Generates **Brigador: Up-Armored Edition** levels (`.tmx` files for the
modkit's Tiled branch) from real-world **OpenStreetMap** data: streets,
buildings, parks, rivers, railways, fences.

The goal is not photographic fidelity but a **playable, coherent** map: a
connected road network, plausible building blocks, spawn/objective/exit
markers placed and connected, and an optional perimeter wall. A manual pass in
Tiled remains expected (and desirable).

```
place / bbox ──► Overpass API ──► tag classification ──► projection (m) ──► auto rotation
                 (disk cache)       OSM → classes          local tmerc        street grid
                                                                                   │
.tmx level ◄── template injection ◄── spawn/objective/exit ◄── tile grid ◄── rasterization
(Tiled modkit)   ALLSTARTER            + wall + corridors      1 cell = N m    roads drawn last
```

## Installation

```bash
pip install -r requirements.txt   # numpy pillow shapely pyproj requests
```

Python ≥ 3.9. No heavy dependencies (no GDAL/geopandas): rasterization is
done with Pillow (1 pixel = 1 tile), OSM multipolygon assembly with Shapely
(`polygonize`).

## Quick start (with the game)

Just give a location — the tool finds the game, the template and the tile
mapping on its own:

```bash
# first run: point the tool at your Brigador install (remembered afterwards
# in ~/.osm2tiled.json; the BRIGADOR_DIR env var and common Steam paths are
# also checked automatically)
python osm2tiled.py generate --place "Story City, Iowa" --brigador-dir "C:/path/to/Brigador"

# from then on, a location is all you need:
python osm2tiled.py generate --place "Montmartre, Paris"
python osm2tiled.py generate --center "48.8867,2.3431"      # GPS point (lat,lon)
python osm2tiled.py generate --bbox -93.61,42.17,-93.55,42.20
```

What gets resolved automatically:
- **template**: `level_ALLSTARTER_usethistobuildnewlevels.tmx` in the game's
  `assets/tiledmaps/`;
- **mapping**: the `mapping.json` shipped with this repo (working GIDs for
  the stock ALLSTARTER tilesets — see below to customize);
- **output**: `assets/tiledmaps/level_<place>.tmx` next to the template
  (never overwrites: a numeric suffix is added if the file exists);
- **covered area**: grid size × scale (120×120 tiles × 6 m/tile = 720 m by
  default; use `--size 200x200` and/or `--meters-per-tile` for more).

Then open the result in the modkit's Tiled, run automapping (step 4 below),
decorate, validate, export.

## Quick start (without the game)

```bash
python osm2tiled.py generate --geojson fixture.geojson \
    --out demo_level.tmx --size auto --preview demo.png
```

Without `--template`, the tool builds a **standalone** template (solid-color
tileset, orthogonal): viewable in a vanilla Tiled to judge the result, **not
loadable by Brigador**.

## Full workflow with the modkit

Steps 1–2 are **optional** when you target the stock ALLSTARTER template:
the repo's `mapping.json` already contains working GIDs for it (derived by
analyzing the game's 36 shipped levels). You only need them for a custom
template, custom tilesets, or a different visual theme.

1. *(optional)* **Inspect the template**:
   ```bash
   python osm2tiled.py inspect-template "Brigador/assets/tiledmaps/level_ALLSTARTER_usethistobuildnewlevels.tmx"
   ```
   The command lists tilesets (`firstgid`), layers, and the **GIDs of the
   markers** already placed on the template's `objectives` layer.

2. *(optional)* **Customize `mapping.json`** starting from
   `mapping.example.json`: for each class (road, grass, wall, buildings by
   footprint size…), the GID of one or more template tiles. In Tiled: click
   a tile → local ID; GID = tileset `firstgid` + local ID.

3. **Generate** (all flags optional — see the quick start above):
   ```bash
   python osm2tiled.py generate --place "Story City, Iowa" \
       --meters-per-tile 6 --size 200x200 --no-border --preview preview.png
   ```
   `--center lat,lon` and `--bbox w,s,e,n` also work. To point at explicit
   files, use `--template "<BRIGADOR>/assets/tiledmaps/level_ALLSTARTER_usethistobuildnewlevels.tmx"`,
   `--mapping mapping.json` and `--out "<BRIGADOR>/assets/tiledmaps/level_x.tmx"`
   (replace `<BRIGADOR>` with your actual game directory).
   **Important: the output `.tmx` must land in `assets/tiledmaps/`** so that
   the template's relative `.tsx` paths resolve and the modkit's Tiled
   displays the assets — omitting `--out` does this automatically.

4. **Run automapping in Tiled** (the modkit's `sjtiled` branch). If your
   mapping uses **automapper marker tiles** (recommended — see below), this
   step is **required**: the markers for prefab houses
   (`automapper_prefabs`) and fences (`automapper`) have no game-side prop
   JSON; the modkit's automapping rules (`rules.txt`) replace them with the
   correctly assembled multi-part structures. Without this pass, the level
   will not load in game.

5. **Manual pass in Tiled**: check the markers, decorate, place enemies and
   turrets (≤ 8!), fix contested areas.

6. **Validate**:
   ```bash
   python osm2tiled.py validate "<BRIGADOR>/assets/tiledmaps/level_story_city_iowa.tmx" --mapping mapping.json
   ```
   Checks: the 4 required layers, consistent dimensions, ≥ 3 markers on
   `objectives`, turret count ≤ 8 (if `turret_gids` is set).

7. **Export to `.json`**: in the modkit's Tiled, `Ctrl+E` to
   `Brigador/assets/levels/`. The export stays manual: Brigador's `.json`
   format is produced by their Tiled branch, and the Tiled pass is needed
   anyway. Then in game: `F1` → LEVEL SELECT.

## Real-world data → tile mapping

| OSM | Class | Rendering |
|---|---|---|
| `highway=motorway…service` | `road` | thickened line (width by type, `lanes`/`width` take precedence), `map` layer |
| `highway=footway/path/cycleway/pedestrian` | `pavement` | thin line or area (`area=yes`) |
| `amenity=parking` | `pavement` | area |
| `building=*` (ways + multipolygon relations) | `building` | filled footprint → tiled with `WxH` catalog props, `props` layer |
| `railway=rail/tram/light_rail` | `rail` | line, `map` layer (tunnels ignored) |
| vegetated `landuse/leisure/natural` | `grass` | area |
| `natural=water`, `waterway=river/canal/stream` | `water` | area / thickened line (+ optional trap on `traps`) |
| `barrier=wall/fence/hedge` | `wall` | 1-tile line, `props` layer |
| `node natural=tree` | `tree` | 1 props tile (+ optional random density in grass) |

Composition priorities: **roads are drawn last** and "carve through" buildings
and walls → the network stays traversable; where a street crosses the river,
the road wins (implicit flat bridge).

## Scale and simplification

1. **Projection**: WGS84 → local Transverse Mercator centered on the bbox
   (pyproj), i.e. **meters** with negligible distortion at neighborhood scale.
2. **Auto rotation**: length-weighted histogram of road segment bearings
   modulo 90° → the dominant street grid is aligned with the tile grid axes
   (disable with `--rotate 0`, or set a manual angle). An axis-aligned grid
   rasterizes much more cleanly than an oblique one.
3. **Grid**: `cell (cx, cy) = ((x − minx)/M, (maxy − y)/M)` with `M =
   --meters-per-tile`. Geometry is simplified *by construction*: polygons
   filled and lines thickened at tile-pixel resolution (Pillow), rounded
   joints, one-cell holes in buildings filled.
4. **Final size**: empty margins trimmed, then **centered** crop/pad to the
   template's dimensions (`--size template`, default), `auto`, or `WxH`.

### Choosing the scale

`--meters-per-tile` × grid size = real-world coverage. Guidelines from
practice:

- **6 m/tile** (the default) works well: a typical house occupies a
  2×2-cell footprint (one complete house prefab), roads are 2-4 cells wide
  and legible. One tile ≈ 36 m² ≈ 388 sq ft.
- `--max-extent` defaults to grid size × scale, so the fetched area always
  matches what the grid can hold. Override it only to fetch less than that.
- At coarse scales (≥ 12 m/tile), override `road_widths_m` in the mapping —
  otherwise every road rounds down to the 1-tile minimum and the hierarchy
  becomes unreadable.

## mapping.json reference

See `mapping.example.json` for a commented skeleton. All keys:

### `tiles` — `map` layer (ground)

| Key | Meaning |
|---|---|
| `ground`, `grass`, `water`, `pavement`, `road`, `rail` | list of GIDs, one picked at random per cell (seeded) |
| `road_edge_nw/ne/sw/se` | straight road edge tiles, placed on road cells whose NW/NE/SW/SE side borders non-road |
| `road_corner_n/e/w/s` | curved corner tiles, placed where two adjacent sides are exposed (bends), and as inner corners where a single non-road diagonal touches the cell (curb wrapping a block corner at junctions) |
| `road_crosswalk_x/y` | crosswalk tile pairs painted on the first rank of each road arm entering a junction (junctions are detected via road run lengths: long in both axes = core, long in one = arm) |

The edge/corner orientation convention (isometric): NW edge ↔ neighbor
`(x−1, y)`, NE ↔ `(y−1)`, SE ↔ `(x+1)`, SW ↔ `(y+1)`. The default
assignments in `mapping.example.json` were derived by scanning the game's 36
shipped levels and extracting each tile's dominant neighbor pattern.

### `props` — `props` layer

| Key | Meaning |
|---|---|
| `wall` | list of GIDs; **one style is chosen per connected run** so a single fence never mixes styles. Use `automapper` marker tiles so the modkit connects them properly |
| `tree` | scattered on OSM tree nodes and via `tree_density_in_grass` |
| `decor` | scattered via `decor_density_in_grass` (farm equipment, statues, rubble…) |
| `car` | scattered on road cells via `car_density_on_road` |
| `rail` | prop placed on every rail cell — use the automapper `track` marker (expanded into connected track pieces by the modkit's automapping) |
| `building_catalog` | `{"WxH": [gids]}` — building footprints are greedily tiled with the largest fitting entries. Anchor = bottom-left corner. **Prefer complete-building tiles or automapper prefabs** (e.g. `automapper_prefabs` house markers); tiles that are corner *parts* of multi-tile assemblies look broken when placed alone |
| `building_catalog_industrial` / `building_catalog_cemetery` | same format; used for buildings inside `landuse=industrial` / `landuse=cemetery` zones (e.g. modular warehouses, crypt mausoleums), falling back to `building_catalog` |
| `decor_park` / `decor_cemetery` / `decor_industrial` | zone-specific decor pools (park furniture, gravestones, industrial clutter) scattered with their own densities inside `leisure=park/garden`, cemetery and industrial polygons |

### `markers` — `objectives` layer

- `mode: "preserve-template"`: keeps the template's `objectives` layer as-is
  (its markers are already valid for the game) and makes their surroundings
  walkable. Requires the output size to match the template; otherwise the
  tool falls back to `auto` automatically.
- `mode: "auto"`: places `spawn` (southmost road), `exit` (road cell farthest
  from spawn, near the border), `objective` (road cell nearest the center) on
  the largest connected road component, using the `spawn`/`objective`/`exit`
  GID lists.

### Densities and extras

| Key | Meaning |
|---|---|
| `tree_density_in_grass` | probability per grass/ground cell (0–1) |
| `decor_density_in_grass` | same, for `decor` props (outside zones) |
| `decor_density_park/cemetery/industrial` | zone decor densities; zone decor is scattered before generic trees/decor and takes precedence inside its polygons |
| `car_density_on_road` | probability per free road cell |
| `roadside_fence_chance` | probability per continuous roadside run (≥ 8 cells) of becoming a fence |
| `road_widths_m` | overrides the default per-`highway=*` widths (meters) |
| `min_road_width_tiles` | roads never rasterize thinner than this (default 2; a post-raster pass also widens the staircase corners of diagonal streets) |
| `traps.water` | optional trap GID placed on every water cell (`traps` layer) |
| `turret_gids`, `max_turrets` | used by `validate` to enforce the 8-turret limit |

## Gameplay layer

- `spawn` / `objective` / `exit` markers on the largest connected road
  component (see `markers` above);
- perimeter wall around the map (open where roads touch the border),
  disable with `--no-border`;
- spawn↔objective↔exit connectivity check (BFS) with emergency corridors
  carved if needed;
- properties written into the map: **OpenStreetMap ODbL attribution**, source
  bbox, rotation, scale.

## Known limitations (manual pass expected)

- **Turrets: 8 maximum** per map, or the game crashes on load. The tool never
  places any; `validate` counts the ones you place (via `turret_gids`).
- **GIDs must be filled in by hand once**: the game's `.tsx` files are not
  redistributable, so the mapping is your local configuration.
- **Elevation, bridges, tunnels, floors**: everything is flattened.
  `tunnel=yes` (rail) is ignored; bridges become flat crossings; OSM
  `layer=*` is not interpreted.
- **Buildings**: greedy rectangular tiling → very jagged or oblique shapes
  become orthogonal blocks; cells with no compatible catalog entry are
  dropped (keep a small entry in the catalog, or accept the gaps as yards).
- **Auto markers**: placed on the road network, not "at the door" of a
  specific building — move them in Tiled if the objective should be a
  building.
- **Size / memory**: `--max-cells` guard (1M cells). Brigador also has a
  global memory budget for game data — stay reasonable on size and prop
  count.
- **OSM data**: quality varies by neighborhood; exotic multipolygon
  relations are occasionally unassemblable (counted and skipped).
- **No water or rail tiles** exist in the game's `t_alltiles` tileset (no
  shipped level has any); map them to the closest ground tone instead.

## Data sources & licenses

- **OpenStreetMap** via the **Overpass API**: © OpenStreetMap contributors,
  **ODbL 1.0** license. Attribution is automatically written into every
  generated map's properties; keep it if you publish the level. Overpass
  responses are cached on disk (`--cache-dir`) to avoid hammering the server;
  the endpoint is configurable (`--overpass-url`).
- **Nominatim** (geocoding for `--place`): identifying User-Agent, occasional
  use, per their usage policy.
- **Brigador assets**: the game's tilesets, sprites and templates belong to
  Stellar Jockeys and are **not** included in this repository. You need your
  own copy of the game and its modkit.
