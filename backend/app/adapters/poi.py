"""POI 适配器：门店距离计算（Haversine），生产替换为高德/百度。"""
from __future__ import annotations

import math
from typing import Any


def haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> int:
    radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return int(round(2 * radius * math.asin(math.sqrt(a))))


def store_distance_m(store: dict[str, Any], lng: float, lat: float) -> int:
    if not store or store.get("lng") is None:
        return 0
    return haversine_m(float(store["lng"]), float(store["lat"]), lng, lat)