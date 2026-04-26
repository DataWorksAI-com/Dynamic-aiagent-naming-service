"""
agentns.geo_policy
==================
Pluggable geo-selection strategy for server ranking.

The GeoPolicy ABC lets you swap in different server-ranking algorithms
without touching the rest of the resolution stack.

Built-in policies
-----------------
    NearestPolicy     — pure Haversine distance (CDN-style edge selection)
    LeastLoadedPolicy — ignores geography, ranks by current load %
    CompositePolicy   — default: balances distance + RTT + load into one score

Usage
-----
    from agentns.geo_policy import CompositePolicy, NearestPolicy, LeastLoadedPolicy
    from agentns.server_selection import rank_servers

    # Use the default (CompositePolicy):
    ranked = rank_servers(servers, health_map, requester_context)

    # Override with a custom policy:
    ranked = rank_servers(servers, health_map, requester_context,
                          geo_policy=NearestPolicy())

    # Write your own:
    class LatencyOnlyPolicy(GeoPolicy):
        def score(self, server, health, requester_latlon):
            return health.get("response_time_ms", 9999.0)

    ranked = rank_servers(servers, health_map, ctx, geo_policy=LatencyOnlyPolicy())
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple


# ── Haversine helper (self-contained — no circular import with server_selection) ──

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def _server_latlon(server: Dict) -> Optional[Tuple[float, float]]:
    """Extract (lat, lon) from a server dict, or return None."""
    loc = server.get("location") or {}
    lat = loc.get("latitude") or loc.get("lat")
    lon = loc.get("longitude") or loc.get("lon") or loc.get("lng")
    if lat is not None and lon is not None:
        return (float(lat), float(lon))
    return None


# ── Abstract base ──────────────────────────────────────────────────────────────

class GeoPolicy(ABC):
    """
    Abstract geo-selection strategy used by rank_servers().

    Implement score() to control how servers are ranked.
    Lower score = better ranking (server is chosen first).

    Parameters passed to score()
    -----------------------------
    server:           The server dict from the registry (has "location", "protocols", etc.)
    health:           Health dict from health_checker (has "status", "load", "response_time_ms")
    requester_latlon: (lat, lon) tuple of the requesting agent, or None if unknown

    Example — prefer servers in the same continent:
        class ContinentPolicy(GeoPolicy):
            def score(self, server, health, requester_latlon):
                # Use coarse distance (continental granularity)
                dist = _haversine(*requester_latlon, *_server_latlon(server)) if requester_latlon else 9999
                return dist // 5000  # bucket by 5000 km
    """

    @abstractmethod
    def score(
        self,
        server: Dict,
        health: Dict,
        requester_latlon: Optional[Tuple[float, float]],
    ) -> float:
        """
        Return a sort score for this server. Lower = better.

        Must never raise — return a high number (e.g. math.inf) on error.
        """
        ...


# ── NearestPolicy ──────────────────────────────────────────────────────────────

class NearestPolicy(GeoPolicy):
    """
    Pure Haversine distance — best for CDN-style edge selection.

    Picks the geographically closest server regardless of load or RTT.
    Returns math.inf if either side has no location data.

    Best when:
    - You have edge servers in many regions
    - Latency is primarily driven by network distance
    - Load balancing is handled separately (e.g. by a load balancer)
    """

    def score(
        self,
        server: Dict,
        health: Dict,
        requester_latlon: Optional[Tuple[float, float]],
    ) -> float:
        if requester_latlon is None:
            return math.inf
        server_ll = _server_latlon(server)
        if server_ll is None:
            return math.inf
        return _haversine(
            requester_latlon[0], requester_latlon[1],
            server_ll[0], server_ll[1],
        )


# ── LeastLoadedPolicy ─────────────────────────────────────────────────────────

class LeastLoadedPolicy(GeoPolicy):
    """
    Ignores geography — ranks purely by current load percentage.

    Best when:
    - All servers are co-located (same datacenter)
    - Network latency is negligible
    - You want to spread traffic evenly across instances
    """

    def score(
        self,
        server: Dict,
        health: Dict,
        requester_latlon: Optional[Tuple[float, float]],
    ) -> float:
        return float(health.get("load", 50.0))


# ── CompositePolicy (default) ─────────────────────────────────────────────────

class CompositePolicy(GeoPolicy):
    """
    Default policy — combines distance, RTT, and load into a single score.

    Score = (geo_weight × distance_km) + (rtt_weight × rtt_ms) + (load_weight × load_pct)

    Lower total = server is preferred.

    This preserves the original agentns 5-tuple sort ordering
    (health, protocol, geo, rtt, load) as a single comparable float,
    making it a drop-in replacement for the previous inline sort key.

    Parameters
    ----------
    geo_weight:   multiplier on geographic distance in km  (default: 1.0)
    rtt_weight:   multiplier on response_time_ms           (default: 0.01)
    load_weight:  multiplier on load_percent               (default: 0.1)

    Tuning examples:
        CompositePolicy(geo_weight=0)      # ignore geo, weight RTT + load
        CompositePolicy(rtt_weight=0)      # ignore RTT, weight geo + load
        CompositePolicy(geo_weight=2.0)    # prefer nearest server more strongly
    """

    def __init__(
        self,
        geo_weight: float = 1.0,
        rtt_weight: float = 0.01,
        load_weight: float = 0.1,
    ):
        self.geo_weight  = geo_weight
        self.rtt_weight  = rtt_weight
        self.load_weight = load_weight

    def score(
        self,
        server: Dict,
        health: Dict,
        requester_latlon: Optional[Tuple[float, float]],
    ) -> float:
        # Geographic distance
        if requester_latlon is None:
            geo_km = 0.0   # no geo data — don't penalise; fall through to rtt+load
        else:
            server_ll = _server_latlon(server)
            if server_ll is None:
                geo_km = 9_999.0
            else:
                geo_km = _haversine(
                    requester_latlon[0], requester_latlon[1],
                    server_ll[0], server_ll[1],
                )

        rtt  = float(health.get("response_time_ms", 9_999.0))
        load = float(health.get("load", 50.0))

        return (
            self.geo_weight  * geo_km +
            self.rtt_weight  * rtt   +
            self.load_weight * load
        )
