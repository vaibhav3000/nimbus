from experiments.robot_edge import t2_background_traffic as t2


def test_t2_pedestrian_lane_is_free_and_separated():
    row = t2.preflight_world_for_test()
    assert row["start_xy"] == [4.0, -13.0]
    assert row["end_xy"] == [4.0, 13.0]
    assert row["yaw_rad"] > 1.5
    assert row["minimum_static_clearance_m"] >= .4
    assert row["minimum_robot_centerline_clearance_m"] >= .5
