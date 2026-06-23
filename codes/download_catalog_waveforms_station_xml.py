#!/usr/bin/env python3
"""
download_catalog_waveforms_station_xml.py
--------------------------------------
Interactive FDSN downloader: catalog, station metadata, waveforms.

  1. Reads (or creates) CAT/config_cat/config.yaml
  2. Queries FDSN for events and stations
  3. Shows an interactive preview map (folium → fallback: pygmt)
  4. After confirmation downloads and saves everything

Output layout
─────────────
  CAT/
    config_cat/config.yaml          ← configuration (created if absent)
    {catalog_name}.xml              ← QuakeML catalog
    {catalog_name}.txt              ← human-readable table
    {catalog_name}.pf               ← Pyrocko basic format
  META_DATA/
    {station_file_name}.xml         ← StationXML with instrument response
  DATA/
    {event_label}_yyyy_mm_dd_hh_mm_ss/
      {event_label}_yyyy_mm_dd_hh_mm_ss_{NET}.{STA}.mseed

Dependencies: obspy, pyrocko, pyyaml, folium (or pygmt as fallback)
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import sys
import argparse
import platform
import subprocess
import webbrowser
import tempfile

# ── Third-party ───────────────────────────────────────────────────────────────
import yaml
from pyrocko import util, model
from obspy.clients.fdsn.client import Client
from obspy import UTCDateTime
from obspy.core.event import Catalog


# ═════════════════════════════════════════════════════════════════════════════
# PATHS  (script lives in codes/; project root is one level up)
# ═════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR   = os.path.normpath(os.path.join(SCRIPT_DIR, '..'))
CONFIG_DIR = os.path.join(WORK_DIR, 'CAT', 'config_cat')
CAT_DIR    = os.path.join(WORK_DIR, 'CAT')
META_DIR    = os.path.join(WORK_DIR, 'META_DATA')
DATA_DIR    = os.path.join(WORK_DIR, 'DATA')


# ═════════════════════════════════════════════════════════════════════════════
# 1 ─ CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

# Written as a raw string so that all comments are preserved in the file.
DEFAULT_CONFIG = """\
# ══════════════════════════════════════════════════════════════════════════════
#  FDSN DOWNLOAD CONFIGURATION
#  Edit this file, then run  download_catalog_waveforms_station_xml.py
# ══════════════════════════════════════════════════════════════════════════════

# ── Output naming ─────────────────────────────────────────────────────────────

# Base name for all catalog files (no extension needed).
#   Produces:  CAT/<catalog_name>.xml   .txt   .pf
catalog_name: "my_catalog"

# Short label used as prefix for event folders and waveform filenames.
#   Produces:  DATA/<event_label>_yyyy_mm_dd_hh_mm_ss/
#                   <event_label>_yyyy_mm_dd_hh_mm_ss_NET.STA.mseed
event_label: "EV"

# Base name for the station metadata file (no extension).
#   Produces:  META_DATA/<station_file_name>.xml
station_file_name: "stations"

# ── FDSN server ───────────────────────────────────────────────────────────────

# Data centre for STATIONS and WAVEFORMS.  Common values:
#   IRIS, INGV, GEOFON, ORFEUS, BGR, ETH, RASPISHAKE, NCEDC, SCEDC, GFZ …
# Full list: https://docs.obspy.org/packages/obspy.clients.fdsn.html
fdsn_site: "INGV"

# Data centre for EVENTS (catalog query).
# Use null to query the same server as fdsn_site.
# NOTE: IRIS does NOT provide an event service anymore — use USGS (global NEIC
# catalog) or ISC when fdsn_site is IRIS.
#   null   → same as fdsn_site
#   "USGS" → USGS/NEIC global catalog  (best choice when fdsn_site = IRIS)
#   "ISC"  → International Seismological Centre
#   "INGV" → Italian catalog
fdsn_site_events: null

# ── Time window ───────────────────────────────────────────────────────────────

# Start and end of the search window.  Format: "YYYY-MM-DD HH:MM:SS"
tmin: "2024-01-01 00:00:00"
tmax: "2024-12-31 23:59:59"

# ── Search area ───────────────────────────────────────────────────────────────

# Shape of the geographic search area.
#   "rectangular"  → uses lat_min / lat_max / lon_min / lon_max
#   "circular"     → uses lat_center / lon_center / radius_km
area_type: "rectangular"

# Rectangular bounding box (decimal degrees)
lat_min:  40.75
lat_max:  40.95
lon_min:  14.00
lon_max:  14.20

# Circular area (decimal degrees / km)
lat_center:  40.83
lon_center:  14.14
radius_km:   15.0

# ── Event filters ─────────────────────────────────────────────────────────────

# Magnitude range.  Use null to disable either bound.
mag_min:  1.5
mag_max:  null

# Depth range in km.  Use null to disable either bound.
depth_min_km:  0.0
depth_max_km:  30.0

# ── Station search area ───────────────────────────────────────────────────────

# The station search area is ALWAYS circular and INDEPENDENT from the event
# search area defined above.  Set the centre and radius to taste.
lat_center_sta:  40.83
lon_center_sta:  14.14
radius_km_sta:   50.0

# ── Station source ────────────────────────────────────────────────────────────

# Path to an existing StationXML to load instead of querying FDSN for stations.
#   null        → query stations from FDSN using the station search area above
#   "file.xml"  → bare filename: looked up inside META_DATA/
#   "/abs/path" → absolute path used as-is
# When set, lat_center_sta / radius_km_sta / network / station / location /
# channel are IGNORED — the inventory is used exactly as stored in the file.
existing_stations_xml: null    # e.g.  "stations_flegrei_INGV_final.xml"  or  null

# ── Station / channel selection ───────────────────────────────────────────────

