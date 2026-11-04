"""
Unit tests for SimRobot (ground_robot_sdk simulation adapter integration)
"""

import time
import unittest

from edge.mock.sim_robot import SimRobot


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


if __name__ == "__main__":
    unittest.main()
