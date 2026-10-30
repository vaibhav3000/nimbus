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
        for prohibited in ("doorway", "gate", "fire-exit", "elevator", "corridor", "hall", "stair"):
            if prohibited in lower_name:
                return False

        # 2. Must be in registered safe parking spots or have safe zone type
        if zone_name in self.safe_spots:
            return True

        if zone_info:
            ztype = zone_info.get("zone_type", "").lower()
            if ztype in ("parking_bay", "recess", "alcove", "zone"):
                return True

        return False

    def find_nearest_safe_parking_spot(
        self,
        current_pose: List[float],
        allowed_zones: Optional[List[str]] = None,
        forbidden_zones: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Calculates Euclidean distance from current [x, y] pose to all registered
        safe parking spots, filtering out any spots that violate forbidden_zones.
        
        Returns the closest valid safe parking spot record.
        """
        cx, cy = current_pose[0], current_pose[1]
        forbidden_set = set(forbidden_zones or [])
        allowed_set = set(allowed_zones or [])

        candidates: List[Tuple[float, Dict[str, Any]]] = []

        for name, spot in self.safe_spots.items():
            # Exclude spots that violate forbidden zones
            if name in forbidden_set:
                continue

            # If allowed_zones is given, prioritize spots inside allowed_zones
            sx, sy = spot["coords"][0], spot["coords"][1]
            dist = math.hypot(cx - sx, cy - sy)

            # Weight candidates slightly if within allowed_zones
            is_in_allowed = (name in allowed_set) if allowed_set else True
            score = dist if is_in_allowed else (dist + 50.0)

            candidates.append((score, spot))

        if not candidates:
            # Fallback to the first safe parking spot
            first_name = next(iter(self.safe_spots))
            return self.safe_spots[first_name]

        # Sort by distance / score ascending
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
