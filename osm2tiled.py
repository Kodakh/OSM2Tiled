#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSM2Tiled — Generates Brigador levels (.tmx files for the modkit's Tiled
branch) from OpenStreetMap data (Overpass API) or a local GeoJSON file.

Pipeline:
  1. fetch    : bbox / place name -> OSM data (Overpass, disk cache) or GeoJSON
  2. classify : OSM tags -> semantic classes (road, building, grass, water, rail, wall, tree...)
  3. project  : WGS84 (degrees) -> meters (local Transverse Mercator projection)
  4. rotate   : auto-align the dominant street grid with the tile grid axes
  5. raster   : geometries -> tile grids (1 cell = N meters), roads drawn last
  6. gameplay : spawn / objective / exit gate, perimeter wall, connection corridors
  7. tmx      : injection into the level_ALLSTARTER template (map/props/traps/objectives layers)

Data license: (c) OpenStreetMap contributors, ODbL — attribution is written
into the generated map's properties.

Quick usage:
  python osm2tiled.py inspect-template ALLSTARTER.tmx
  python osm2tiled.py generate --bbox -118.268,34.041,-118.256,34.051 \
      --template level_ALLSTARTER_usethistobuildnewlevels.tmx \
      --mapping mapping.json --out level_dtla.tmx --preview dtla.png
  python osm2tiled.py validate level_dtla.tmx --mapping mapping.json
"""

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import random
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw
from pyproj import CRS, Transformer
from shapely import affinity
from shapely.geometry import (LineString, MultiLineString, MultiPolygon, Point,
                              Polygon, box, shape)
from shapely.ops import linemerge, polygonize, transform as shp_transform, unary_union

TOOL = "osm2tiled"
VERSION = "0.1.0"
GID_MASK = 0x1FFFFFFF  # mask for Tiled's flip bits

# --------------------------------------------------------- ground classes ---
G_VOID, G_GROUND, G_GRASS, G_WATER, G_PAVE, G_ROAD, G_RAIL = 0, 1, 2, 3, 4, 5, 6
GROUND_NAMES = {G_GROUND: "ground", G_GRASS: "grass", G_WATER: "water",
                G_PAVE: "pavement", G_ROAD: "road", G_RAIL: "rail"}
# ----------------------------------------------------------- prop classes ---
P_NONE, P_BUILDING, P_WALL, P_TREE, P_DECOR = 0, 1, 2, 3, 4

WALKABLE = {G_GROUND, G_GRASS, G_PAVE, G_ROAD, G_RAIL}  # props excluded
ROADLIKE = {G_ROAD, G_PAVE}

# Road widths in meters, per highway=* value (overridden by lanes/width tags)
ROAD_WIDTHS_M = {
    "motorway": 16, "motorway_link": 8, "trunk": 14, "trunk_link": 7,
    "primary": 12, "primary_link": 6, "secondary": 10, "secondary_link": 5,
    "tertiary": 8, "tertiary_link": 4, "residential": 7, "unclassified": 7,
    "living_street": 6, "service": 4, "track": 3.5, "road": 6,
}
PAVE_WIDTHS_M = {"pedestrian": 5, "footway": 2.5, "path": 2.5, "cycleway": 2.5, "steps": 2.5}
RAIL_TYPES = {"rail", "light_rail", "tram", "subway", "narrow_gauge"}
WATERWAY_WIDTHS_M = {"river": 12, "canal": 8, "stream": 3}

DEFAULT_MAPPING = {
    "_comment": "GIDs = global tile ids as shown in Tiled (map/props layers of the ALLSTARTER template). Lists = variants picked at random (seeded).",
    "tiles": {  # 'map' layer
        "ground": [1], "grass": [2], "water": [3],
        "pavement": [4], "road": [5], "rail": [6],
    },
    "props": {  # 'props' layer
        "wall": [7],
        "tree": [8],
        "building_catalog": {"1x1": [9]},  # add "2x2": [..], "3x2": [..] etc.
    },
    "markers": {  # 'objectives' layer
        "mode": "auto",          # auto | preserve-template | objects
        "spawn": [0], "objective": [0], "exit": [0],
    },
    "traps": {"water": []},      # optional: trap gid placed on every water cell
    "turret_gids": [],           # gids counted by 'validate' (Brigador limit: 8)
    "road_widths_m": {},         # overrides ROAD_WIDTHS_M, e.g. {"residential": 9}
    "tree_density_in_grass": 0.0,
    "max_turrets": 8,
}

def log(msg):
    print(f"[{TOOL}] {msg}", file=sys.stderr)

def warn(msg):
    print(f"[{TOOL}] ⚠ {msg}", file=sys.stderr)

# ------------------------------------------------- zero-config discovery ----

CONFIG_PATH = Path.home() / ".osm2tiled.json"
TEMPLATE_NAME = "level_ALLSTARTER_usethistobuildnewlevels.tmx"

def load_user_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_user_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        warn(f"Could not save {CONFIG_PATH}: {e}")

def find_template(brigador_dir=None):
    """Locates the ALLSTARTER template. Search order: --brigador-dir, the
    BRIGADOR_DIR env var, the saved config, then common Steam paths."""
    cands = []
    if brigador_dir:
        cands.append(Path(brigador_dir))
    env = os.environ.get("BRIGADOR_DIR")
    if env:
        cands.append(Path(env))
    saved = load_user_config().get("brigador_dir")
    if saved:
        cands.append(Path(saved))
    for steam in ("C:/Program Files (x86)/Steam", "C:/Program Files/Steam",
                  "D:/Steam", "D:/SteamLibrary", "E:/SteamLibrary",
                  Path.home() / ".steam/steam", Path.home() / ".local/share/Steam"):
        cands.append(Path(steam) / "steamapps/common/Brigador")
    for c in cands:
        for p in (c / "assets/tiledmaps" / TEMPLATE_NAME, c / TEMPLATE_NAME):
            if p.exists():
                return p
    return None

def slugify(s, maxlen=40):
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(s))
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:maxlen] or "map"

def unique_path(p):
    """Avoids clobbering an existing file: level_x.tmx -> level_x_02.tmx…"""
    if not p.exists():
        return p
    for i in range(2, 100):
        q = p.with_name(f"{p.stem}_{i:02d}{p.suffix}")
        if not q.exists():
            return q
    return p

# ============================================================== 1. FETCH ====

def parse_bbox(s):
    """'west,south,east,north' in decimal degrees."""
    try:
        w, s_, e, n = (float(x) for x in s.split(","))
    except Exception:
        raise SystemExit("--bbox expects west,south,east,north (lon,lat,lon,lat)")
    if not (w < e and s_ < n):
        raise SystemExit("invalid bbox: requires west<east and south<north")
    return (w, s_, e, n)

def geocode_place(name):
    """Nominatim (usage policy: identifying User-Agent, ~1 req/s)."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": name, "format": "json", "limit": 1},
        headers={"User-Agent": f"{TOOL}/{VERSION} (Brigador map generation)"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise SystemExit(f"Place not found via Nominatim: {name!r}")
    s, n, w, e = (float(x) for x in data[0]["boundingbox"])
    log(f"Geocoded {name!r} -> bbox {w:.5f},{s:.5f},{e:.5f},{n:.5f} ({data[0].get('display_name','')})")
    return (w, s, e, n)

def clamp_bbox_extent(bbox, max_extent_m):
    """Crops the bbox around its center if it exceeds max_extent_m."""
    w, s, e, n = bbox
    lat0 = (s + n) / 2.0
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    dx, dy = (e - w) * m_per_deg_lon, (n - s) * m_per_deg_lat
    if dx <= max_extent_m and dy <= max_extent_m:
        return bbox
    warn(f"bbox of {dx:.0f}×{dy:.0f} m cropped to {max_extent_m:.0f} m around its center "
         f"(use --max-extent to change)")
    cx, cy = (w + e) / 2.0, (s + n) / 2.0
    hx = min(dx, max_extent_m) / 2.0 / m_per_deg_lon
    hy = min(dy, max_extent_m) / 2.0 / m_per_deg_lat
    return (cx - hx, cy - hy, cx + hx, cy + hy)

def overpass_query(bbox):
    w, s, e, n = bbox
    bb = f"({s},{w},{n},{e})"
    sel = "\n".join([
        f'way["highway"]{bb};',
        f'way["building"]{bb};', f'relation["building"]{bb};',
        f'way["railway"]{bb};',
        f'way["landuse"~"^(grass|forest|meadow|recreation_ground|cemetery|village_green|orchard)$"]{bb};',
        f'relation["landuse"~"^(grass|forest|meadow|recreation_ground|cemetery|village_green)$"]{bb};',
        f'way["leisure"~"^(park|garden|pitch|playground|golf_course|common)$"]{bb};',
        f'relation["leisure"~"^(park|garden|golf_course)$"]{bb};',
        f'way["natural"~"^(water|wood|scrub|grassland)$"]{bb};',
        f'relation["natural"="water"]{bb};',
        f'way["waterway"~"^(river|canal|stream|riverbank)$"]{bb};',
        f'way["amenity"="parking"]{bb};',
        f'way["barrier"~"^(wall|fence|hedge|retaining_wall)$"]{bb};',
        f'node["natural"="tree"]{bb};',
    ])
    return f"[out:json][timeout:120];(\n{sel}\n);\nout geom;"

def fetch_overpass(bbox, cache_dir, endpoint="https://overpass-api.de/api/interpreter"):
    q = overpass_query(bbox)
    cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(q.encode()).hexdigest()[:16]
    cache = cache_dir / f"overpass_{key}.json"
    if cache.exists():
        log(f"Overpass cache: {cache}")
        return json.loads(cache.read_text(encoding="utf-8"))
    log("Querying Overpass…")
    for attempt in range(3):
        r = requests.post(endpoint, data={"data": q},
                          headers={"User-Agent": f"{TOOL}/{VERSION}"}, timeout=180)
        if r.status_code in (429, 502, 504):
            wait = 15 * (attempt + 1)
            warn(f"Overpass {r.status_code}, retrying in {wait}s…")
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        cache.write_text(json.dumps(data), encoding="utf-8")
        log(f"{len(data.get('elements', []))} OSM elements received (cache: {cache})")
        return data
    raise SystemExit("Overpass unavailable after 3 attempts (retry later or provide --geojson).")

# ---------------------------------------------- OSM json -> geometries ------

def _way_coords(el):
    return [(pt["lon"], pt["lat"]) for pt in el.get("geometry", [])]

def _assemble_multipolygon(rel):
    """Assembles an OSM multipolygon (outer/inner roles) via polygonize."""
    outers, inners = [], []
    for m in rel.get("members", []):
        if m.get("type") != "way" or "geometry" not in m:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in m["geometry"]]
        if len(coords) < 2:
            continue
        (outers if m.get("role") != "inner" else inners).append(LineString(coords))
    def rings(lines):
        if not lines:
            return []
        merged = linemerge(MultiLineString(lines)) if len(lines) > 1 else lines[0]
        return [p for p in polygonize(merged) if p.is_valid and p.area > 0]
    outer_polys, inner_polys = rings(outers), rings(inners)
    if not outer_polys:
        return None
    geom = unary_union(outer_polys)
    if inner_polys:
        geom = geom.difference(unary_union(inner_polys))
    return geom if not geom.is_empty else None

