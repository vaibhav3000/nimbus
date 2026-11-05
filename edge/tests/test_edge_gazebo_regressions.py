"""Deterministic regressions for the edge/Gazebo execution contract.

These use injected SDK sensor feedback.  They do not claim that Gazebo ran.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time

from edge.mock.mock_robot import MockRobot
from edge.mock.sim_robot import SimRobot
from edge.supervisor.supervisor import EdgeSupervisor
from ground_robot_sdk import MockAdapter, NavigationConfig, RobotRuntime, SafetyConfig
from ground_robot_sdk.models import GroundTruthPose, Odometry, Pose2D, RangeScan, Twist2D


def _command(target=(1.0,12.0),cmd_id="move"):
    return {"cmd_id":cmd_id,"action":"go_to","params":{"target":list(target),"speed":0.4}}


def test_continuous_pose_synchronization_uses_measured_feedback():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.15)
    result={}
    try:
        worker=threading.Thread(target=lambda:result.setdefault("value",robot.execute_command(_command((1.2,12.0)))))
        worker.start(); time.sleep(.12)
        during=list(robot.pose)
        worker.join(5)
        assert 0.0 < during[0] < 1.2
        assert result["value"]["status"] == "COMPLETED"
        assert result["value"]["pose_source"].startswith("odometry/")
        assert abs(robot.pose[0]-1.2) <= .25
        assert robot.pose[0] != 1.2
    finally: robot.close()


def test_success_requires_fresh_sensors_and_verified_arrival():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.15)
    try:
        result=robot.execute_command(_command((.6,12.0)))
        assert result["status"] == "COMPLETED"
        assert result["arrival_verified"] is True
        assert result["verification"]["verification_source"] == "odometry"
        assert result["verification"]["planner"] == "astar_costmap"
        assert result["verification"]["planned_path"]
        assert result["execution_mode"] == "sdk_mock"
        assert result["physical_verification"] is False
    finally: robot.close()


def test_obstacle_stops_navigation_and_returns_blocked():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.15)
    try:
        robot.inject_obstacle("north_hall",obstacle_type="persistent")
        result=robot.execute_command(_command((1.0,12.0)))
        assert result["status"] == "BLOCKED"
        assert robot.runtime.adapter.last_command == Twist2D()
    finally: robot.close()


def test_range_disconnect_during_motion_fails_closed():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.08)
    result={}
    try:
        worker=threading.Thread(target=lambda:result.setdefault("value",robot.execute_command(_command((1.5,12.67)))))
        worker.start(); time.sleep(.12); robot.set_mock_sensor_enabled(range_scan=False)
        worker.join(5)
        assert result["value"]["status"] == "SENSOR_UNAVAILABLE"
        assert result["value"]["arrival_verified"] is False
        assert robot.runtime.adapter.last_command == Twist2D()
    finally: robot.close()


def test_odometry_disconnect_during_motion_prevents_completion():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.08)
    result={}
    try:
        worker=threading.Thread(target=lambda:result.setdefault("value",robot.execute_command(_command((1.5,12.67)))))
        worker.start(); time.sleep(.12); robot.set_mock_sensor_enabled(odometry=False)
        worker.join(5)
        assert result["value"]["status"] == "LOCALIZATION_UNAVAILABLE"
        assert result["value"]["arrival_verified"] is False
        assert robot.runtime.adapter.last_command == Twist2D()
    finally: robot.close()


def test_unknown_destination_is_rejected_without_motion():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.15)
    try:
        before=list(robot.pose)
        result=robot.execute_command({"cmd_id":"bad","action":"go_to","params":{"to":"nowhere-in-hospital"}})
        assert result["status"] == "INVALID_DESTINATION"
        assert robot.pose == before
        assert robot.runtime.adapter.last_command == Twist2D()
    finally: robot.close()


def test_coordinate_outside_corridor_costmap_is_rejected():
    robot=SimRobot("R1",use_gazebo=False,sensor_timeout=.15)
    try:
        result=robot.execute_command(_command((10.,12.)))
        assert result["status"] == "INVALID_DESTINATION"
        assert result["arrival_verified"] is False
        assert robot.runtime.adapter.last_command == Twist2D()
    finally: robot.close()


def test_ground_truth_mismatch_withholds_completion():
    adapter=MockAdapter()
    runtime=RobotRuntime(adapter,safety_config=SafetyConfig(True,.25),navigation_config=NavigationConfig(
        position_tolerance=.05,control_period=.01,odometry_timeout=.2,require_obstacle_feedback=True,
        obstacle_timeout=.2,validation_source="ground_truth",ground_truth_tolerance=.05,max_localization_discrepancy=.10,
    )).start()
    try:
        now=time.time(); adapter.set_odometry(Odometry(Pose2D(),timestamp=now)); adapter.set_range_scan(RangeScan((2.,),2.,2.,now)); adapter.set_ground_truth(GroundTruthPose(Pose2D(10.,5.,0.),now))
        task=runtime.commands.goto(1.,0.,speed=.2)
        adapter.set_range_scan(RangeScan((2.,),2.,2.,time.time()))
        adapter.set_ground_truth(GroundTruthPose(Pose2D(10.,5.,0.),time.time()))
        adapter.set_odometry(Odometry(Pose2D(1.,0.,0.),timestamp=time.time()))
        deadline=time.time()+1
        while runtime.commands.status(task.command_id).status.value == "running" and time.time() < deadline: time.sleep(.01)
        result=runtime.commands.status(task.command_id)
        assert result.status.value == "ground_truth_mismatch"
        assert result.details["arrival_verified"] is False
        assert adapter.last_command == Twist2D()
    finally: runtime.close()


class _FailureRobot(MockRobot):
    def execute_command(self,cmd):
        if cmd["action"] == "go_to":
            return {"robot_id":self.robot_id,"cmd_id":cmd["cmd_id"],"status":"SENSOR_UNAVAILABLE","pose":list(self.pose),"zone":self.current_zone,"message":"range stream disconnected","arrival_verified":False}
        return super().execute_command(cmd)


def _lease():
    now=datetime.now(timezone.utc)
    return {"lease_id":"lease-strict-result","version":1,"priority":"routine","deadline_ts":(now+timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),"expiry_ts":(now+timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),"allowed_zones":["north_hall"],"forbidden_zones":[],"speed_limit":.5,"assigned_robots":["R1"],"task_sequence":[{"cmd":"go_to","robot":"R1","params":{"to":"north_hall"}}],"cluster_id":"east-wing","when_to_escalate":["route_blocked_no_alternative"]}


def test_supervisor_preserves_detailed_failure_and_never_emits_completion(tmp_path):
    supervisor=EdgeSupervisor(cluster_id="east-wing",db_path=str(tmp_path/"strict.db"),slm_mode="mock")
    supervisor.robots["R1"]=_FailureRobot("R1")
    result=supervisor.process_lease(_lease(),verbose=False)
    assert result["status"] == "FAILED"
    assert result["execution_status"] == "SENSOR_UNAVAILABLE"
    assert supervisor.store.get_lease("lease-strict-result")["status"] == "FAILED"
    assert not any(item["message_type"] == "OUTPUT_READY" for item in supervisor.store.get_pending_outbox())


def test_explicit_mock_mode_never_claims_physical_verification():
    robot=MockRobot("R1")
    result=robot.execute_command({"cmd_id":"mock","action":"go_to","params":{"to":"north_hall"}})
    assert result["status"] == "COMPLETED"
    assert result["physical_verification"] is False
    assert result["execution_mode"] == "mock"
