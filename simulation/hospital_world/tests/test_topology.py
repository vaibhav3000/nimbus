import json
from pathlib import Path
import unittest

from topology.resolver import UnknownDestination, resolve_destination, resolve_for_robot, shortest_route, world_to_sdk_startup


ROOT = Path(__file__).resolve().parents[1]


class TopologyTest(unittest.TestCase):
    def setUp(self):
        self.topology = json.loads((ROOT / "topology/hospital_topology.json").read_text())
        self.waypoints = json.loads((ROOT / "topology/topology_waypoints.json").read_text())["waypoints"]
        self.scenarios = json.loads((ROOT / "topology/scenario_locations.json").read_text())
        self.deployment = json.loads((ROOT / "config/robot_deployment.json").read_text())

    def test_groups_are_an_exact_node_partition_and_match_wings(self):
        members = []
        for group, nodes in self.topology["groups"].items():
            members.extend(nodes)
            for node in nodes:
                self.assertEqual(self.topology["nodes"][node]["wing"], group)
        self.assertEqual(set(members), set(self.topology["nodes"]))
        self.assertEqual(len(members), len(set(members)))

    def test_exclusive_gates_follow_sop_gate_03(self):
        expected = {"lift_lobby", "west_north_gate", "east_north_gate"}
        actual = {node for node, values in self.topology["nodes"].items() if values.get("exclusive")}
        self.assertEqual(actual, expected)
        self.assertTrue(all(self.topology["nodes"][node]["kind"] == "gate" for node in actual))
        self.assertEqual(len({self.topology["nodes"][node]["reservation_id"] for node in actual}), 3)

    def test_prd_aliases_resolve_to_documented_physical_waypoints(self):
        expected = {
            "2B": "east_waiting_approach", "2B-12": "east_waiting_approach",
            "2C": "west_waiting_approach", "ward-3": "south_room_approach",
            "equipment-store": "south_equipment_approach",
        }
        for alias, node in expected.items():
            result = resolve_destination(alias)
            self.assertEqual(result["physical_node"], node)
            self.assertEqual(result["waypoint"], self.waypoints[node])
        with self.assertRaises(UnknownDestination):
            resolve_destination("ward-999")

    def test_robot_deployment_associations_and_world_to_sdk_transform(self):
        first = self.deployment["robots"]["robot_1"]
        second = self.deployment["robots"]["robot_2"]
        self.assertEqual(first["start_node"], "north_hall")
        self.assertEqual(first["spawn_pose"][:2], self.waypoints["north_hall"])
        self.assertIsNone(second["start_node"])
        self.assertEqual(second["start_edge"], ["north_hall", "nurse_station_approach"])
        self.assertEqual(world_to_sdk_startup([0, 5], [0, 12, 0]), (0.0, -7.0))
        x, y = world_to_sdk_startup([1, 0], [0, 0, 1.5707963267948966])
        self.assertAlmostEqual(x, 0.0, places=8)
        self.assertAlmostEqual(y, -1.0, places=8)
        goal = resolve_for_robot("2B", "robot_1")
        self.assertEqual(goal["sdk_startup_waypoint"], [4.5, -4.0])

    def test_logical_route_uses_bidirectional_weighted_graph(self):
        route = shortest_route("north_hall", "2B")
        self.assertEqual(route["nodes"], ["north_hall", "east_north_gate", "east_waiting_approach"])
        self.assertAlmostEqual(route["cost_m"], 10.92)


if __name__ == "__main__":
    unittest.main()