def _parse_num(v):
    try:
        return float(str(v).split(";")[0].replace("m", "").replace(",", ".").strip())
    except Exception:
        return None

def classify(tags, geom_is_closed):
    """OSM tags -> list of semantic features (cls, geom_kind, width_m).
    geom_kind: 'poly' | 'line' | 'point'."""
    out = []
    hw = tags.get("highway")
    if hw:
        if tags.get("area") == "yes" and geom_is_closed:
            out.append(("pavement", "poly", None))
        elif hw in PAVE_WIDTHS_M:
            out.append(("pavement", "line", PAVE_WIDTHS_M[hw]))
        elif hw in ROAD_WIDTHS_M:
            width = ROAD_WIDTHS_M[hw]
            lanes = _parse_num(tags.get("lanes"))
            if lanes:
                width = max(width, lanes * 3.5)
            w_tag = _parse_num(tags.get("width"))
            if w_tag:
                width = w_tag
            out.append(("road", "line", width))
    if "building" in tags and tags.get("building") != "no":
        out.append(("building", "poly", None))
    rw = tags.get("railway")
    if rw in RAIL_TYPES and tags.get("tunnel") not in ("yes", "true"):
        out.append(("rail", "line", 3.0))
    if tags.get("natural") == "water" or tags.get("waterway") == "riverbank" \
            or tags.get("landuse") in ("reservoir", "basin"):
        out.append(("water", "poly", None))
    ww = tags.get("waterway")
    if ww in WATERWAY_WIDTHS_M:
        out.append(("water", "line", WATERWAY_WIDTHS_M[ww]))
    if tags.get("landuse") in ("grass", "forest", "meadow", "recreation_ground",
                               "cemetery", "village_green", "orchard") \
            or tags.get("leisure") in ("park", "garden", "pitch", "playground",
                                       "golf_course", "common") \
            or tags.get("natural") in ("wood", "scrub", "grassland"):
        out.append(("grass", "poly", None))
    if tags.get("amenity") == "parking":
        out.append(("pavement", "poly", None))
    if tags.get("barrier") in ("wall", "fence", "hedge", "retaining_wall"):
        out.append(("wall", "line", 1.0))
    if tags.get("natural") == "tree":
        out.append(("tree", "point", None))
    return out

def features_from_overpass(data):
    """-> list of dicts {cls, kind, width_m, geom (WGS84)}"""
    feats, skipped = [], 0
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        t = el.get("type")
        if t == "node":
            for cls, kind, w in classify(tags, False):
                if kind == "point":
                    feats.append(dict(cls=cls, kind=kind, width_m=w,
                                      geom=Point(el["lon"], el["lat"])))
        elif t == "way":
            coords = _way_coords(el)
            if len(coords) < 2:
                continue
            closed = coords[0] == coords[-1] and len(coords) >= 4
            for cls, kind, w in classify(tags, closed):
                try:
                    if kind == "poly":
                        if not closed:
                            skipped += 1
                            continue
                        g = Polygon(coords)
                        if not g.is_valid:
                            g = g.buffer(0)
                    else:
                        g = LineString(coords)
                    if not g.is_empty:
                        feats.append(dict(cls=cls, kind=kind, width_m=w, geom=g))
                except Exception:
                    skipped += 1
        elif t == "relation" and tags.get("type") in ("multipolygon", None, "boundary"):
            for cls, kind, w in classify(tags, True):
                if kind != "poly":
                    continue
                g = _assemble_multipolygon(el)
                if g is not None:
                    feats.append(dict(cls=cls, kind="poly", width_m=w, geom=g))
                else:
                    skipped += 1
    if skipped:
        warn(f"{skipped} OSM geometrie(s) skipped (could not be assembled)")
    return feats

