import time
import math
import threading

from ground_robot_sdk import (
    AStarPlanner,
    AvoidanceConfig,
    CostmapConfig,
    DynamicObstacleAvoider,
    MockAdapter,
    NavigationConfig,
    OccupancyGrid,
    RobotRuntime,
    SafetyConfig,
)
from ground_robot_sdk.models import CommandStatus, Odometry, Pose2D, RangeScan, Twist2D


def test_astar_routes_around_inflated_obstacle():
    grid=OccupancyGrid(30,20,.2,-1.,-2.)
    dynamic=grid.inflated_cells(((2.,0.),),.5)
    path=AStarPlanner(grid).plan((0.,0.),(4.,0.),dynamic)
    assert path is not None
    assert len(path) >= 3
    assert any(abs(y) > .45 for _,y in path[1:-1])
    assert all(not grid.is_occupied(grid.world_to_cell(x,y),dynamic) for x,y in path[1:-1])


def test_astar_refuses_an_occupied_goal_instead_of_appending_a_unsafe_final_segment():
    grid=OccupancyGrid(30,20,.2,-1.,-2.)
    dynamic=grid.inflated_cells(((4.,0.),),.5)
    assert AStarPlanner(grid).plan((0.,0.),(4.,0.),dynamic) is None


def test_corridor_costmap_constrains_goals_to_validated_space():
    grid=OccupancyGrid.from_corridor_graph(
        {"start":(0.,0.),"corner":(2.,0.),"goal":(2.,2.)},
        (("start","corner",2.),("corner","goal",2.)),
        CostmapConfig(resolution=.1,corridor_width=1.,robot_radius=.2,margin=.5),
    )
    planner=AStarPlanner(grid)
    path=planner.plan((0.,0.),(2.,2.))
    assert path and len(path) >= 3
    assert planner.plan((0.,0.),(8.,8.)) is None


def test_dynamic_avoider_slows_and_steers_toward_clear_side():
    avoider=DynamicObstacleAvoider(AvoidanceConfig(safety_distance=.25,caution_distance=.8))
    scan=RangeScan((.35,.4,.5,1.5,2.),.5,.35,time.time(),-.2,.1,.02,4.)
    decision=avoider.evaluate(scan,Pose2D())
    assert not decision.blocked
    assert 0 < decision.speed_scale < 1
    assert decision.steering_bias > 0
    assert decision.obstacle_points


def test_navigation_waits_for_transient_obstacle_then_resumes():
    adapter=MockAdapter()
    runtime=RobotRuntime(
        adapter,
        safety_config=SafetyConfig(True,.25),
        navigation_config=NavigationConfig(
            enable_path_planning=True,enable_dynamic_avoidance=True,
            require_obstacle_feedback=True,obstacle_timeout=.3,
            obstacle_wait_timeout=.3,control_period=.01,position_tolerance=.05,
        ),
    ).start()
    runtime.navigation.configure_costmap(OccupancyGrid(40,20,.1,-1.,-1.))
    try:
        now=time.time(); adapter.set_odometry(Odometry(Pose2D(),timestamp=now))
        adapter.set_range_scan(RangeScan((.2,.2,.2),.2,.2,now,-.1,.1,.02,4.))
        task=runtime.commands.goto(1.,0.,speed=.2)
        time.sleep(.05)
        assert runtime.commands.status(task.command_id).status == CommandStatus.RUNNING
        assert adapter.last_command == Twist2D()
        now=time.time(); adapter.set_range_scan(RangeScan((2.,2.,2.),2.,2.,now,-.1,.1,.02,4.))
        time.sleep(.06)
        assert adapter.last_command.linear > 0
        now=time.time(); adapter.set_range_scan(RangeScan((2.,2.,2.),2.,2.,now,-.1,.1,.02,4.)); adapter.set_odometry(Odometry(Pose2D(1.,0.,0.),timestamp=now))
        time.sleep(.08)
        result=runtime.commands.status(task.command_id)
        assert result.status == CommandStatus.COMPLETED, (result.reason,result.details)
        assert result.details["planner"] == "astar_costmap"
        assert result.details["avoidance_events"] > 0
    finally:
        runtime.close()


def test_navigation_returns_blocked_for_persistent_dynamic_obstacle():
    adapter=MockAdapter()
    runtime=RobotRuntime(
        adapter,
        safety_config=SafetyConfig(True,.25),
        navigation_config=NavigationConfig(
            enable_path_planning=True,enable_dynamic_avoidance=True,
            require_obstacle_feedback=True,obstacle_timeout=.3,
            obstacle_wait_timeout=.08,control_period=.01,
        ),
    ).start()
    runtime.navigation.configure_costmap(OccupancyGrid(40,20,.1,-1.,-1.))
    try:
        now=time.time(); adapter.set_odometry(Odometry(Pose2D(),timestamp=now)); adapter.set_range_scan(RangeScan((.15,.15,.15),.15,.15,now,-.1,.1,.02,4.))
        task=runtime.commands.goto(1.,0.,speed=.2)
        time.sleep(.15)
        result=runtime.commands.status(task.command_id)
        assert result.status == CommandStatus.BLOCKED
        assert result.details["arrival_verified"] is False
        assert adapter.last_command == Twist2D()
    finally:
        runtime.close()


