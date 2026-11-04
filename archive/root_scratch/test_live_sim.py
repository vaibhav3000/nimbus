import os, sys, time
sys.path.insert(0, '/root/nimbus')
sys.path.insert(0, '/root/nimbus/simulation/hospital_world')
os.environ['IGN_PARTITION'] = 'custom_hospital_world'
import rclpy
print('rclpy loaded successfully')
from edge.mock.sim_robot import SimRobot
print('Instantiating SimRobot R1...')
r = SimRobot('R1', use_gazebo=True)
print('SimRobot initialized. Is gazebo:', r._is_gazebo)
time.sleep(3)
state = r.runtime.state()
print('Odom fresh:', state.health.odometry.fresh)
print('Range fresh:', state.health.range.fresh)
r.close()
print('SimRobot closed successfully.')