def features_from_geojson(path):
    """GeoJSON FeatureCollection: 'properties' are interpreted as OSM tags."""
    fc = json.loads(Path(path).read_text(encoding="utf-8"))
    feats = []
    for f in fc.get("features", []):
        tags = f.get("properties") or {}
        g = shape(f["geometry"])
        closed = g.geom_type in ("Polygon", "MultiPolygon")
        for cls, kind, w in classify(tags, closed):
            if kind == "poly" and g.geom_type in ("Polygon", "MultiPolygon"):
                feats.append(dict(cls=cls, kind="poly", width_m=w, geom=g))
            elif kind == "line" and g.geom_type in ("LineString", "MultiLineString"):
                feats.append(dict(cls=cls, kind="line", width_m=w, geom=g))
            elif kind == "point" and g.geom_type == "Point":
                feats.append(dict(cls=cls, kind="point", width_m=w, geom=g))
    return feats

# ================================================= 2. PROJECTION / ROTATION =

def project_features(feats, bbox):
    """WGS84 -> local Transverse Mercator centered on the bbox (units: meters)."""
    w, s, e, n = bbox
    lon0, lat0 = (w + e) / 2.0, (s + n) / 2.0
    crs = CRS.from_proj4(f"+proj=tmerc +lat_0={lat0} +lon_0={lon0} +k=1 "
                         f"+x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs")
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    fwd = lambda x, y, z=None: tr.transform(x, y)
    proj = []
    for f in feats:
        g = shp_transform(fwd, f["geom"])
        if not g.is_empty:
            proj.append({**f, "geom": g})
    clip = shp_transform(fwd, box(w, s, e, n))
    return proj, clip

def dominant_bearing(feats):
    """Dominant bearing (deg, mod 90) of road segments, weighted by length."""
    hist = np.zeros(90)
    for f in feats:
        if f["cls"] != "road" or f["kind"] != "line":
            continue
        cs = list(f["geom"].coords)
        for (x1, y1), (x2, y2) in zip(cs, cs[1:]):
            L = math.hypot(x2 - x1, y2 - y1)
            if L < 1:
                continue
            a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 90.0
            hist[int(a) % 90] += L
    if hist.sum() == 0:
        return 0.0
    # light circular smoothing
    k = np.array([0.25, 0.5, 1.0, 0.5, 0.25])
    sm = np.convolve(np.tile(hist, 3), k, mode="same")[90:180]
    return float(np.argmax(sm))

def rotate_features(feats, clip, angle_deg):
    if abs(angle_deg) < 0.5:
        return feats, clip
    origin = clip.centroid
    rot = [{**f, "geom": affinity.rotate(f["geom"], -angle_deg, origin=origin)} for f in feats]
    return rot, affinity.rotate(clip, -angle_deg, origin=origin)

# ======================================================= 3. RASTERIZATION ===

class Grid:
    def __init__(self, clip, meters_per_tile):
        minx, miny, maxx, maxy = clip.bounds
        self.mpt = meters_per_tile
        self.minx, self.maxy = minx, maxy
        self.w = max(1, int(math.ceil((maxx - minx) / meters_per_tile)))
        self.h = max(1, int(math.ceil((maxy - miny) / meters_per_tile)))

    def to_px(self, x, y):
        return ((x - self.minx) / self.mpt, (self.maxy - y) / self.mpt)

def _draw_poly(draw, grid, geom, val):
    polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for p in polys:
        ext = [grid.to_px(x, y) for x, y in p.exterior.coords]
        if len(ext) >= 3:
            draw.polygon(ext, fill=val)
        for ring in p.interiors:
            pts = [grid.to_px(x, y) for x, y in ring.coords]
            if len(pts) >= 3:
                draw.polygon(pts, fill=0)

def _draw_line(draw, grid, geom, val, width_px):
    lines = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
    w = max(1, int(round(width_px)))
    r = w / 2.0
    for ln in lines:
        pts = [grid.to_px(x, y) for x, y in ln.coords]
        if len(pts) >= 2:
            draw.line(pts, fill=val, width=w)
        for (px, py) in pts:  # rounded joints
            draw.ellipse([px - r, py - r, px + r, py + r], fill=val)

def _mask(grid):
    img = Image.new("L", (grid.w, grid.h), 0)
    return img, ImageDraw.Draw(img)

def rasterize(feats, grid, rng, mapping):
    """-> ground[h,w] (G_* classes), props[h,w] (P_* classes), building_mask, trees"""
    order = ["grass", "water", "pavement", "road", "rail"]
    layers = {}
    for cls in order + ["building", "wall"]:
        layers[cls] = _mask(grid)
    tree_pts = []
    for f in feats:
        cls = f["cls"]
        if cls == "tree":
            px, py = grid.to_px(f["geom"].x, f["geom"].y)
            tree_pts.append((int(px), int(py)))
            continue
        if cls not in layers:
            continue
        img, draw = layers[cls]
        if f["kind"] == "poly":
            _draw_poly(draw, grid, f["geom"], 1)
        else:
            width_px = (f["width_m"] or grid.mpt) / grid.mpt
            _draw_line(draw, grid, f["geom"], 1, width_px)

    to_np = lambda cls: (np.asarray(layers[cls][0], dtype=np.uint8) > 0)
    ground = np.full((grid.h, grid.w), G_GROUND, dtype=np.uint8)
    for cls, gid in [("grass", G_GRASS), ("water", G_WATER), ("pavement", G_PAVE),
                     ("road", G_ROAD), ("rail", G_RAIL)]:
        ground[to_np(cls)] = gid

    building = to_np("building")
    building = _fill_pinholes(building)
    wall = to_np("wall")

    # Roads "carve through": guarantees network connectivity.
    carve = np.isin(ground, (G_ROAD, G_PAVE, G_RAIL))
    building &= ~carve
    wall &= ~carve
    building = _drop_specks(building, min_cells=1)

    props = np.zeros_like(ground)
    props[wall] = P_WALL
    props[building] = P_BUILDING
    for (x, y) in tree_pts:
        if 0 <= x < grid.w and 0 <= y < grid.h and props[y, x] == P_NONE \
                and ground[y, x] in (G_GROUND, G_GRASS):
            props[y, x] = P_TREE
    fence_chance = float(mapping.get("roadside_fence_chance", 0.0) or 0.0)
    if fence_chance > 0:
        _roadside_fences(ground, props, rng, fence_chance)
    dens = float(mapping.get("tree_density_in_grass", 0.0) or 0.0)
    if dens > 0:
        gy, gx = np.where(((ground == G_GRASS) | (ground == G_GROUND)) & (props == P_NONE))
        for i in range(len(gx)):
            if rng.random() < dens:
                props[gy[i], gx[i]] = P_TREE
    decor_dens = float(mapping.get("decor_density_in_grass", 0.0) or 0.0)
    if decor_dens > 0:
        gy, gx = np.where(((ground == G_GRASS) | (ground == G_GROUND)) & (props == P_NONE))
        for i in range(len(gx)):
            if rng.random() < decor_dens:
                props[gy[i], gx[i]] = P_DECOR
    return ground, props, building

def _roadside_fences(ground, props, rng, chance, min_run=8):
    """Fences along roads: continuous runs of grass/ground cells bordering the
    road network become P_WALL with probability `chance` per run (placed as
    automapper markers -> connected by the modkit's automapping rules)."""
    h, w = ground.shape
    road = (ground == G_ROAD)
    cand = ((ground == G_GRASS) | (ground == G_GROUND)) & (props == P_NONE)
    adj = np.zeros_like(road)
    adj[1:, :] |= road[:-1, :]
    adj[:-1, :] |= road[1:, :]
    adj[:, 1:] |= road[:, :-1]
    adj[:, :-1] |= road[:, 1:]
    cand &= adj
    for y in range(h):                      # horizontal runs
        x = 0
        while x < w:
            if cand[y, x]:
                x0 = x
                while x < w and cand[y, x]:
                    x += 1
                if x - x0 >= min_run and rng.random() < chance:
                    props[y, x0 + 1:x - 1] = P_WALL
            else:
                x += 1
    for x in range(w):                      # vertical runs
        y = 0
        while y < h:
            if cand[y, x]:
                y0 = y
                while y < h and cand[y, x]:
                    y += 1
                if y - y0 >= min_run and rng.random() < chance:
                    props[y0 + 1:y - 1, x] = P_WALL
            else:
                y += 1