def test_navigation_stops_when_a_peer_robot_occupies_the_goal():
    adapter=MockAdapter()
    runtime=RobotRuntime(
        adapter,
        safety_config=SafetyConfig(True,.25),
        navigation_config=NavigationConfig(
            enable_path_planning=True,enable_dynamic_avoidance=True,
            require_obstacle_feedback=True,obstacle_timeout=.3,
            obstacle_wait_timeout=.05,control_period=.01,
        ),
    ).start()
    runtime.navigation.configure_costmap(
        OccupancyGrid(40,20,.1,-1.,-1.),
        obstacle_provider=lambda: ((1.,0.),),
    )
    try:
        now=time.time()
        adapter.set_odometry(Odometry(Pose2D(),timestamp=now))
        adapter.set_range_scan(RangeScan((2.,2.,2.),2.,2.,now,-.1,.1,.02,4.))
        task=runtime.commands.goto(1.,0.,speed=.2)
        result=runtime.commands.status(task.command_id)
        assert result.status == CommandStatus.NO_PATH
        assert adapter.last_command == Twist2D()
    finally:
        runtime.close()


def test_navigation_rejects_goal_outside_costmap_without_motion():
    adapter=MockAdapter(); runtime=RobotRuntime(adapter,navigation_config=NavigationConfig(enable_path_planning=True)).start()
    runtime.navigation.configure_costmap(OccupancyGrid(10,10,.1,0.,0.))
    try:
        adapter.set_odometry(Odometry(Pose2D()))
        result=runtime.commands.goto(5.,5.)
        assert result.status == CommandStatus.INVALID_DESTINATION
        assert adapter.last_command == Twist2D()
    finally:
        runtime.close()


def test_navigation_uses_a_map_checked_forward_uturn_for_a_goal_behind_it():
    adapter=MockAdapter()
    runtime=RobotRuntime(
        adapter,
        safety_config=SafetyConfig(True,.25),
        navigation_config=NavigationConfig(
            enable_path_planning=True,enable_dynamic_avoidance=True,
            require_obstacle_feedback=True,obstacle_timeout=.3,
            control_period=.01,uturn_speed=.12,
        ),
    ).start()
    runtime.navigation.configure_costmap(OccupancyGrid(60,60,.1,-3.,-3.))
    try:
        now=time.time()
        adapter.set_odometry(Odometry(Pose2D(),timestamp=now))
        adapter.set_range_scan(RangeScan((2.,2.,2.),2.,2.,now,-.1,.1,.02,4.))
        task=runtime.commands.goto(-1.,0.,speed=.2)
        time.sleep(.06)
        result=runtime.commands.status(task.command_id)
        assert result.status == CommandStatus.RUNNING
        assert result.details["map_checked_uturns"] == 1
        assert adapter.last_command.linear > 0
        assert abs(adapter.last_command.angular) > 0
    finally:
        runtime.close()


def test_navigation_prefers_a_forward_first_astar_detour_before_a_uturn():
    adapter=MockAdapter()
    runtime=RobotRuntime(adapter,navigation_config=NavigationConfig(enable_path_planning=True)).start()
    runtime.navigation.configure_costmap(OccupancyGrid(100,100,.1,-5.,-5.))
    try:
        pose=Pose2D()
        assert runtime.navigation._replan_forward_first(pose,Pose2D(-2.,0.),time.monotonic())
        first=runtime.navigation.path[1]
        assert first[0] >= -.05
    finally:
        runtime.close()


def test_closed_loop_replans_and_drives_around_static_obstacle():
    adapter=MockAdapter()
    runtime=RobotRuntime(
        adapter,
        safety_config=SafetyConfig(True,.25),
        navigation_config=NavigationConfig(
            enable_path_planning=True,enable_dynamic_avoidance=True,
            require_obstacle_feedback=True,obstacle_timeout=.3,
            obstacle_wait_timeout=.2,control_period=.01,position_tolerance=.10,
            avoidance_distance=1.5,dynamic_obstacle_ttl=8.,path_lookahead=.35,
        ),
    ).start()
    runtime.navigation.configure_costmap(OccupancyGrid(60,30,.1,-1.,-1.5))
    stop=threading.Event(); trace=[]

    def simulate():
        x=y=yaw=0.; dt=.01; obstacle=(2.,0.,.30)
        angles=(-.2,-.1,0.,.1,.2)
        while not stop.is_set():
            command=adapter.last_command; speedup=4.
            yaw=math.atan2(math.sin(yaw+command.angular*speedup*dt),math.cos(yaw+command.angular*speedup*dt))
            x+=command.linear*speedup*math.cos(yaw)*dt; y+=command.linear*speedup*math.sin(yaw)*dt
            ranges=[]
            for angle in angles:
                bearing=yaw+angle; dx=obstacle[0]-x; dy=obstacle[1]-y
                projection=dx*math.cos(bearing)+dy*math.sin(bearing)
                perpendicular_sq=max(0.,dx*dx+dy*dy-projection*projection)
                if projection > 0 and perpendicular_sq <= obstacle[2]**2:
                    ranges.append(max(.02,projection-math.sqrt(obstacle[2]**2-perpendicular_sq)))
                else: ranges.append(4.)
            now=time.time(); trace.append((x,y,yaw,command.linear,command.angular))
            adapter.set_odometry(Odometry(Pose2D(x,y,yaw),Twist2D(command.linear,command.angular),now))
            adapter.set_range_scan(RangeScan(tuple(ranges),min(ranges[1:4]),min(ranges),now,-.2,.1,.02,4.))
            time.sleep(dt)

    worker=threading.Thread(target=simulate,daemon=True); worker.start()
    try:
        time.sleep(.03); task=runtime.commands.goto(4.,0.,speed=.5)
        deadline=time.time()+6
        while runtime.commands.status(task.command_id).status == CommandStatus.RUNNING and time.time() < deadline: time.sleep(.02)
        result=runtime.commands.status(task.command_id)
        assert result.status == CommandStatus.COMPLETED, (result.reason,result.details,max(abs(row[1]) for row in trace))
        assert result.details["replans"] >= 1
        assert result.details["avoidance_events"] >= 1
        assert max(abs(row[1]) for row in trace) > .31
    finally:
        stop.set(); worker.join(1); runtime.close()
