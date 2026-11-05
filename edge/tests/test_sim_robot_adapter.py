"""
Unit tests for SimRobot (ground_robot_sdk simulation adapter integration)
"""

import time
import unittest

from edge.mock.sim_robot import (
    DEFAULT_ROBOT_DEPLOYMENT,
    SimRobot,
    canonical_topology_location,
)
from edge.supervisor.supervisor import EdgeSupervisor


class TestSimRobotAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.robot = SimRobot(
            robot_id="R1",
            initial_pos=[4.5, 8.0, 0.0],
            current_zone="east_waiting_approach",
            use_gazebo=False,
        )

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls.robot, "close"):
            cls.robot.close()

    def test_01_initial_state(self):
        self.assertEqual(self.robot.robot_id, "R1")
        self.assertEqual(self.robot.current_zone, "east_waiting_approach")
        self.assertAlmostEqual(self.robot.pose[0], 4.5)
        self.assertAlmostEqual(self.robot.pose[1], 8.0)

    def test_02_goto_navigation(self):
        cmd = {
            "cmd_id": "cmd-test-goto",
            "action": "go_to",
            "params": {"to": "nurse_station_approach", "speed": 0.4},
        }
        res = self.robot.execute_command(cmd)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["zone"], "nurse_station_approach")
        self.assertLessEqual(((self.robot.pose[0]-0.0)**2+(self.robot.pose[1]-5.0)**2)**0.5,0.25)

    def test_03_photo_capture(self):
        cmd = {
            "cmd_id": "cmd-test-photo",
            "action": "photo",
            "params": {},
        }
        res = self.robot.execute_command(cmd)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertTrue(res["has_photo"])
        self.assertGreater(res["photo_size"], 0)
        self.assertIsNotNone(self.robot.last_photo_bytes)

    def test_04_beep_and_wait(self):
        res_beep = self.robot.execute_command({"cmd_id": "c-beep", "action": "beep", "params": {}})
        self.assertEqual(res_beep["status"], "COMPLETED")

        res_wait = self.robot.execute_command({"cmd_id": "c-wait", "action": "wait", "params": {"seconds": 0.1}})
        self.assertEqual(res_wait["status"], "COMPLETED")

    def test_05_report(self):
        res = self.robot.execute_command({"cmd_id": "c-rep", "action": "report", "params": {}})
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["zone"], "nurse_station_approach")
        self.assertIn("report", res)

    def test_06_logical_location_ids_resolve_to_physical_topology(self):
        self.assertEqual(canonical_topology_location("2B-12"), "east_waiting_approach")
        self.assertEqual(canonical_topology_location("2b-12"), "east_waiting_approach")
        self.assertEqual(canonical_topology_location("bed 2B-12"), "east_waiting_approach")
        self.assertEqual(canonical_topology_location("2C"), "west_waiting_approach")
        self.assertEqual(canonical_topology_location("2c"), "west_waiting_approach")

    def test_07_supervisor_spawn_poses_match_simulator_deployment(self):
        for robot_id in ("R1", "R2", "R3", "R4", "R5", "R6"):
            self.assertEqual(
                EdgeSupervisor.ROBOT_SPAWN_POSES[robot_id],
                DEFAULT_ROBOT_DEPLOYMENT[robot_id]["spawn_pose"],
            )
        self.assertAlmostEqual(EdgeSupervisor.ROBOT_SPAWN_POSES["R3"][2], -1.5707963)
        self.assertAlmostEqual(EdgeSupervisor.ROBOT_SPAWN_POSES["R4"][2], -1.5707963)

    def test_08_east_and_west_spawn_headings_transform_world_goals(self):
        east = SimRobot(robot_id="R3", use_gazebo=False)
        west = SimRobot(robot_id="R4", use_gazebo=False)
        try:
            # Both robots face south at startup. Their next same-wing waypoint
            # is therefore five metres straight ahead in startup-local odom.
            east_goal = east._world_to_navigation((4.5, 3.0))
            west_goal = west._world_to_navigation((-4.5, 3.0))
            self.assertAlmostEqual(east_goal[0], 5.0, places=5)
            self.assertAlmostEqual(east_goal[1], 0.0, places=5)
            self.assertAlmostEqual(west_goal[0], 5.0, places=5)
            self.assertAlmostEqual(west_goal[1], 0.0, places=5)
        finally:
            east.close()
            west.close()

    def test_09_explicit_non_world_target_is_rejected(self):
        result = self.robot.execute_command({
            "cmd_id": "cmd-wrong-frame",
            "action": "go_to",
            "params": {
                "target": [1.0, 0.0],
                "target_frame": "base_link",
            },
        })
        self.assertEqual(result["status"], "INVALID_DESTINATION")
        self.assertIn("gazebo/world", result["message"])


if __name__ == "__main__":
    unittest.main()