def _fill_pinholes(mask):
    """Fills one-cell holes inside buildings (>=7 filled neighbors)."""
    m = mask.astype(np.uint8)
    p = np.pad(m, 1)
    neigh = sum(p[1 + dy:1 + dy + m.shape[0], 1 + dx:1 + dx + m.shape[1]]
                for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0))
    return (m | ((m == 0) & (neigh >= 7))).astype(bool)

def _drop_specks(mask, min_cells=1):
    return mask  # placeholder: isolated 1x1 cells remain valid (1x1 props)

# ==================================================== 4. GAMEPLAY / MARKERS =

def _components(mask):
    """4-connected components. -> labels[h,w], sizes (dict)."""
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    sizes, cur = {}, 0
    for sy in range(h):
        for sx in range(w):
            if mask[sy, sx] and labels[sy, sx] == 0:
                cur += 1
                stack = [(sx, sy)]
                labels[sy, sx] = cur
                n = 0
                while stack:
                    x, y = stack.pop()
                    n += 1
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < w and 0 <= ny < h and mask[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = cur
                            stack.append((nx, ny))
                sizes[cur] = n
    return labels, sizes

def _carve_corridor(ground, props, a, b, half=1):
    """L-shaped corridor (road) between two cells, razing props on the way."""
    (x1, y1), (x2, y2) = a, b
    cells = [(x, y1) for x in range(min(x1, x2), max(x1, x2) + 1)]
    cells += [(x2, y) for y in range(min(y1, y2), max(y1, y2) + 1)]
    h, w = ground.shape
    for (x, y) in cells:
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    ground[ny, nx] = G_ROAD
                    props[ny, nx] = P_NONE

def _clear_around(ground, props, x, y, r=2, make=G_GROUND):
    h, w = ground.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                props[ny, nx] = P_NONE
                if ground[ny, nx] == G_WATER or ground[ny, nx] == G_VOID:
                    ground[ny, nx] = make

def place_gameplay(ground, props, template_markers, mapping, border_wall=True):
    """Places spawn / objective / exit, perimeter wall, and ensures connectivity.
    -> markers {'spawn':(x,y), 'objective':(x,y), 'exit':(x,y)}, mode"""
    h, w = ground.shape
    mode = (mapping.get("markers") or {}).get("mode", "auto")
    markers = {}

    if mode == "preserve-template" and template_markers:
        # keep the template's objectives layer untouched; just make the
        # marker locations walkable and connected.
        for gid, x, y in template_markers:
            if 0 <= x < w and 0 <= y < h:
                _clear_around(ground, props, x, y, r=2)
        pts = [(x, y) for _, x, y in template_markers if 0 <= x < w and 0 <= y < h]
        _connect_points(ground, props, pts)
        return {}, mode

    roadlike = np.isin(ground, tuple(ROADLIKE))
    if roadlike.sum() < 20:
        warn("Very little road network detected: markers placed at default positions.")
        markers = {"spawn": (w // 2, h - 3), "objective": (w // 2, h // 2),
                   "exit": (w // 2, 2)}
        for k, (x, y) in markers.items():
            _clear_around(ground, props, x, y, r=2)
        _connect_points(ground, props, list(markers.values()))
    else:
        labels, sizes = _components(roadlike)
        main = max(sizes, key=sizes.get)
        ys, xs = np.where(labels == main)
        # spawn: southmost road cell (max y), preferring central x
        i_spawn = np.lexsort((np.abs(xs - w // 2), -ys))[0]
        spawn = (int(xs[i_spawn]), int(ys[i_spawn]))
        # exit: road cell farthest from spawn (BFS),
        # preferably near the map border (exit *gate*)
        dist = _bfs_dist(roadlike & (labels == main), spawn)
        b = max(2, min(w, h) // 20)
        near_border = np.zeros_like(dist, dtype=bool)
        near_border[:b, :] = near_border[-b:, :] = True
        near_border[:, :b] = near_border[:, -b:] = True
        cand = (dist >= 0) & near_border
        pool = np.where(cand, dist, -1) if cand.any() else np.where(dist >= 0, dist, -1)
        far = np.unravel_index(np.argmax(pool), dist.shape)
        exit_ = (int(far[1]), int(far[0]))
        # objective: main-component cell closest to the map center
        i_obj = np.argmin((xs - w // 2) ** 2 + (ys - h // 2) ** 2)
        objective = (int(xs[i_obj]), int(ys[i_obj]))
        markers = {"spawn": spawn, "objective": objective, "exit": exit_}
        for k, (x, y) in markers.items():
            _clear_around(ground, props, x, y, r=1)

    _dedupe_markers(markers, w, h)
    if border_wall:
        _apply_border_wall(ground, props, markers.get("exit"))
    return markers, mode

def _dedupe_markers(markers, w, h):
    """Nudges markers that would land on the same cell (tiny maps)."""
    seen = {}
    for name, (x, y) in list(markers.items()):
        while (x, y) in seen.values():
            y = (y + 2) % h if h > 2 else y
            x = (x + 1) % w if (x, y) in seen.values() else x
        seen[name] = (x, y)
        markers[name] = (x, y)

def _bfs_dist(mask, start):
    h, w = mask.shape
    dist = np.full((h, w), -1, dtype=np.int32)
    from collections import deque
    q = deque([start])
    dist[start[1], start[0]] = 0
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and mask[ny, nx] and dist[ny, nx] < 0:
                dist[ny, nx] = dist[y, x] + 1
                q.append((nx, ny))
    return dist

def _connect_points(ground, props, pts):
    """Connects each point to the previous one with a corridor if not already
    connected (walkable)."""
    if len(pts) < 2:
        return
    for a, b in zip(pts, pts[1:]):
        walk = np.isin(ground, tuple(WALKABLE)) & (props == P_NONE)
        d = _bfs_dist(walk, a)
        if d[b[1], b[0]] < 0:
            _carve_corridor(ground, props, a, b)

def _apply_border_wall(ground, props, exit_pos):
    """Perimeter wall around the map, left open (3 cells) where border roads
    and the exit gate are."""
    h, w = ground.shape
    ring = np.zeros_like(props, dtype=bool)
    ring[0, :] = ring[-1, :] = True
    ring[:, 0] = ring[:, -1] = True
    keep_open = np.isin(ground, (G_ROAD, G_PAVE, G_RAIL))
    props[ring & ~keep_open & (props == P_NONE)] = P_WALL
    if exit_pos:
        x, y = exit_pos
        _clear_around(ground, props, x, y, r=2, make=G_GROUND)

def verify_connectivity(ground, props, markers):
    if not markers:
        return True
    walk = np.isin(ground, tuple(WALKABLE)) & (props == P_NONE)
    pts = list(markers.values())
    d = _bfs_dist(walk, pts[0])
    ok = all(d[y, x] >= 0 for (x, y) in pts[1:])
    if not ok:
        warn("spawn/objective/exit connectivity not guaranteed — emergency corridors carved.")
        _connect_points(ground, props, pts)
    return ok

# =========================================== 5. BUILDINGS -> RECTANGLES =====

def _largest_rect(mask):
    """Largest solid rectangle in a binary mask (histograms + stack)."""
    h, w = mask.shape
    heights = np.zeros(w, dtype=int)
    best_area, best = 0, None
    for y in range(h):
        heights = (heights + 1) * mask[y]
        stack = []
        for x in range(w + 1):
            cur = heights[x] if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                sx, sh = stack.pop()
                area = sh * (x - sx)
                if area > best_area:
                    best_area, best = area, (sx, y - sh + 1, x - sx, sh)
                start = sx
            stack.append((start, cur))
    return best

def decompose_buildings(building_mask, catalog):
    """Cuts the building mask into catalog rectangles (greedy).
    catalog: {(w,h): [gids]} -> list of (x, y, w, h)."""
    sizes = sorted(catalog.keys(), key=lambda s: -(s[0] * s[1]))
    mask = building_mask.copy()
    rects, dropped = [], 0
    guard = mask.sum() * 2 + 10
    while mask.any() and guard > 0:
        guard -= 1
        r = _largest_rect(mask)
        if r is None:
            break
        x, y, rw, rh = r
        fit = next(((cw, ch) for (cw, ch) in sizes if cw <= rw and ch <= rh), None)
        if fit is None:  # no template fits: drop this area
            mask[y:y + rh, x:x + rw] = False
            dropped += rw * rh
            continue
        cw, ch = fit
        # tile the largest sub-area that is a multiple of (cw,ch); the rest stays
        # in the mask and is handled in later iterations (smaller templates)
        ny, nx = rh // ch, rw // cw
        for iy in range(ny):
            for ix in range(nx):
                rects.append((x + ix * cw, y + iy * ch, cw, ch))
        mask[y:y + ny * ch, x:x + nx * cw] = False
    if dropped:
        warn(f"{dropped} building cell(s) with no compatible template (add '1x1' to the catalog).")
    return rects

# ====================================================== 6. TMX (reading) ====

def read_layer_grid(layer_el, w, h):
    """Decodes <data> (csv, base64[+zlib/gzip], or <tile> XML) -> np.int64[h,w]."""
    data_el = layer_el.find("data")
    if data_el is None:
        return np.zeros((h, w), dtype=np.int64)
    enc = data_el.get("encoding")
    comp = data_el.get("compression")
    if enc == "csv":
        vals = [int(v) for v in data_el.text.replace("\n", ",").split(",") if v.strip()]
    elif enc == "base64":
        raw = base64.b64decode(data_el.text.strip())
        if comp == "zlib":
            raw = zlib.decompress(raw)
        elif comp == "gzip":
            raw = gzip.decompress(raw)
        vals = list(struct.unpack("<%dI" % (len(raw) // 4), raw))
    else:  # <tile gid="..">
        vals = [int(t.get("gid", 0)) for t in data_el.findall("tile")]
    arr = np.zeros(w * h, dtype=np.int64)
    arr[:min(len(vals), w * h)] = vals[:w * h]
    return arr.reshape(h, w)

def load_template(path):
    tree = ET.parse(path)
    root = tree.getroot()
    info = dict(
        width=int(root.get("width")), height=int(root.get("height")),
        tilewidth=int(root.get("tilewidth")), tileheight=int(root.get("tileheight")),
        orientation=root.get("orientation", "orthogonal"),
        layers={ly.get("name"): ly for ly in root.findall("layer")},
    )
    return tree, root, info

def template_marker_cells(root, info):
    """Non-empty cells of the template's 'objectives' layer -> [(gid, x, y)]."""
    ly = None
    for cand in root.findall("layer"):
        if cand.get("name", "").lower() == "objectives":
            ly = cand
            break
    if ly is None:
        return []
    g = read_layer_grid(ly, info["width"], info["height"])
    ys, xs = np.where(g != 0)
    return [(int(g[y, x]) & GID_MASK, int(x), int(y)) for x, y in zip(xs, ys)]

def csv_encode(grid_int):
    return "\n" + "\n".join(",".join(str(int(v)) for v in row) for row in grid_int) + "\n"

def set_layer_data(root, name, grid_int, insert_index=None):
    w = grid_int.shape[1]; h = grid_int.shape[0]
    layer = None
    for ly in root.findall("layer"):
        if ly.get("name") == name:
            layer = ly
            break
    if layer is None:
        layer = ET.Element("layer", {"name": name})
        kids = list(root)
        pos = len(kids)
        root.insert(pos if insert_index is None else insert_index, layer)
    layer.set("width", str(w)); layer.set("height", str(h))
    old = layer.find("data")
    if old is not None:
        layer.remove(old)
    data = ET.SubElement(layer, "data", {"encoding": "csv"})
    rows = csv_encode(grid_int)
    # csv_encode already handles per-row encoding, but Tiled also expects
    # commas between rows:
    data.text = "\n" + ",\n".join(",".join(str(int(v)) for v in row) for row in grid_int) + "\n"
    return layer

def set_map_property(root, key, value):
    props = root.find("properties")
    if props is None:
        props = ET.Element("properties")
        root.insert(0, props)
    for p in props.findall("property"):
        if p.get("name") == key:
            p.set("value", str(value))
            return
    ET.SubElement(props, "property", {"name": key, "value": str(value)})

# --------------------------------------- standalone template (preview) -----

CLASS_COLORS = {
    "ground": (138, 127, 106), "grass": (111, 158, 88), "water": (59, 110, 165),
    "pavement": (154, 160, 166), "road": (60, 60, 64), "rail": (107, 91, 69),
    "wall": (68, 68, 68), "tree": (34, 85, 43), "building": (122, 74, 58),
    "spawn": (80, 220, 100), "objective": (240, 200, 60), "exit": (80, 200, 230),
    "decor": (200, 100, 180),
}
STANDALONE_ORDER = ["ground", "grass", "water", "pavement", "road", "rail",
                    "wall", "tree", "building", "spawn", "objective", "exit"]

def build_standalone_template(w, h, out_dir):
    """Orthogonal 32px template with a solid-color tileset — preview in a
    vanilla Tiled only, NOT loadable by the game."""
    ts = 32
    img = Image.new("RGB", (ts * len(STANDALONE_ORDER), ts), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, name in enumerate(STANDALONE_ORDER):
        d.rectangle([i * ts, 0, (i + 1) * ts - 1, ts - 1], fill=CLASS_COLORS[name],
                    outline=(20, 20, 20))
    png = Path(out_dir) / "placeholder_tiles.png"
    img.save(png)
    root = ET.Element("map", {
        "version": "1.0", "orientation": "orthogonal", "renderorder": "right-down",
        "width": str(w), "height": str(h), "tilewidth": str(ts), "tileheight": str(ts),
    })
    tset = ET.SubElement(root, "tileset", {
        "firstgid": "1", "name": "placeholder", "tilewidth": str(ts),
        "tileheight": str(ts), "tilecount": str(len(STANDALONE_ORDER)), "columns": str(len(STANDALONE_ORDER)),
    })
    ET.SubElement(tset, "image", {"source": png.name,
                                  "width": str(ts * len(STANDALONE_ORDER)), "height": str(ts)})
    mapping = json.loads(json.dumps(DEFAULT_MAPPING))
    gid = {name: i + 1 for i, name in enumerate(STANDALONE_ORDER)}
    mapping["tiles"] = {k: [gid[k]] for k in ["ground", "grass", "water", "pavement", "road", "rail"]}
    mapping["props"] = {"wall": [gid["wall"]], "tree": [gid["tree"]],
                        "building_catalog": {"1x1": [gid["building"]]}}
    mapping["markers"] = {"mode": "auto", "spawn": [gid["spawn"]],
                          "objective": [gid["objective"]], "exit": [gid["exit"]]}
    return ET.ElementTree(root), root, dict(width=w, height=h, tilewidth=ts,
                                            tileheight=ts, orientation="orthogonal",
                                            layers={}), mapping

# =================================================== 7. LEVEL ASSEMBLY ======

def pick(rng, lst):
    lst = lst or [0]
    return int(rng.choice(lst))

def grids_to_gids(ground, props, building_mask, markers, marker_mode,
                  template_obj_grid, mapping, rng):
    h, w = ground.shape
    tiles = mapping.get("tiles", {})
    propmap = mapping.get("props", {})
    g_map = np.zeros((h, w), dtype=np.int64)
    for cls_id, name in GROUND_NAMES.items():
        ys, xs = np.where(ground == cls_id)
        gids = tiles.get(name) or tiles.get("ground") or [0]
        for y, x in zip(ys, xs):
            g_map[y, x] = pick(rng, gids)

    # Road edges and corners (t_alltiles). Per-cell detection of the 4
    # exposed sides (non-road neighbor): two adjacent exposed sides -> curve
    # corner tile, a single one -> straight edge tile. Assignments derived
    # from actual usage in the game's shipped levels (dominant neighbor
    # pattern per tile, cf. level_*.tmx analysis).
    e_nw = tiles.get("road_edge_nw") or []
    e_ne = tiles.get("road_edge_ne") or []
    e_sw = tiles.get("road_edge_sw") or []
    e_se = tiles.get("road_edge_se") or []
    c_n = tiles.get("road_corner_n") or []
    c_e = tiles.get("road_corner_e") or []
    c_w = tiles.get("road_corner_w") or []
    c_s = tiles.get("road_corner_s") or []
    if e_nw or e_ne or e_sw or e_se:
        roadlike = (ground == G_ROAD)
        for y, x in zip(*np.where(roadlike)):
            nw = x == 0 or not roadlike[y, x - 1]
            ne = y == 0 or not roadlike[y - 1, x]
            se = x == w - 1 or not roadlike[y, x + 1]
            sw = y == h - 1 or not roadlike[y + 1, x]
            gid = None
            if nw and ne and c_n:
                gid = pick(rng, c_n)
            elif ne and se and c_e:
                gid = pick(rng, c_e)
            elif nw and sw and c_w:
                gid = pick(rng, c_w)
            elif se and sw and c_s:
                gid = pick(rng, c_s)
            elif nw and e_nw:
                gid = pick(rng, e_nw)
            elif ne and e_ne:
                gid = pick(rng, e_ne)
            elif sw and e_sw:
                gid = pick(rng, e_sw)
            elif se and e_se:
                gid = pick(rng, e_se)
            if gid:
                g_map[y, x] = gid

    g_props = np.zeros((h, w), dtype=np.int64)
    # Walls: a single style per connected run (otherwise the same fence
    # would alternate brick/hedge/wire cell by cell).
    wall_gids = propmap.get("wall", [0])
    wall_mask = (props == P_WALL)
    seen = np.zeros((h, w), dtype=bool)
    for y0, x0 in zip(*np.where(wall_mask)):
        if seen[y0, x0]:
            continue
        gid = pick(rng, wall_gids)
        stack = [(x0, y0)]
        seen[y0, x0] = True
        while stack:
            x, y = stack.pop()
            g_props[y, x] = gid
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and wall_mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((nx, ny))
    for y, x in zip(*np.where(props == P_TREE)):
        g_props[y, x] = pick(rng, propmap.get("tree", [0]))
    for y, x in zip(*np.where(props == P_DECOR)):
        g_props[y, x] = pick(rng, propmap.get("decor", [0]))

    # Cars scattered on the road network (road cells without props).
    car_gids = propmap.get("car") or []
    car_dens = float(mapping.get("car_density_on_road", 0.0) or 0.0)
    if car_gids and car_dens > 0:
        for y, x in zip(*np.where((ground == G_ROAD) & (g_props == 0))):
            if rng.random() < car_dens:
                g_props[y, x] = pick(rng, car_gids)

    catalog_raw = propmap.get("building_catalog", {}) or {}
    catalog = {}
    for key, gids in catalog_raw.items():
        if key.startswith("_"):  # comment keys
            continue
        try:
            cw, ch = (int(v) for v in key.lower().split("x"))
            catalog[(cw, ch)] = gids
        except Exception:
            warn(f"Building catalog key ignored: {key!r} (expected format 'WxH')")
    if catalog:
        rects = decompose_buildings(building_mask & (props == P_BUILDING), catalog)
        for (x, y, cw, ch) in rects:
            ax, ay = x, y + ch - 1  # Tiled anchor: bottom-left corner of the footprint
            g_props[ay, ax] = pick(rng, catalog[(cw, ch)])
        log(f"{len(rects)} buildings placed (catalog: {sorted(catalog)})")
    else:
        warn("No building catalog in the mapping: building cells left empty.")

    g_traps = np.zeros((h, w), dtype=np.int64)
    water_gids = (mapping.get("traps") or {}).get("water") or []
    if water_gids:
        for y, x in zip(*np.where(ground == G_WATER)):
            g_traps[y, x] = pick(rng, water_gids)

    if marker_mode == "preserve-template" and template_obj_grid is not None:
        g_obj = template_obj_grid.astype(np.int64)
    else:
        g_obj = np.zeros((h, w), dtype=np.int64)
        mk = mapping.get("markers", {})
        for name in ("spawn", "objective", "exit"):
            if name in markers:
                x, y = markers[name]
                g_obj[y, x] = pick(rng, mk.get(name, [0]))
    return g_map, g_props, g_traps, g_obj

def trim_to_content(ground, props, building_mask, margin=2):
    """Trims empty margins (bare ground without props) — rotation inflates the
    canvas with empty corners; crop back to actual content."""
    content = (ground != G_GROUND) | (props != P_NONE)
    if not content.any():
        return ground, props, building_mask
    ys, xs = np.where(content)
    y0 = max(0, ys.min() - margin); y1 = min(ground.shape[0], ys.max() + 1 + margin)
    x0 = max(0, xs.min() - margin); x1 = min(ground.shape[1], xs.max() + 1 + margin)
    return ground[y0:y1, x0:x1], props[y0:y1, x0:x1], building_mask[y0:y1, x0:x1]

def resize_center(arr, W, H, fill=0):
    """Crops/pads arr centered to (H,W). -> arr2, (offx, offy)"""
    h, w = arr.shape
    out = np.full((H, W), fill, dtype=arr.dtype)
    sx = max(0, (w - W) // 2); sy = max(0, (h - H) // 2)
    dx = max(0, (W - w) // 2); dy = max(0, (H - h) // 2)
    cw = min(w, W); ch = min(h, H)
    out[dy:dy + ch, dx:dx + cw] = arr[sy:sy + ch, sx:sx + cw]
    return out, (dx - sx, dy - sy)

# ============================================================= PREVIEW ======

def render_preview(ground, props, markers, path, scale=5):
    h, w = ground.shape
    img = Image.new("RGB", (w * scale, h * scale), (0, 0, 0))
    d = ImageDraw.Draw(img)
    gcol = {G_GROUND: CLASS_COLORS["ground"], G_GRASS: CLASS_COLORS["grass"],
            G_WATER: CLASS_COLORS["water"], G_PAVE: CLASS_COLORS["pavement"],
            G_ROAD: CLASS_COLORS["road"], G_RAIL: CLASS_COLORS["rail"]}
    pcol = {P_BUILDING: CLASS_COLORS["building"], P_WALL: CLASS_COLORS["wall"],
            P_TREE: CLASS_COLORS["tree"], P_DECOR: CLASS_COLORS["decor"]}
    for y in range(h):
        for x in range(w):
            c = pcol.get(props[y, x]) or gcol.get(ground[y, x], (10, 10, 10))
            d.rectangle([x * scale, y * scale, (x + 1) * scale - 1, (y + 1) * scale - 1], fill=c)
    for name, (x, y) in (markers or {}).items():
        c = CLASS_COLORS[name]
        r = scale * 1.6
        cx, cy = x * scale + scale / 2, y * scale + scale / 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=max(2, scale // 2))
    img.save(path)
    log(f"Preview: {path}")

# ============================================================ COMMANDS ======

def load_mapping(path):
    m = json.loads(json.dumps(DEFAULT_MAPPING))
    if path:
        user = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(m.get(k), dict):
                m[k].update(v)
            else:
                m[k] = v
    return m

def cmd_generate(args):
    # --- zero-config resolution ------------------------------------------------
    # Template: --template, else auto-detected (--brigador-dir / BRIGADOR_DIR /
    # saved config / common Steam paths). Mapping: --mapping, else the
    # mapping.json shipped next to this script (GIDs for the stock ALLSTARTER).
    template_path = args.template
    if not template_path:
        found = find_template(args.brigador_dir)
        if found:
            template_path = str(found)
            log(f"Template auto-detected: {template_path}")
    if args.brigador_dir and template_path:
        cfg = load_user_config()
        if cfg.get("brigador_dir") != args.brigador_dir:
            cfg["brigador_dir"] = args.brigador_dir
            save_user_config(cfg)
            log(f"Brigador directory remembered in {CONFIG_PATH}")

    if not args.mapping:
        bundled = Path(__file__).resolve().parent / "mapping.json"
        if template_path and bundled.exists():
            args.mapping = str(bundled)
            log(f"Mapping auto-selected: {bundled}")
    mapping = load_mapping(args.mapping)
    rng = random.Random(args.seed)

    if template_path:
        tree, root, info = load_template(template_path)
        tmpl_markers = template_marker_cells(root, info)
    else:
        warn("No --template and no Brigador install found: generating a "
             "standalone template (Tiled preview only, NOT loadable by "
             "Brigador). Point me at the game with --brigador-dir.")
        # free size determined after rasterization; placeholders assigned later
        tree = root = info = None
        tmpl_markers = []

    # Target grid dimensions, if already known — they drive the default extent
    # so that the fetched area matches what the grid can hold.
    Wt = Ht = None
    if args.size not in ("auto", "template"):
        Wt, Ht = (int(v) for v in args.size.lower().split("x"))
    elif args.size == "template" and info:
        Wt, Ht = info["width"], info["height"]
    max_extent = args.max_extent
    if max_extent is None:
        max_extent = max(Wt, Ht) * args.meters_per_tile if Wt else 1500.0
        log(f"Max extent: {max_extent:.0f} m "
            f"({'grid size × scale' if Wt else 'default'})")

    # --- output path -------------------------------------------------------------
    if not args.out:
        if args.place:
            slug = slugify(args.place)
        elif args.center:
            slug = "at_" + slugify(args.center)
        elif args.bbox:
            slug = "bbox_" + slugify(args.bbox)
        else:
            slug = slugify(Path(args.geojson).stem)
        base = Path(template_path).parent if template_path else Path(".")
        args.out = str(unique_path(base / f"level_{slug}.tmx"))
        log(f"Output: {args.out}")

    # --- data source ---------------------------------------------------------
    if args.geojson:
        feats_raw = features_from_geojson(args.geojson)
        if args.bbox:
            bbox = parse_bbox(args.bbox)
        else:
            gs = [f["geom"] for f in feats_raw]
            minx = min(g.bounds[0] for g in gs); miny = min(g.bounds[1] for g in gs)
            maxx = max(g.bounds[2] for g in gs); maxy = max(g.bounds[3] for g in gs)
            bbox = (minx, miny, maxx, maxy)
        log(f"{len(feats_raw)} GeoJSON features loaded")
    else:
        if args.center:
            try:
                lat, lon = (float(v) for v in args.center.split(","))
            except Exception:
                raise SystemExit("--center expects lat,lon (GPS decimal degrees)")
            half = max_extent / 2.0
            m_lat = 111320.0
            m_lon = 111320.0 * math.cos(math.radians(lat))
            bbox = (lon - half / m_lon, lat - half / m_lat,
                    lon + half / m_lon, lat + half / m_lat)
            log(f"GPS center {lat:.5f},{lon:.5f} -> bbox "
                f"{bbox[0]:.5f},{bbox[1]:.5f},{bbox[2]:.5f},{bbox[3]:.5f}")
        else:
            bbox = parse_bbox(args.bbox) if args.bbox else geocode_place(args.place)
            bbox = clamp_bbox_extent(bbox, max_extent)
        data = fetch_overpass(bbox, args.cache_dir, endpoint=args.overpass_url)
        feats_raw = features_from_overpass(data)
        log(f"{len(feats_raw)} features classified")
    if not feats_raw:
        raise SystemExit("No usable data in the requested area.")

    # --- projection + rotation -----------------------------------------------
    feats, clip = project_features(feats_raw, bbox)
    if args.rotate == "auto":
        ang = dominant_bearing(feats)
        log(f"Auto rotation: {ang:.1f}° (aligning the dominant street grid)")
    else:
        ang = float(args.rotate)
    feats, clip = rotate_features(feats, clip, ang)

    standalone_dir = Path(args.out).parent or Path(".")

    # --- grid + rasterization -------------------------------------------------
    grid = Grid(clip, args.meters_per_tile)
    log(f"Raw grid: {grid.w}×{grid.h} tiles ({args.meters_per_tile} m/tile)")
    if grid.w * grid.h > args.max_cells:
        raise SystemExit(f"Grid {grid.w}×{grid.h} too large (> {args.max_cells} cells). "
                         f"Shrink the bbox or increase --meters-per-tile.")
    ground, props, building_mask = rasterize(feats, grid, rng, mapping)
    ground, props, building_mask = trim_to_content(ground, props, building_mask)
    if ground.shape != (grid.h, grid.w):
        log(f"Empty margins trimmed -> {ground.shape[1]}×{ground.shape[0]} tiles")

    # --- final size ------------------------------------------------------------
    cur_h, cur_w = ground.shape
    if args.size == "template" and info:
        W, H = info["width"], info["height"]
    elif args.size in ("auto", "template"):
        W, H = max(cur_w, 24), max(cur_h, 24)
    else:
        W, H = (int(v) for v in args.size.lower().split("x"))
    if (W, H) != (cur_w, cur_h):
        log(f"Centered crop/pad {cur_w}×{cur_h} -> {W}×{H}")
        ground, _ = resize_center(ground, W, H, fill=G_GROUND)
        props, _ = resize_center(props, W, H, fill=P_NONE)
        building_mask, _ = resize_center(building_mask.astype(np.uint8), W, H, 0)
        building_mask = building_mask.astype(bool)

    if root is None:
        tree, root, info, auto_map = build_standalone_template(W, H, standalone_dir)
        # the standalone mapping only fills gids not provided by the user
        if not args.mapping:
            mapping = auto_map
        tmpl_markers = []

    # --- gameplay ----------------------------------------------------------------
    mk_mode = (mapping.get("markers") or {}).get("mode", "auto")
    if mk_mode == "preserve-template" and (info["width"], info["height"]) != (W, H):
        warn("markers.mode=preserve-template requires the output size to match "
             "the template; falling back to 'auto'.")
        mapping["markers"]["mode"] = "auto"
    markers, mk_mode = place_gameplay(ground, props, tmpl_markers, mapping,
                                      border_wall=not args.no_border)
    verify_connectivity(ground, props, markers)

    # --- gids + TMX write ------------------------------------------------------
    tmpl_obj_grid = None
    if mk_mode == "preserve-template":
        for ly in root.findall("layer"):
            if ly.get("name", "").lower() == "objectives":
                tmpl_obj_grid = read_layer_grid(ly, info["width"], info["height"])
    g_map, g_props, g_traps, g_obj = grids_to_gids(
        ground, props, building_mask, markers, mk_mode, tmpl_obj_grid, mapping, rng)

    root.set("width", str(W)); root.set("height", str(H))
    for name, gridi in (("map", g_map), ("props", g_props),
                        ("traps", g_traps), ("objectives", g_obj)):
        set_layer_data(root, name, gridi)
    # auxiliary template layers of a different size: blank them to avoid a crash
    for ly in root.findall("layer"):
        if ly.get("name") not in ("map", "props", "traps", "objectives"):
            if int(ly.get("width", W)) != W or int(ly.get("height", H)) != H:
                set_layer_data(root, ly.get("name"), np.zeros((H, W), dtype=np.int64))
                warn(f"Auxiliary layer '{ly.get('name')}' resized and blanked.")

    set_map_property(root, "attribution", "Map data (c) OpenStreetMap contributors, ODbL 1.0 — openstreetmap.org/copyright")
    set_map_property(root, "generator", f"{TOOL} {VERSION}")
    set_map_property(root, "source_bbox_wsen", ",".join(f"{v:.6f}" for v in bbox))
    set_map_property(root, "rotation_deg", f"{ang:.1f}")
    set_map_property(root, "meters_per_tile", str(args.meters_per_tile))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space=" ")
    tree.write(out, encoding="UTF-8", xml_declaration=True)
    log(f"TMX written: {out}  ({W}×{H} tiles)")
    if markers:
        mk = mapping.get("markers") or {}
        for k, (x, y) in markers.items():
            placeholder = not any(mk.get(k) or [])
            log(f"  marker {k:<9} -> tile ({x},{y})"
                + ("   [gid=0: place manually in Tiled]" if placeholder else ""))
    if args.preview:
        render_preview(ground, props, markers, args.preview)
    _post_checks(g_props, g_obj, mapping)

def _post_checks(g_props, g_obj, mapping):
    turret_gids = set(mapping.get("turret_gids") or [])
    if turret_gids:
        n = int(np.isin(g_props & GID_MASK, list(turret_gids)).sum())
        limit = int(mapping.get("max_turrets", 8))
        if n > limit:
            warn(f"{n} turrets detected > limit {limit}: THE GAME WILL CRASH. Remove some in Tiled.")
    nz = int((g_obj != 0).sum())
    if nz < 3:
        warn(f"objectives layer: {nz} non-empty cell(s). At least spawn + objective "
             f"+ exit are required — complete in Tiled (gids from the ALLSTARTER template).")

def cmd_inspect(args):
    tree, root, info = load_template(args.template)
    print(f"map: {info['width']}×{info['height']} tiles, tile {info['tilewidth']}×{info['tileheight']} px, "
          f"orientation={info['orientation']}")
    for ts in root.findall("tileset"):
        src = ts.get("source") or (ts.find("image").get("source") if ts.find("image") is not None else "?")
        print(f"tileset firstgid={ts.get('firstgid'):>6}  name={ts.get('name','(external)')}  source={src}")
    for ly in root.findall("layer"):
        g = read_layer_grid(ly, int(ly.get("width", info["width"])), int(ly.get("height", info["height"])))
        vals, counts = np.unique(g[g != 0] & GID_MASK, return_counts=True)
        top = sorted(zip(vals.tolist(), counts.tolist()), key=lambda t: -t[1])[:15]
        print(f"layer '{ly.get('name')}': {int((g != 0).sum())} non-empty cells; frequent gids: {top}")
        if ly.get("name", "").lower() == "objectives":
            for gid, x, y in template_marker_cells(root, info):
                print(f"    objectives gid={gid} @ ({x},{y})   <- spawn/objective/exit candidates for mapping.json")
    for og in root.findall("objectgroup"):
        print(f"objectgroup '{og.get('name')}': {len(og.findall('object'))} objects")

def cmd_validate(args):
    mapping = load_mapping(args.mapping)
    tree, root, info = load_template(args.tmx)
    ok = True
    names = {ly.get("name") for ly in root.findall("layer")}
    for req in ("map", "props", "traps", "objectives"):
        if req not in names:
            print(f"ERROR: missing layer '{req}'"); ok = False
    W, H = info["width"], info["height"]
    for ly in root.findall("layer"):
        g = read_layer_grid(ly, int(ly.get("width", W)), int(ly.get("height", H)))
        if g.shape != (H, W):
            print(f"ERROR: layer '{ly.get('name')}' size {g.shape[::-1]} ≠ map {W}×{H}"); ok = False
        if ly.get("name") == "objectives":
            nz = int((g != 0).sum())
            print(f"objectives: {nz} marker(s)")
            if nz < 3:
                print("ERROR: at least spawn + objective + exit (3 markers) required"); ok = False
        if ly.get("name") == "props" and mapping.get("turret_gids"):
            n = int(np.isin(g & GID_MASK, list(mapping["turret_gids"])).sum())
            print(f"turrets: {n} / {mapping.get('max_turrets', 8)}")
            if n > int(mapping.get("max_turrets", 8)):
                print("ERROR: too many turrets, the game will crash on load"); ok = False
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 2)

def main():
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate a .tmx from OSM/GeoJSON")
    src = g.add_mutually_exclusive_group(required=True)
    src.add_argument("--place", help="place name, e.g. 'Story City, Iowa' (Nominatim geocoding)")
    src.add_argument("--center", help="lat,lon GPS point; covered area = grid size × scale (or --max-extent)")
    src.add_argument("--bbox", help="west,south,east,north (degrees)")
    src.add_argument("--geojson", help="local FeatureCollection (OSM tags in properties)")
    g.add_argument("--brigador-dir", help="Brigador install/modkit root; auto-detected from BRIGADOR_DIR, "
                   "a saved config or common Steam paths, and remembered after first use")
    g.add_argument("--template", help="path to level_ALLSTARTER_usethistobuildnewlevels.tmx "
                   "(default: auto-detected in the Brigador directory)")
    g.add_argument("--mapping", help="mapping.json (tile gids; default: the one shipped with the tool)")
    g.add_argument("--out", help="output .tmx (default: assets/tiledmaps/level_<place>.tmx next to the template)")
    g.add_argument("--meters-per-tile", type=float, default=6.0)
    g.add_argument("--size", default="template",
                   help="'template' (default), 'auto', or 'WIDTHxHEIGHT' in tiles")
    g.add_argument("--rotate", default="auto", help="'auto' (default) or angle in degrees (0 = north up)")
    g.add_argument("--seed", type=int, default=1337)
    g.add_argument("--max-extent", type=float, default=None,
                   help="max bbox side in meters (default: grid size × scale, else 1500)")
    g.add_argument("--max-cells", type=int, default=1024 * 1024)
    g.add_argument("--no-border", action="store_true", help="no automatic perimeter wall")
    g.add_argument("--preview", help="preview PNG")
    g.add_argument("--cache-dir", default=".osmcache")
    g.add_argument("--overpass-url", default="https://overpass-api.de/api/interpreter")
    g.set_defaults(func=cmd_generate)

    i = sub.add_parser("inspect-template", help="list tilesets, layers and markers of a .tmx")
    i.add_argument("template")
    i.set_defaults(func=cmd_inspect)

    v = sub.add_parser("validate", help="check layers, markers and the turret limit")
    v.add_argument("tmx")
    v.add_argument("--mapping")
    v.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
