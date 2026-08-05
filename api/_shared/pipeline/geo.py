"""
Region geography: lat/lon coordinates and continent groupings for the dashboard.

Coordinates are approximate datacenter locations published by Microsoft on the
Azure Geographies page. Used for the world-map view.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# (latitude, longitude, continent, country)
REGION_GEO: Dict[str, Tuple[float, float, str, str]] = {
    "australia east":         (-33.86, 151.21, "Asia Pacific", "Australia"),
    "australia southeast":    (-37.81, 144.96, "Asia Pacific", "Australia"),
    "australia central":      (-35.31, 149.12, "Asia Pacific", "Australia"),
    "austria east":           (48.21, 16.37,   "Europe", "Austria"),
    "belgium central":        (50.85, 4.35,    "Europe", "Belgium"),
    "brazil south":           (-23.55, -46.63, "Americas", "Brazil"),
    "brazil southeast":       (-22.91, -43.21, "Americas", "Brazil"),
    "canada central":         (43.65, -79.38,  "Americas", "Canada"),
    "canada east":            (46.81, -71.21,  "Americas", "Canada"),
    "central india":          (18.52, 73.86,   "Asia Pacific", "India"),
    "central us":             (41.59, -93.62,  "Americas", "United States"),
    "chile central":          (-33.45, -70.66, "Americas", "Chile"),
    "denmark east":           (55.68, 12.57,   "Europe", "Denmark"),
    "east asia":              (22.27, 114.16,  "Asia Pacific", "Hong Kong"),
    "east us":                (37.43, -78.65,  "Americas", "United States"),
    "east us 2":              (36.66, -78.39,  "Americas", "United States"),
    "france central":         (46.35, 2.42,    "Europe", "France"),
    "france south":           (43.30, 5.37,    "Europe", "France"),
    "germany north":          (53.55, 9.99,    "Europe", "Germany"),
    "germany west central":   (50.11, 8.68,    "Europe", "Germany"),
    "indonesia central":      (-6.21, 106.85,  "Asia Pacific", "Indonesia"),
    "israel central":         (31.77, 35.21,   "Middle East", "Israel"),
    "italy north":            (45.46, 9.19,    "Europe", "Italy"),
    "japan east":             (35.68, 139.69,  "Asia Pacific", "Japan"),
    "japan west":             (34.69, 135.50,  "Asia Pacific", "Japan"),
    "korea central":          (37.57, 126.98,  "Asia Pacific", "South Korea"),
    "korea south":            (35.18, 129.08,  "Asia Pacific", "South Korea"),
    "malaysia west":          (3.14, 101.69,   "Asia Pacific", "Malaysia"),
    "mexico central":         (20.59, -100.39, "Americas", "Mexico"),
    "new zealand north":      (-36.85, 174.76, "Asia Pacific", "New Zealand"),
    "north central us":       (41.88, -87.63,  "Americas", "United States"),
    "north europe":           (53.35, -6.26,   "Europe", "Ireland"),
    "norway east":            (59.91, 10.75,   "Europe", "Norway"),
    "poland central":         (52.23, 21.01,   "Europe", "Poland"),
    "qatar central":          (25.29, 51.53,   "Middle East", "Qatar"),
    "south africa north":     (-25.75, 28.19,  "Africa", "South Africa"),
    "south africa west":      (-33.92, 18.42,  "Africa", "South Africa"),
    "south central us":       (29.42, -98.49,  "Americas", "United States"),
    "south india":            (13.08, 80.27,   "Asia Pacific", "India"),
    "southeast asia":         (1.35, 103.82,   "Asia Pacific", "Singapore"),
    "spain central":          (40.42, -3.70,   "Europe", "Spain"),
    "sweden central":         (60.67, 17.14,   "Europe", "Sweden"),
    "switzerland north":      (47.45, 8.56,    "Europe", "Switzerland"),
    "switzerland west":       (46.20, 6.14,    "Europe", "Switzerland"),
    "uae central":            (24.47, 54.37,   "Middle East", "UAE"),
    "uae north":              (25.27, 55.30,   "Middle East", "UAE"),
    "uk south":               (50.94, -0.13,   "Europe", "United Kingdom"),
    "uk west":                (51.48, -3.18,   "Europe", "United Kingdom"),
    "us gov virginia":        (37.43, -78.65,  "Americas", "United States"),
    "west central us":        (40.89, -110.85, "Americas", "United States"),
    "west europe":            (52.37, 4.89,    "Europe", "Netherlands"),
    "west india":             (19.07, 72.87,   "Asia Pacific", "India"),
    "west us":                (37.78, -122.42, "Americas", "United States"),
    "west us 2":              (47.23, -119.85, "Americas", "United States"),
    "west us 3":              (33.45, -112.07, "Americas", "United States"),
}


def lookup(display_name: str) -> Dict[str, Optional[object]]:
    """Return {lat, lon, continent, country} for a display name (case-insensitive)."""
    rec = REGION_GEO.get(display_name.lower())
    if rec is None:
        return {"lat": None, "lon": None, "continent": "Unknown", "country": "Unknown"}
    lat, lon, continent, country = rec
    return {"lat": lat, "lon": lon, "continent": continent, "country": country}


def continents() -> List[str]:
    return sorted({v[2] for v in REGION_GEO.values()})


def coords(display_name: str) -> Optional[Tuple[float, float]]:
    """Return ``(lat, lon)`` for a display name, or ``None`` if unknown."""
    rec = REGION_GEO.get(display_name.lower())
    if rec is None:
        return None
    return (rec[0], rec[1])


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two (lat, lon) points in kilometers.

    Used by ``model.alternatives`` to compute distance-based fallback
    recommendations when neither the published latency table nor the
    curated ``GEO_FALLBACK`` list yields healthy candidates (e.g. for
    newly-launched regions like Indonesia Central).
    """
    import math
    earth_km = 6371.0088
    r1 = math.radians(lat1)
    r2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return earth_km * c