# FDSN wildcards: * (any string) and ? (exactly one character).
# Set network: "*" to retrieve ALL networks visible in the area.
# channel accepts comma-separated patterns:  "HH?,BH?"  "HHZ,HHN,HHE"  "*"
network:  "*"
station:  "*"
location: "*"
channel:  "HH?,BH?"

# ── Waveform time window ──────────────────────────────────────────────────────

# Seconds BEFORE the event origin time to start the trace.
t_before_s: 60
# Seconds AFTER the event origin time to end the trace.
t_after_s:  300

# ── Download chunking ─────────────────────────────────────────────────────────

# Event queries are split into blocks of this many days to avoid FDSN limits.
# Reduce if the server returns HTTP 413 or timeout errors on large windows.
chunk_days: 365

# ── Download mode ─────────────────────────────────────────────────────────────

# "event"      → one waveform excerpt per event (uses the catalog above)
#                  Files: DATA/<event_label>_yyyy_mm_dd_hh_mm_ss/
#                              <event_label>_yyyy_mm_dd_hh_mm_ss_NET_STA.mseed
# "continuous" → full continuous stream for every station across tmin→tmax
#                  Files: DATA/CONTINUOUS/<event_label>_yyyy_mm_dd_NET_STA.mseed
download_mode: "event"

# Size of each continuous chunk (hours).  24 = one file per day per station.
# Only used when download_mode is "continuous".
chunk_hours: 24

# ── Map preview ───────────────────────────────────────────────────────────────

