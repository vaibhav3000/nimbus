"""
Nimbus Safe Parking Engine (FR-6: Lease Expiry & Safe Parking Enforcement)
Guarantees that upon lease expiry or emergency interrupt:
1. Current robot errand is halted immediately (model-free reflex stop).
2. The nearest verified safe parking recess is selected.
3. Prohibited parking zones (doorways, gates, fire exits, active corridors, elevator vestibules) are strictly avoided.
4. Forbidden zones from the lease are strictly excluded.
"""

import math
from typing import Any, Dict, List, Optional, Tuple, Union

from edge.supervisor.geofence import DEFAULT_ZONE_REGISTRY, SpatialGeofenceValidator


# Invariant 4 (FR-6): Robots must NEVER be left standing in doorways, corridors, gates, or elevator vestibules.
PROHIBITED_PARKING_ZONE_TYPES = {
    "doorway",
    "gate",
    "fire_exit",
    "elevator",
    "lift",
    "stairwell",
    "corridor",
    "hallway",
    "transit",
    "isolation"
}

# Verified static safe parking bays (wall alcoves, dedicated recesses, docks)
DEFAULT_SAFE_PARKING_SPOTS: Dict[str, Dict[str, Any]] = {
    "safe-parking-bay-1": {
        "zone_name": "safe-parking-bay-1",
        "coords": [-1.0, 4.0],
        "zone_type": "parking_bay",
        "wing": "east-wing",
        "capacity": 2,
        "description": "Supply Depot Equipment Recess Bay 1"
    },
    "safe-parking-bay-2": {
        "zone_name": "safe-parking-bay-2",
        "coords": [11.0, -1.8],
        "zone_type": "parking_bay",
        "wing": "east-wing",
        "capacity": 2,
        "description": "Corridor 2B Wall Alcove Recess Bay 2"
    },
    "safe-parking-bay-east": {
        "zone_name": "safe-parking-bay-east",
        "coords": [20.0, 3.0],
        "zone_type": "parking_bay",
        "wing": "east-wing",
        "capacity": 2,
        "description": "East Wing Sub-station Parking Recess"
    }
}


class SafeParkingEngine:
    """
    Manages safe parking spot resolution and execution on lease expiration (FR-6).
    """

    def __init__(
        self,
        safe_spots: Optional[Dict[str, Dict[str, Any]]] = None,
        zone_registry: Optional[Dict[str, Dict[str, Any]]] = None
    ):
        self.safe_spots = safe_spots or DEFAULT_SAFE_PARKING_SPOTS
        self.zone_registry = zone_registry or DEFAULT_ZONE_REGISTRY
        self.geofence = SpatialGeofenceValidator(self.zone_registry)

    def is_safe_for_parking(self, zone_name: str) -> bool:
        """
        Validates that a zone is an approved safe parking area and does not violate
        Invariant 4 (doorways, gates, elevator vestibules, or active transit corridors).
        """
        # 1. Check known prohibited zone types
        zone_info = self.zone_registry.get(zone_name)
        if zone_info:
            ztype = zone_info.get("zone_type", "").lower()
            if ztype in PROHIBITED_PARKING_ZONE_TYPES:
                return False

        # Name heuristic check
        lower_name = zone_name.lower()
        for prohibited in ("doorway", "gate", "fire-exit", "elevator", "lift", "corridor", "hall", "stair"):
            if prohibited in lower_name:
                return False

        # 2. Must be in registered safe parking spots or have safe zone type
        if zone_name in self.safe_spots:
            return True

        if zone_info:
            ztype = zone_info.get("zone_type", "").lower()
            if ztype in ("parking_bay", "recess", "alcove"):
                return True

        return False

    def _is_in_forbidden_zone(
        self,
        spot_name: str,
        coords: List[float],
        forbidden_zones: Optional[List[str]]
    ) -> bool:
        """
        Checks if spot name or its physical coordinates fall inside any forbidden zone (FR-6, C6).
        Resolves spatial containment against zone registry bounds (e.g. safe-parking-bay-2
        inside prohibited corridor-2B-service).
        """
        if not forbidden_zones:
            return False
        forbidden_set = set(forbidden_zones)
        if spot_name in forbidden_set:
            return True
        x, y = coords[0], coords[1]
        for fz_name in forbidden_set:
            fz_info = self.zone_registry.get(fz_name)
            if fz_info and "bounds" in fz_info:
                xmin, ymin, xmax, ymax = fz_info["bounds"]
                if xmin <= x <= xmax and ymin <= y <= ymax:
                    return True
        return False

    def find_nearest_safe_parking_spot(
        self,
        current_pose: List[float],
        allowed_zones: Optional[List[str]] = None,
        forbidden_zones: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Calculates Euclidean distance from current [x, y] pose to all registered
        safe parking spots, strictly filtering out any spots that violate forbidden_zones
        either by name or coordinate bounds containment (FR-6, C6 / Invariant 4).
        
        Returns the closest valid safe parking spot record, or None if all spots
        are excluded or violate forbidden zones.
        """
        cx, cy = current_pose[0], current_pose[1]
        allowed_set = set(allowed_zones or [])

        candidates: List[Tuple[float, Dict[str, Any]]] = []

        for name, spot in self.safe_spots.items():
            # 1. Enforce static safe parking verification (not doorway, lift, corridor, etc.)
            if not self.is_safe_for_parking(name):
                continue

            sx, sy = spot["coords"][0], spot["coords"][1]

            # 2. Exclude spots that violate forbidden zones by name or coordinate containment
            if self._is_in_forbidden_zone(name, [sx, sy], forbidden_zones):
                continue

            dist = math.hypot(cx - sx, cy - sy)

            # Weight candidates slightly if within allowed_zones
            is_in_allowed = (name in allowed_set) if allowed_set else True
            score = dist if is_in_allowed else (dist + 50.0)

            candidates.append((score, spot))

        if not candidates:
            # Strictly do not return a spot that is in forbidden_zones (FR-6)
            return None

        # Sort by distance / score ascending
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
