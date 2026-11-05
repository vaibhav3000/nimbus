import time
import unittest
from ground_robot_sdk import MockAdapter, NavigationConfig, RobotRuntime, SafetyConfig
from ground_robot_sdk.namespaces import RobotIdentity
from ground_robot_sdk.gazebo_adapter import GazeboAdapter
from ground_robot_sdk.models import CameraFrame, Odometry, Pose2D, RangeScan, Twist2D

class SDKTest(unittest.TestCase):
    def setUp(self):
        self.adapter=MockAdapter(); self.runtime=RobotRuntime(self.adapter); self.runtime.start()
    def tearDown(self): self.runtime.close()
    def test_mock_sensor_state_and_capabilities(self):
        self.adapter.set_odometry(Odometry(Pose2D(1,2,.3),Twist2D(.2,.1),1.))
        self.adapter.set_range_scan(RangeScan((.5,.4,.3),.4,.3,1.))
        self.adapter.set_camera_frame(CameraFrame(1.,10,10,"jpeg",True,b"jpeg"))
        state=self.runtime.state(); self.assertEqual(state.odometry.pose.x,1); self.assertEqual(state.range.front,.4); self.assertEqual(state.health.status,"healthy")
        self.assertEqual(self.runtime.capabilities().to_dict()["motion"]["wheelbase"],.325)

    def test_high_level_photo_beep_and_report(self):
        self.adapter.set_camera_frame(CameraFrame(4.,10,8,"jpeg",True,b"jpeg"))
        photo=self.runtime.commands.photo()
        self.assertTrue(photo.available)
        self.assertEqual(photo.data,b"jpeg")
        beep=self.runtime.commands.beep()
        self.assertEqual(beep.command_type,"beep")
        self.assertEqual(beep.status.value,"completed")
        report=self.runtime.commands.report()
        self.assertEqual(report["capabilities"]["robot_type"],"mit_racecar")
        self.assertTrue(report["state"]["camera"]["available"])
    def test_drive_clamps_and_completes(self):
        result=self.runtime.motion.drive(99,99,duration=.03); self.assertEqual(result.status.value,"running")
        self.assertAlmostEqual(self.adapter.last_command.linear,1.); self.assertGreater(self.adapter.last_command.angular,0)
        time.sleep(.08); self.assertEqual(self.adapter.last_command,Twist2D()); self.assertEqual(self.runtime.controller.get_command(result.command_id).status.value,"completed")
    def test_arbiter_and_emergency_stop(self):
        keyboard=self.runtime.motion.set_velocity(.1,0,duration=.3,source="keyboard")
        remote=self.runtime.motion.set_velocity(.1,0,duration=.3,source="remote")
        self.assertEqual(remote.status.value,"running")
        self.assertEqual(self.runtime.controller.get_command(keyboard.command_id).status.value,"cancelled")
        rejected=self.runtime.motion.set_velocity(.1,0,duration=.3,source="keyboard")
        self.assertEqual(rejected.status.value,"rejected")
        self.runtime.motion.stop(emergency=True); self.assertTrue(self.runtime.safety.emergency)
        blocked=self.runtime.motion.set_velocity(.1,0); self.assertEqual(blocked.status.value,"rejected")
    def test_obstacle_guard_rejects_forward_and_allows_reverse(self):
        runtime=RobotRuntime(MockAdapter(),safety_config=SafetyConfig(True,.25)); runtime.start()
        try:
            runtime.adapter.set_range_scan(RangeScan((.1,),.1,.1,1.))
            self.assertEqual(runtime.motion.drive(.1,0).status.value,"rejected")
            self.assertEqual(runtime.motion.drive(-.1,0).status.value,"running")
        finally: runtime.close()

    def test_reflex_stops_active_command_when_obstacle_appears(self):
        runtime=RobotRuntime(MockAdapter(),safety_config=SafetyConfig(True,.25,reflex_period=.01)).start()
        try:
            runtime.adapter.set_range_scan(RangeScan((1.,),1.,1.,time.time()))
            self.assertEqual(runtime.motion.drive(.2,0,duration=1).status.value,"running")
            runtime.adapter.set_range_scan(RangeScan((.1,),.1,.1,time.time()))
            time.sleep(.06)
            self.assertEqual(runtime.adapter.last_command,Twist2D())
            self.assertIn("obstacle",runtime.safety.reflex_stop_reason)
        finally: runtime.close()

    def test_reflex_stops_active_command_when_range_contact_times_out(self):
        runtime=RobotRuntime(MockAdapter(),safety_config=SafetyConfig(True,.25,lost_contact_timeout=.03,reflex_period=.01)).start()
        try:
            runtime.adapter.set_range_scan(RangeScan((1.,),1.,1.,time.time()))
            self.assertEqual(runtime.motion.drive(.2,0,duration=1).status.value,"running")
            time.sleep(.08)
            self.assertEqual(runtime.adapter.last_command,Twist2D())
            self.assertIn("contact lost",runtime.safety.reflex_stop_reason)
        finally: runtime.close()

    def test_topic_construction_preserves_global_default(self):
        legacy=RobotIdentity().topics
        self.assertEqual(legacy.cmd_vel,"/cmd_vel")
        self.assertEqual(legacy.range_scan,"/picarx/ultrasonic/scan")
        robot=RobotIdentity("robot_1","robot_1").topics
        self.assertEqual(robot.cmd_vel,"/robot_1/cmd_vel")
        self.assertEqual(robot.odom,"/robot_1/odom")
        self.assertEqual(robot.range_scan,"/robot_1/ultrasonic/scan")
        self.assertEqual(robot.camera_image,"/robot_1/camera/image")
        self.assertEqual(robot.odom_frame,"robot_1/odom")
        self.assertEqual(robot.base_frame,"robot_1/base_link")
        adapter=GazeboAdapter(start_bridges=False,identity=RobotIdentity("robot_2","/robot_2"))
        self.assertEqual(adapter.CMD_VEL,"/robot_2/cmd_vel")
        self.assertEqual(adapter.SCAN,"/robot_2/ultrasonic/scan")

    def test_two_namespaced_runtimes_are_isolated(self):
        first=RobotRuntime(MockAdapter(),robot_name="robot_1",namespace="/robot_1").start()
        second=RobotRuntime(MockAdapter(),robot_name="robot_2",namespace="/robot_2").start()
        try:
            first.adapter.set_range_scan(RangeScan((.3,),.3,.3,1.))
            first.motion.drive(.2,.1,duration=.2)
            self.assertEqual(first.sensors.get_front_distance(),.3)
            self.assertIsNone(second.sensors.get_front_distance())
            self.assertNotEqual(first.topics.cmd_vel,second.topics.cmd_vel)
            self.assertNotEqual(first.adapter.last_command,second.adapter.last_command)
            self.assertEqual(first.capabilities().to_dict()["namespace"],"/robot_1")
            self.assertEqual(second.capabilities().to_dict()["namespace"],"/robot_2")
        finally:
            first.close(); second.close()

    def test_navigation_initial_pose_and_odometry_localisation(self):
        self.assertEqual(self.runtime.navigation.pose,Pose2D())
        self.adapter.set_odometry(Odometry(Pose2D(10,5,1.57079632679)))
        self.assertEqual(self.runtime.navigation.pose,Pose2D())
        self.adapter.set_odometry(Odometry(Pose2D(10,6,1.57079632679)))
        pose=self.runtime.navigation.pose
        self.assertAlmostEqual(pose.x,1.0); self.assertAlmostEqual(pose.y,0.0); self.assertAlmostEqual(pose.yaw,0.0)

    def test_goto_world_and_base_link_target_conversion(self):
        self.adapter.set_odometry(Odometry(Pose2D()))
        world=self.runtime.commands.goto(3,2,"world")
        self.assertEqual((self.runtime.navigation.target.x,self.runtime.navigation.target.y),(3.,2.))
        self.runtime.commands.cancel(); time.sleep(.08)
        self.adapter.set_odometry(Odometry(Pose2D(1,2,1.57079632679)))
        relative=self.runtime.commands.goto(1,0,"base_link")
        target=self.runtime.navigation.target
        self.assertAlmostEqual(target.x,1.0); self.assertAlmostEqual(target.y,3.0)
        self.assertNotEqual(world.command_id,relative.command_id); self.runtime.commands.cancel()

    def test_goto_completes_inside_tolerance_and_can_cancel(self):
        self.adapter.set_odometry(Odometry(Pose2D()))
        complete=self.runtime.commands.goto(.05,0)
        time.sleep(.12)
        self.assertEqual(self.runtime.commands.status(complete.command_id).status.value,"completed")
        self.assertEqual(self.runtime.commands.status().command_id,complete.command_id)
        active=self.runtime.commands.goto(2,0)
        self.assertIs(self.runtime.commands.cancel(),active)
        time.sleep(.12)
        self.assertEqual(self.runtime.commands.status(active.command_id).status.value,"cancelled")

    def test_goto_uses_requested_speed_and_defaults_to_one_metre_per_second(self):
        self.adapter.set_odometry(Odometry(Pose2D()))
        requested=self.runtime.commands.goto(5,0,speed=.35)
        time.sleep(.08)
        self.assertAlmostEqual(self.adapter.last_command.linear,.35)
        self.runtime.commands.cancel(); time.sleep(.08)
        default=self.runtime.commands.goto(5,0)
        time.sleep(.08)
        self.assertAlmostEqual(self.adapter.last_command.linear,1.)
        self.runtime.commands.cancel()

    def test_goto_waits_for_odometry_and_fails_when_it_never_arrives(self):
        runtime=RobotRuntime(
            MockAdapter(),
            navigation_config=NavigationConfig(startup_odometry_timeout=.03,control_period=.01),
        ).start()
        try:
            task=runtime.commands.goto(1,0)
            time.sleep(.08)
            result=runtime.commands.status(task.command_id)
            self.assertEqual(result.status.value,"localization_unavailable")
            self.assertIn("no odometry",result.reason)
            self.assertEqual(runtime.adapter.last_command,Twist2D())
        finally: runtime.close()

    def test_runtime_close_cancels_and_joins_navigation_worker(self):
        runtime=RobotRuntime(MockAdapter()).start()
        runtime.adapter.set_odometry(Odometry(Pose2D()))
        runtime.commands.goto(5,0)
        runtime.close()
        self.assertFalse(runtime.navigation._threads)

    def test_goto_fails_cleanly_when_the_adapter_cannot_publish(self):
        class FailingAdapter(MockAdapter):
            def set_velocity(self, command):
                raise RuntimeError("publisher unavailable")

        runtime=RobotRuntime(FailingAdapter()).start()
        try:
            runtime.adapter.set_odometry(Odometry(Pose2D()))
            task=runtime.commands.goto(1,0)
            time.sleep(.08)
            result=runtime.commands.status(task.command_id)
            self.assertEqual(result.status.value,"failed")
            self.assertIn("publisher unavailable",result.reason)
        finally: runtime.close()

    def test_namespaced_navigation_tasks_are_independent(self):
        first=RobotRuntime(MockAdapter(),robot_name="robot_1",namespace="/robot_1").start()
        second=RobotRuntime(MockAdapter(),robot_name="robot_2",namespace="/robot_2").start()
        try:
            first.adapter.set_odometry(Odometry(Pose2D())); second.adapter.set_odometry(Odometry(Pose2D()))
            one=first.commands.goto(1,0); two=second.commands.goto(0,1)
            self.assertEqual((first.navigation.target.x,first.navigation.target.y),(1.,0.))
            self.assertEqual((second.navigation.target.x,second.navigation.target.y),(0.,1.))
            self.assertNotEqual(one.command_id,two.command_id)
        finally:
            first.close(); second.close()

if __name__=="__main__": unittest.main()