# Backend for the interactive preview map.
#   "folium"  → HTML map opened in the browser  (pip install folium)
#   "pygmt"   → static figure via PyGMT          (pip install pygmt)
map_backend: "folium"
"""


def load_or_create_config(path: str) -> dict:
    """Return the config dict; create a commented default file if absent."""
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(DEFAULT_CONFIG)
        print(f"\n[CONFIG] Default configuration written to:\n  {path}")
        print("  Please review it, then re-run the script.\n")
        sys.exit(0)

    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    return cfg


def print_config_summary(cfg: dict) -> None:
    w = 62
    print("\n" + "═" * w)
    print("  CONFIGURATION SUMMARY")
    print("═" * w)
    ev_site = cfg.get('fdsn_site_events') or cfg['fdsn_site']
    print(f"  FDSN site       : {cfg['fdsn_site']}  (stations + waveforms)")
    if ev_site != cfg['fdsn_site']:
        print(f"  FDSN events     : {ev_site}  (catalog query)")
    print(f"  Time window     : {cfg['tmin']}  →  {cfg['tmax']}")
    print(f"  Event area      : {cfg['area_type']}")
    if cfg['area_type'] == 'rectangular':
        print(f"    lat [{cfg['lat_min']:.3f} – {cfg['lat_max']:.3f}]  "
              f"lon [{cfg['lon_min']:.3f} – {cfg['lon_max']:.3f}]")
    else:
        print(f"    centre ({cfg['lat_center']:.4f}, {cfg['lon_center']:.4f})  "
              f"radius {cfg['radius_km']:.1f} km")
    print(f"  Station area    : circular  "
          f"centre ({cfg['lat_center_sta']:.4f}, {cfg['lon_center_sta']:.4f})  "
          f"radius {cfg['radius_km_sta']:.1f} km")
    existing_sta = cfg.get('existing_stations_xml') or None
    sta_src = f"EXISTING FILE → {existing_sta}" if existing_sta else "FDSN query"
    print(f"  Station source  : {sta_src}")
    mag_str   = f"{cfg.get('mag_min') or '–'}  –  {cfg.get('mag_max') or '–'}"
    depth_str = (f"{cfg.get('depth_min_km') or '–'}  –  "
                 f"{cfg.get('depth_max_km') or '–'}  km")
    mode = cfg.get('download_mode', 'event')
    print(f"  ── Mode         : {mode.upper()}")
    if mode == 'continuous':
        print(f"  Chunk size      : {cfg.get('chunk_hours', 24)} h per file")
    else:
        print(f"  Magnitude       : {mag_str}")
        print(f"  Depth           : {depth_str}")
    print(f"  Network         : {cfg['network']}   Station: {cfg['station']}")
    print(f"  Channel         : {cfg['channel']}")
    if mode == 'event':
        print(f"  Waveform window : -{cfg['t_before_s']} s  /  +{cfg['t_after_s']} s")
        print(f"  Catalog name    : {cfg['catalog_name']}")
    print(f"  Event label     : {cfg['event_label']}")
    print(f"  Station file    : {cfg['station_file_name']}")
    print(f"  Map backend     : {cfg.get('map_backend', 'folium')}")
    print("═" * w + "\n")


def _update_config_file(path: str, cfg: dict) -> None:
    """Rewrite config.yaml preserving all comments but updating values."""
    with open(path) as fh:
        lines = fh.readlines()
    out = []
    for line in lines:
        stripped = line.lstrip()
        # Keep comments and blank lines untouched
        if not stripped or stripped.startswith('#') or ':' not in stripped:
            out.append(line)
            continue
        key = stripped.split(':')[0].strip()
        if key in cfg:
            indent = len(line) - len(line.lstrip())
            v = cfg[key]
            if v is None:
                out.append(' ' * indent + f'{key}: null\n')
            elif isinstance(v, bool):
                out.append(' ' * indent + f'{key}: {str(v).lower()}\n')
            elif isinstance(v, str):
                out.append(' ' * indent + f'{key}: "{v}"\n')
            else:
                out.append(' ' * indent + f'{key}: {v}\n')
        else:
            out.append(line)
    with open(path, 'w') as fh:
        fh.writelines(out)


def ask_modify_config(path: str, cfg: dict) -> dict:
    """Optionally let the user change individual parameters interactively."""
    ans = input("Modify a parameter before running? [y/N]: ").strip().lower()
    if ans != 'y':
        return cfg

    keys = list(cfg.keys())
    print(f"  Available parameters: {keys}")
    print("  Type 'done' when finished.\n")

    while True:
        key = input("  Parameter name (or 'done'): ").strip()
        if key.lower() == 'done':
            break
        if key not in cfg:
            print(f"  ! Unknown key '{key}'.  Choose from: {keys}")
            continue
        old = cfg[key]
        raw = input(f"  {key} [{old}]  →  new value: ").strip()
        # Preserve the original type where possible
        if raw.lower() in ('null', 'none', ''):
            cfg[key] = None
        elif isinstance(old, bool):
            cfg[key] = raw.lower() in ('true', 'yes', '1')
        elif isinstance(old, int):
            try:    cfg[key] = int(raw)
            except: cfg[key] = raw
        elif isinstance(old, float):
            try:    cfg[key] = float(raw)
            except: cfg[key] = raw
        else:
            cfg[key] = raw
        print(f"  ✓  {key} = {cfg[key]}\n")

    save = input("  Save changes to config.yaml? [y/N]: ").strip().lower()
    if save == 'y':
        _update_config_file(path, cfg)
        print(f"  Config updated: {path}\n")
    return cfg


# ═════════════════════════════════════════════════════════════════════════════
# 2 ─ EXISTING FILE LOADERS
# ═════════════════════════════════════════════════════════════════════════════

def _resolve_xml_path(value: str, base_dir: str) -> str:
    """Return absolute path to an XML file.

    Bare filename (no path separator) → looked up in base_dir.
    Anything else → treated as an absolute or relative path.
    """
    if os.sep not in value and '/' not in value:
        return os.path.join(base_dir, value)
    return os.path.abspath(value)


def load_stations_from_xml(xml_path: str):
    """Load an existing StationXML inventory from disk."""
    from obspy import read_inventory
    if not os.path.isfile(xml_path):
        print(f"\n  [ERROR] File not found: {xml_path}\n")
        sys.exit(1)
    print(f"  File : {xml_path}")
    inv = read_inventory(xml_path)
    n_sta = sum(len(net) for net in inv)
    n_net = len(inv)
    print(f"  {n_sta} stations across {n_net} network(s) loaded.\n")
    return inv


# ═════════════════════════════════════════════════════════════════════════════
# 3 ─ FDSN QUERIES
# ═════════════════════════════════════════════════════════════════════════════

def _radius_km_to_deg(km: float) -> float:
    """Approximate conversion: km → degrees (1° ≈ 111.32 km)."""
    return km / 111.32


def _area_kwargs_events(cfg: dict) -> dict:
    """Geographic kwargs for the EVENT query (rectangular or circular)."""
    if cfg['area_type'] == 'rectangular':
        return dict(
            minlatitude=cfg['lat_min'],  maxlatitude=cfg['lat_max'],
            minlongitude=cfg['lon_min'], maxlongitude=cfg['lon_max'],
        )
    else:
        return dict(
            latitude=cfg['lat_center'],
            longitude=cfg['lon_center'],
            maxradius=_radius_km_to_deg(cfg['radius_km']),
        )


def _area_kwargs_stations(cfg: dict) -> dict:
    """Geographic kwargs for the STATION query (always circular)."""
    return dict(
        latitude=cfg['lat_center_sta'],
        longitude=cfg['lon_center_sta'],
        maxradius=_radius_km_to_deg(cfg['radius_km_sta']),
    )


def query_events(client: Client, cfg: dict) -> Catalog:
    """Download the event catalog in time chunks to avoid server limits."""
    tmin  = UTCDateTime(cfg['tmin'])
    tmax  = UTCDateTime(cfg['tmax'])
    chunk = cfg.get('chunk_days', 365) * 86400   # seconds

    geo = _area_kwargs_events(cfg)

    # Optional magnitude / depth filters
    filters = {}
    if cfg.get('mag_min')      is not None: filters['minmagnitude'] = cfg['mag_min']
    if cfg.get('mag_max')      is not None: filters['maxmagnitude'] = cfg['mag_max']
    if cfg.get('depth_min_km') is not None: filters['mindepth']     = cfg['depth_min_km']
    if cfg.get('depth_max_km') is not None: filters['maxdepth']     = cfg['depth_max_km']

    cat = Catalog()
    t1  = tmin
    while t1 < tmax:
        t2 = min(t1 + chunk, tmax)
        label = (f"{t1.strftime('%Y-%m-%d')} → {t2.strftime('%Y-%m-%d')}")
        print(f"  Querying events  {label} … ", end='', flush=True)
        try:
            tmp = client.get_events(starttime=t1, endtime=t2,
                                    **geo, **filters)
            cat += tmp
            print(f"{len(tmp)} events")
        except Exception as exc:
            s = str(exc)
            if 'No data' in s or '204' in s:
                print("0 events")
            else:
                print(f"WARNING – {exc}")
        t1 = t2

    print(f"\n  ── Total events found: {len(cat)} ──\n")
    return cat


def query_stations(client: Client, cfg: dict) -> object:
    """Download station inventory (includes full instrument response)."""
    tmin = UTCDateTime(cfg['tmin'])
    tmax = UTCDateTime(cfg['tmax'])
    geo  = _area_kwargs_stations(cfg)

    print("  Querying stations … ", end='', flush=True)
    try:
        inv = client.get_stations(
            starttime=tmin, endtime=tmax,
            network=cfg['network'], station=cfg['station'],
            location=cfg['location'], channel=cfg['channel'],
            level='response',
            **geo,
        )
        n_sta = sum(len(net) for net in inv)
        n_net = len(inv)
        print(f"{n_sta} stations across {n_net} network(s)\n")
        return inv
    except Exception as exc:
        s = str(exc)
        if 'No data' in s or '204' in s:
            print("0 stations found\n")
        else:
            print(f"WARNING – {exc}\n")
        return None


def get_stations(client: Client, cfg: dict) -> object:
    """Return station inventory: from an existing StationXML file or FDSN.

    Controlled by config key 'existing_stations_xml'.
    """
    existing = cfg.get('existing_stations_xml') or None
    if existing:
        print("  Loading stations from existing XML …")
        return load_stations_from_xml(_resolve_xml_path(existing, META_DIR))
    return query_stations(client, cfg)


# ═════════════════════════════════════════════════════════════════════════════
# 3 ─ PREVIEW MAP
# ═════════════════════════════════════════════════════════════════════════════

# One distinct color per integer magnitude value (floor).
# Keys are the integer magnitude floor; the last entry is used for all higher values.
_MAG_COLOR_MAP = {
    0: '#BDBDBD',   # light gray
    1: '#FFEE58',   # yellow
    2: '#FFA726',   # orange
    3: '#FF5722',   # deep orange
    4: '#E53935',   # red
    5: '#AD1457',   # pink/crimson
    6: '#6A1B9A',   # purple
    7: '#1565C0',   # blue
    8: '#00695C',   # dark teal
}
_MAG_COLOR_MAX_KEY  = max(_MAG_COLOR_MAP)
_MAG_COLOR_OVERFLOW = '#212121'   # near-black for M ≥ 9


def _mag_color(mag: float) -> str:
    """Return a CSS color for a given magnitude (one color per integer floor)."""
    key = max(0, int(mag))   # floor; negative mags → 0
    if key > _MAG_COLOR_MAX_KEY:
        return _MAG_COLOR_OVERFLOW
    return _MAG_COLOR_MAP[key]


def _build_folium_map(cat, inv, cfg: dict):
    """Build the folium map.  cat=None signals continuous (no-event) mode."""
    import folium

    is_continuous = (cat is None)

    # Map centre: station area centre in continuous mode, event area otherwise
    if is_continuous or cfg['area_type'] == 'circular':
        clat = cfg['lat_center_sta'] if is_continuous else cfg['lat_center']
        clon = cfg['lon_center_sta'] if is_continuous else cfg['lon_center']
    else:
        clat = (cfg['lat_min'] + cfg['lat_max']) / 2
        clon = (cfg['lon_min'] + cfg['lon_max']) / 2

    m = folium.Map(location=[clat, clon], zoom_start=9,
                   tiles='CartoDB positron')

    # ── event search area boundary (event mode only) ──────────────────────────
    if not is_continuous:
        if cfg['area_type'] == 'rectangular':
            folium.Rectangle(
                bounds=[[cfg['lat_min'], cfg['lon_min']],
                        [cfg['lat_max'], cfg['lon_max']]],
                color='#607D8B', weight=2, dash_array='6',
                fill=False, tooltip='Event search area',
            ).add_to(m)
        else:
            folium.Circle(
                location=[cfg['lat_center'], cfg['lon_center']],
                radius=cfg['radius_km'] * 1000,
                color='#607D8B', weight=2, dash_array='6',
                fill=False, tooltip='Event search area',
            ).add_to(m)

    # ── station search area boundary (always) ─────────────────────────────────
    folium.Circle(
        location=[cfg['lat_center_sta'], cfg['lon_center_sta']],
        radius=cfg['radius_km_sta'] * 1000,
        color='#1565C0', weight=2, dash_array='4',
        fill=False, tooltip='Station search area',
    ).add_to(m)

    # ── events (event mode only) ──────────────────────────────────────────────
    if not is_continuous:
        ev_group = folium.FeatureGroup(name=f'Events ({len(cat)})')
        for ev in cat:
            try:
                origin = ev.preferred_origin() or ev.origins[0]
                mag    = (ev.preferred_magnitude() or ev.magnitudes[0]).mag
            except (IndexError, AttributeError):
                continue
            t_str    = origin.time.strftime('%Y-%m-%d  %H:%M:%S')
            depth_km = (origin.depth or 0.0) / 1000.0
            popup_html = (
                f"<b>{t_str} UTC</b><br>"
                f"Magnitude : {mag:.1f}<br>"
                f"Depth     : {depth_km:.1f} km<br>"
                f"Lat / Lon : {origin.latitude:.4f} / {origin.longitude:.4f}"
            )
            folium.CircleMarker(
                location=[origin.latitude, origin.longitude],
                radius=max(4, mag * 2.5),
                color=_mag_color(mag),
                fill=True, fill_color=_mag_color(mag), fill_opacity=0.75,
                popup=folium.Popup(popup_html, max_width=240),
                tooltip=f"M {mag:.1f}",
            ).add_to(ev_group)
        ev_group.add_to(m)

    # ── stations (CSS triangle marker) ───────────────────────────────────────
    n_sta = sum(len(net) for net in inv) if inv else 0
    sta_group = folium.FeatureGroup(name=f'Stations ({n_sta})')
    if inv is not None:
        for net in inv:
            for sta in net:
                popup_html = (
                    f"<b>{net.code}.{sta.code}</b><br>"
                    f"{sta.site.name}<br>"
                    f"Lat / Lon : {sta.latitude:.4f} / {sta.longitude:.4f}"
                )
                # Upward-pointing triangle via CSS border trick
                triangle_html = (
                    '<div style="'
                    'width:0;height:0;'
                    'border-left:8px solid transparent;'
                    'border-right:8px solid transparent;'
                    'border-bottom:16px solid #1565C0;'
                    '"></div>'
                )
                folium.Marker(
                    location=[sta.latitude, sta.longitude],
                    icon=folium.DivIcon(
                        html=triangle_html,
                        icon_size=(16, 16),
                        icon_anchor=(8, 16),
                    ),
                    popup=folium.Popup(popup_html, max_width=200),
                    tooltip=f"{net.code}.{sta.code}",
                ).add_to(sta_group)
    sta_group.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    # ── bottom-left panel: magnitude legend OR continuous info ────────────────
    if is_continuous:
        n_sta   = sum(len(net) for net in inv) if inv else 0
        t0      = UTCDateTime(cfg['tmin'])
        t1_end  = UTCDateTime(cfg['tmax'])
        n_days  = max(1, int((t1_end - t0) / 86400))
        ch      = cfg.get('chunk_hours', 24)
        n_files = n_sta * int(n_days * 24 / ch)
        panel_html = f"""
    <div style="
        position: fixed; bottom: 30px; left: 30px; z-index: 1000;
        background: white; padding: 12px 16px; border-radius: 8px;
        border: 1px solid #ccc; font-size: 12px; line-height: 1.9;
        box-shadow: 2px 2px 6px rgba(0,0,0,.2);">
      <b>Continuous download</b><br>
      {cfg['tmin'][:10]} → {cfg['tmax'][:10]}<br>
      Stations  : {n_sta}<br>
      Chunk     : {ch} h / file<br>
      Est. files: ~{n_files}<br>
      <span style="color:#1565C0;font-size:16px">▲</span>&nbsp;Station<br>
      <span style="color:#1565C0">─ ─</span>&nbsp;Station search area
    </div>"""
        m.get_root().html.add_child(folium.Element(panel_html))
    else:
        rows = ''
        for m_int, color in sorted(_MAG_COLOR_MAP.items()):
            label = (f'M ≥ {m_int}' if m_int == _MAG_COLOR_MAX_KEY
                     else f'M {m_int}')
            rows += (f'<span style="color:{color};font-size:16px">●</span>'
                     f'&nbsp;{label}<br>\n')
        rows += (f'<span style="color:{_MAG_COLOR_OVERFLOW};font-size:16px">●</span>'
                 f'&nbsp;M ≥ {_MAG_COLOR_MAX_KEY + 1}<br>\n')
        rows += ('<span style="color:#1565C0;font-size:16px">▲</span>&nbsp;Station<br>\n'
                 '<span style="color:#607D8B">─ ─</span>&nbsp;Event area&nbsp;&nbsp;'
                 '<span style="color:#1565C0">─ ─</span>&nbsp;Station area')
        legend_html = f"""
    <div style="
        position: fixed; bottom: 30px; left: 30px; z-index: 1000;
        background: white; padding: 12px 16px; border-radius: 8px;
        border: 1px solid #ccc; font-size: 12px; line-height: 1.8;
        box-shadow: 2px 2px 6px rgba(0,0,0,.2);">
      <b>Legend</b><br>
      {rows}
    </div>"""
        m.get_root().html.add_child(folium.Element(legend_html))

    return m


def _build_pygmt_map(cat: Catalog, inv, cfg: dict):
    import pygmt
    import numpy as np

    # Map region with a small margin
    if cfg['area_type'] == 'rectangular':
        margin = 0.1
        region = [
            cfg['lon_min'] - margin, cfg['lon_max'] + margin,
            cfg['lat_min'] - margin, cfg['lat_max'] + margin,
        ]
    else:
        r = _radius_km_to_deg(cfg['radius_km']) + 0.05
        region = [
            cfg['lon_center'] - r, cfg['lon_center'] + r,
            cfg['lat_center'] - r, cfg['lat_center'] + r,
        ]

    fig = pygmt.Figure()
    fig.basemap(region=region, projection='M15c', frame='a')
    fig.coast(land='gray90', water='lightblue',
              shorelines='1/0.4p,black', resolution='h')

    # Search area boundary
    if cfg['area_type'] == 'rectangular':
        fig.plot(
            x=[cfg['lon_min'], cfg['lon_max'],
               cfg['lon_max'], cfg['lon_min'], cfg['lon_min']],
            y=[cfg['lat_min'], cfg['lat_min'],
               cfg['lat_max'], cfg['lat_max'], cfg['lat_min']],
            pen='1p,gray50,dashed',
        )
    else:
        angles = np.linspace(0, 360, 361)
        r_deg  = _radius_km_to_deg(cfg['radius_km'])
        fig.plot(
            x=cfg['lon_center'] + r_deg * np.cos(np.radians(angles)),
            y=cfg['lat_center'] + r_deg * np.sin(np.radians(angles)),
            pen='1p,gray50,dashed',
        )

    # Events
    ev_lons, ev_lats, ev_mags = [], [], []
    for ev in cat:
        try:
            o   = ev.preferred_origin() or ev.origins[0]
            mag = (ev.preferred_magnitude() or ev.magnitudes[0]).mag
            ev_lons.append(o.longitude)
            ev_lats.append(o.latitude)
            ev_mags.append(mag)
        except (IndexError, AttributeError):
            continue
    if ev_lons:
        sizes = [max(0.08, m * 0.04) for m in ev_mags]
        fig.plot(x=ev_lons, y=ev_lats, size=sizes,
                 style='cc', color='red', pen='0.3p,darkred')

    # Stations
    if inv is not None:
        sta_lons = [sta.longitude for net in inv for sta in net]
        sta_lats = [sta.latitude  for net in inv for sta in net]
        if sta_lons:
            fig.plot(x=sta_lons, y=sta_lats,
                     style='t0.35c', color='royalblue', pen='0.5p,navy')

    fig.show()
    return fig


def show_preview_map(cat: Catalog, inv, cfg: dict) -> None:
    """Display the preview map; tries folium first, then pygmt."""
    backend = cfg.get('map_backend', 'folium')

    if backend == 'folium':
        try:
            m = _build_folium_map(cat, inv, cfg)
            tmp = tempfile.NamedTemporaryFile(
                suffix='.html', delete=False,
                prefix='seismic_preview_',
            )
            tmp.close()   # release handle before folium writes to the path
            m.save(tmp.name)
            print(f"  Map saved to: {tmp.name}")
            # macOS: use the native 'open' command (webbrowser.open is unreliable
            # for file:// URLs when launched from a terminal)
            if platform.system() == 'Darwin':
                subprocess.run(['open', tmp.name])
            else:
                webbrowser.open(f'file://{tmp.name}')
            return
        except ImportError:
            print("  [WARN] folium not installed – falling back to pygmt.")
            backend = 'pygmt'

    if backend == 'pygmt':
        try:
            _build_pygmt_map(cat, inv, cfg)
            return
        except ImportError:
            print("  [WARN] pygmt not installed either. No map shown.")


# ═════════════════════════════════════════════════════════════════════════════
# 4 ─ SAVE CATALOG
# ═════════════════════════════════════════════════════════════════════════════

def _obspy_ev_to_pyrocko(ev, label: str) -> model.Event | None:
    """Convert an obspy Event to a pyrocko model.Event.

    The event name follows the convention:  <label>_yyyy_mm_dd_hh_mm_ss
    which is also used as the DATA subfolder name and waveform file prefix.
    """
    try:
        origin = ev.preferred_origin() or ev.origins[0]
    except IndexError:
        return None

    try:
        mag = (ev.preferred_magnitude() or ev.magnitudes[0]).mag
    except (IndexError, AttributeError):
        mag = None

    # Build pyrocko time from obspy UTCDateTime string
    t_raw   = str(origin.time)                          # e.g. "2024-01-15T03:22:10.123456Z"
    t_str   = t_raw[0:10] + ' ' + t_raw[11:23]         # "2024-01-15 03:22:10.123"
    ptime   = util.str_to_time(t_str)

    # Name: label_yyyy_mm_dd_hh_mm_ss
    ts   = util.time_to_str(ptime)                      # "yyyy-mm-dd hh:mm:ss.sss"
    name = (label + '_'
            + ts[0:4] + '_' + ts[5:7]  + '_' + ts[8:10] + '_'
            + ts[11:13] + '_' + ts[14:16] + '_' + ts[17:19])

    return model.Event(
        name=name,
        time=ptime,
        lat=float(origin.latitude),
        lon=float(origin.longitude),
        depth=float(origin.depth) if origin.depth is not None else 0.0,
        magnitude=float(mag) if mag is not None else None,
    )


def save_catalog(cat: Catalog, cfg: dict) -> list:
    """Save catalog in QuakeML, Pyrocko .pf and plain-text formats.

    Returns the list of pyrocko model.Event objects (used for waveform naming).
    """
    os.makedirs(CAT_DIR, exist_ok=True)
    base  = os.path.join(CAT_DIR, cfg['catalog_name'])
    label = cfg['event_label']

    # ── QuakeML (.xml) ───────────────────────────────────────────────────────
    xml_path = base + '.xml'
    cat.write(xml_path, format='QUAKEML')
    print(f"  Saved: {xml_path}")

    # ── Pyrocko basic format (.pf) ───────────────────────────────────────────
    pf_events = []
    for ev in cat:
        pev = _obspy_ev_to_pyrocko(ev, label)
        if pev is not None:
            pf_events.append(pev)
    pf_events.sort(key=lambda e: e.time)

    pf_path = base + '.pf'
    model.dump_events(pf_events, pf_path)   # default format='basic'
    print(f"  Saved: {pf_path}")

    # ── Human-readable table (.txt) ──────────────────────────────────────────
    txt_path = base + '.txt'
    with open(txt_path, 'w') as fh:
        header = (f"{'#':>5}  {'Time (UTC)':23}  {'Lat':>9}  {'Lon':>9}  "
                  f"{'Depth(km)':>9}  {'Mag':>5}  Name\n")
        fh.write(header)
        fh.write('─' * (len(header) - 1) + '\n')
        for i, pev in enumerate(pf_events, 1):
            t   = util.time_to_str(pev.time)
            dep = pev.depth / 1000.0 if pev.depth is not None else 0.0
            mag = f"{pev.magnitude:.2f}" if pev.magnitude is not None else '   –'
            fh.write(f"{i:>5}  {t:23}  {pev.lat:>9.4f}  {pev.lon:>9.4f}  "
                     f"{dep:>9.2f}  {mag:>5}  {pev.name}\n")
    print(f"  Saved: {txt_path}")

    return pf_events


# ═════════════════════════════════════════════════════════════════════════════
# 5 ─ SAVE STATION METADATA
# ═════════════════════════════════════════════════════════════════════════════

def save_stations(inv, cfg: dict) -> None:
    """Write StationXML (with response) to META_DATA/."""
    if inv is None:
        print("  [SKIP] No inventory to save.")
        return
    os.makedirs(META_DIR, exist_ok=True)
    path = os.path.join(META_DIR, cfg['station_file_name'] + '.xml')
    inv.write(path, format='STATIONXML')
    print(f"  Saved: {path}")


# ═════════════════════════════════════════════════════════════════════════════
# 6 ─ DOWNLOAD WAVEFORMS
# ═════════════════════════════════════════════════════════════════════════════

def _ev_time_str(pev: model.Event) -> str:
    """Extract 'yyyy_mm_dd_hh_mm_ss' from the event origin time field."""
    ts = util.time_to_str(pev.time)   # "yyyy-mm-dd hh:mm:ss.sss"
    return (ts[0:4] + '_' + ts[5:7] + '_' + ts[8:10] + '_'
            + ts[11:13] + '_' + ts[14:16] + '_' + ts[17:19])


def download_waveforms(pf_events: list, inv, client: Client, cfg: dict) -> None:
    """Download one miniSEED file per (event, station) pair.

    Files already on disk are silently skipped so the script is resumable.
    """
    if inv is None:
        print("  [SKIP] No inventory available – cannot download waveforms.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)

    label  = cfg['event_label']
    tbef   = cfg['t_before_s']
    taft   = cfg['t_after_s']
    chan   = cfg['channel']
    n_ev   = len(pf_events)
    n_dl   = 0
    n_skip = 0
    n_err  = 0

    for idx, pev in enumerate(pf_events, 1):
        # Event folder name = pev.name  (already contains label + timestamp)
        ev_name = pev.name
        ev_dir  = os.path.join(DATA_DIR, ev_name)
        t0      = UTCDateTime(util.time_to_str(pev.time))

        print(f"\n[{idx:>4}/{n_ev}]  {ev_name}")
        os.makedirs(ev_dir, exist_ok=True)

        for net in inv:
            for sta in net:
                # One file per station: ev_name_NET.STA.mseed
                fname    = f"{ev_name}_{net.code}.{sta.code}.mseed"
                out_path = os.path.join(ev_dir, fname)

                if os.path.isfile(out_path):
                    print(f"  [SKIP]   {net.code}.{sta.code}  (already on disk)")
                    n_skip += 1
                    continue

                try:
                    st = client.get_waveforms(
                        network=net.code,
                        station=sta.code,
                        location='*',
                        channel=chan,
                        starttime=t0 - tbef,
                        endtime=t0 + taft,
                    )
                    if len(st) == 0:
                        print(f"  [EMPTY]  {net.code}.{sta.code}")
                        continue
                    st.write(out_path, format='MSEED')
                    print(f"  [OK]     {net.code}.{sta.code}  "
                          f"({len(st)} trace(s))")
                    n_dl += 1

                except Exception as exc:
                    msg = str(exc)
                    if 'No data' in msg or '204' in msg:
                        print(f"  [–]      {net.code}.{sta.code}  no data")
                    else:
                        print(f"  [ERR]    {net.code}.{sta.code}  {msg}")
                        n_err += 1

    print(f"\n{'─' * 54}")
    print(f"  Waveform files downloaded  : {n_dl}")
    print(f"  Waveform files skipped     : {n_skip}  (already on disk)")
    print(f"  Errors                     : {n_err}")
    print(f"{'─' * 54}\n")


# ═════════════════════════════════════════════════════════════════════════════
# 7 ─ CONTINUOUS WAVEFORM DOWNLOAD
# ═════════════════════════════════════════════════════════════════════════════

def download_continuous(inv, client: Client, cfg: dict) -> None:
    """Download continuous waveforms in fixed-size time chunks.

    Output: DATA/CONTINUOUS/{event_label}_yyyy_mm_dd_{NET}_{STA}.mseed
    Files already on disk are skipped (resumable).
    """
    if inv is None:
        print("  [SKIP] No inventory – cannot download waveforms.")
        return

    tmin  = UTCDateTime(cfg['tmin'])
    tmax  = UTCDateTime(cfg['tmax'])
    chunk = cfg.get('chunk_hours', 24) * 3600   # seconds
    label = cfg['event_label']
    chan  = cfg['channel']

    cont_dir = os.path.join(DATA_DIR, 'CONTINUOUS')
    os.makedirs(cont_dir, exist_ok=True)

    n_dl   = 0
    n_skip = 0
    n_err  = 0

    t1 = tmin
    while t1 < tmax:
        t2       = min(t1 + chunk, tmax)
        date_str = t1.strftime('%Y_%m_%d')   # used in filename
        print(f"\n  [{t1.strftime('%Y-%m-%d  %H:%M')} → {t2.strftime('%H:%M')}]")

        for net in inv:
            for sta in net:
                fname    = f"{label}_{date_str}_{net.code}_{sta.code}.mseed"
                out_path = os.path.join(cont_dir, fname)

                if os.path.isfile(out_path):
                    print(f"  [SKIP]   {net.code}.{sta.code}  (already on disk)")
                    n_skip += 1
                    continue

                try:
                    st = client.get_waveforms(
                        network=net.code, station=sta.code,
                        location='*', channel=chan,
                        starttime=t1, endtime=t2,
                    )
                    if len(st) == 0:
                        print(f"  [EMPTY]  {net.code}.{sta.code}")
                        continue
                    st.write(out_path, format='MSEED')
                    print(f"  [OK]     {net.code}.{sta.code}  ({len(st)} trace(s))")
                    n_dl += 1
                except Exception as exc:
                    msg = str(exc)
                    if 'No data' in msg or '204' in msg:
                        print(f"  [–]      {net.code}.{sta.code}  no data")
                    else:
                        print(f"  [ERR]    {net.code}.{sta.code}  {msg}")
                        n_err += 1

        t1 = t2

    print(f"\n{'─' * 54}")
    print(f"  Files downloaded : {n_dl}")
    print(f"  Files skipped    : {n_skip}  (already on disk)")
    print(f"  Errors           : {n_err}")
    print(f"{'─' * 54}\n")


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # ── Command-line argument: config file name or path ───────────────────────
    parser = argparse.ArgumentParser(
        description='FDSN seismic data downloader (catalog + stations + waveforms)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Examples:\n'
            '  python download_catalog_waveforms_station_xml.py\n'
            '  python download_catalog_waveforms_station_xml.py campi_flegrei.yaml\n'
            '  python download_catalog_waveforms_station_xml.py /abs/path/my_config.yaml\n\n'
            'If only a filename is given (no path separators), the script looks\n'
            f'for it inside  CAT/config_cat/  (i.e. {CONFIG_DIR}).'
        ),
    )
    parser.add_argument(
        'config',
        nargs='?',
        default='config.yaml',
        metavar='CONFIG_FILE',
        help='config filename or absolute path  (default: config.yaml)',
    )
    args = parser.parse_args()

    # Resolve config path: bare filename → CONFIG_DIR; otherwise use as-is
    cfg_arg = args.config
    if os.sep not in cfg_arg and '/' not in cfg_arg:
        config_path = os.path.join(CONFIG_DIR, cfg_arg)
    else:
        config_path = os.path.abspath(cfg_arg)

    w = 64
    print("\n" + "═" * w)
    print("  FDSN SEISMIC DATA DOWNLOADER  –  ObsPy + Pyrocko")
    print("═" * w)
    print(f"  Config: {config_path}\n")

    # ── 1. Configuration ──────────────────────────────────────────────────────
    cfg = load_or_create_config(config_path)
    print_config_summary(cfg)
    cfg = ask_modify_config(config_path, cfg)

    # ── 2. Connect to FDSN ────────────────────────────────────────────────────
    # Main client: used for stations and waveforms
    print(f"\n  Connecting to {cfg['fdsn_site']} …", end=' ', flush=True)
    client = Client(cfg['fdsn_site'])
    print("Connected.")

    # Optional separate client for event queries (e.g. IRIS → USGS)
    ev_site = cfg.get('fdsn_site_events') or None
    if ev_site and ev_site != cfg['fdsn_site']:
        print(f"  Connecting to {ev_site} (events) …", end=' ', flush=True)
        event_client = Client(ev_site)
        print("Connected.")
    else:
        event_client = client
    print()

    mode = cfg.get('download_mode', 'event')

    # ══════════════════════════════════════════════════════════════════════════
    if mode == 'continuous':
    # ══════════════════════════════════════════════════════════════════════════

        # ── 3. Stations ───────────────────────────────────────────────────────
        print(f"[STEP 1/3]  Loading station inventory …")
        inv   = get_stations(client, cfg)
        n_sta = sum(len(net) for net in inv) if inv else 0

        # ── 4. Preview map (stations only) ────────────────────────────────────
        print(f"[STEP 2/3]  Building preview map "
              f"({cfg.get('map_backend', 'folium')}) …\n")
        show_preview_map(None, inv, cfg)   # cat=None → continuous map

        # ── 5. Confirmation ───────────────────────────────────────────────────
        chunk_h = cfg.get('chunk_hours', 24)
        tspan   = UTCDateTime(cfg['tmax']) - UTCDateTime(cfg['tmin'])
        n_days  = max(1, int(tspan / 86400))
        est     = n_sta * int(n_days * 24 / chunk_h)
        print(f"\n  Summary before download:")
        print(f"    Mode      : CONTINUOUS")
        print(f"    Period    : {cfg['tmin']}  →  {cfg['tmax']}  ({n_days} days)")
        print(f"    Stations  : {n_sta}")
        print(f"    Chunk     : {chunk_h} h / file   (~{est} files total)")
        print(f"    Output    : {os.path.join(DATA_DIR, 'CONTINUOUS')}\n")

        ans = input("  Proceed with download? [y/N]: ").strip().lower()
        if ans != 'y':
            print("\n  Aborted. Nothing was saved.\n")
            sys.exit(0)

        # ── 6. Save stations + download ───────────────────────────────────────
        print(f"\n{'─' * w}")
        print("  Saving station metadata …")
        save_stations(inv, cfg)

        print(f"\n{'─' * w}")
        print(f"[STEP 3/3]  Downloading continuous waveforms …")
        download_continuous(inv, client, cfg)

    # ══════════════════════════════════════════════════════════════════════════
    else:   # mode == 'event'
    # ══════════════════════════════════════════════════════════════════════════

        # ── 3. Events: query FDSN ─────────────────────────────────────────────
        ev_site_label = ev_site if ev_site else cfg['fdsn_site']
        print(f"[STEP 1/4]  Querying event catalog from {ev_site_label} …")
        cat = query_events(event_client, cfg)

        if len(cat) == 0:
            print("\n  No events found. Check your parameters and try again.\n")
            sys.exit(0)

        # ── 4. Stations ───────────────────────────────────────────────────────
        print(f"[STEP 2/4]  Loading station inventory …")
        inv   = get_stations(client, cfg)
        n_sta = sum(len(net) for net in inv) if inv else 0

        # ── 5. Preview map ────────────────────────────────────────────────────
        print(f"[STEP 3/4]  Building preview map "
              f"({cfg.get('map_backend', 'folium')}) …\n")
        show_preview_map(cat, inv, cfg)

        # ── 6. Confirmation ───────────────────────────────────────────────────
        print(f"\n  Summary before download:")
        print(f"    Mode      : EVENT")
        print(f"    Events    : {len(cat)}")
        print(f"    Stations  : {n_sta}")
        print(f"    Window    : -{cfg['t_before_s']} s  /  +{cfg['t_after_s']} s")
        print(f"    Output    : {WORK_DIR}\n")

        ans = input("  Proceed with download? [y/N]: ").strip().lower()
        if ans != 'y':
            print("\n  Aborted. Nothing was saved.\n")
            sys.exit(0)

        # ── 7. Save catalog ───────────────────────────────────────────────────
        print(f"\n{'─' * w}")
        print("  Saving catalog …")
        pf_events = save_catalog(cat, cfg)

        # ── 8. Save stations ──────────────────────────────────────────────────
        print(f"\n{'─' * w}")
        print("  Saving station metadata …")
        save_stations(inv, cfg)

        # ── 9. Download event waveforms ───────────────────────────────────────
        print(f"\n{'─' * w}")
        print(f"[STEP 4/4]  Downloading event waveforms …")
        download_waveforms(pf_events, inv, client, cfg)

    print(f"{'═' * w}")
    print("  All done.\n")


if __name__ == '__main__':
    main()
