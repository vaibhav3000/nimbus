"""ROS 2 / Gazebo Fortress implementation of RobotAdapter."""
from __future__ import annotations
import math, os, signal, subprocess, threading
from .adapter import RobotAdapter
from .models import CameraFrame, GroundTruthPose, Odometry, Pose2D, RangeScan, Twist2D
from .namespaces import RobotIdentity

_RCLPY_LOCK=threading.RLock()
_RCLPY_USERS=0
_RCLPY_INITIALIZED_BY_SDK=False

class GazeboAdapter(RobotAdapter):
    def __init__(self, start_bridges=True, identity: RobotIdentity=RobotIdentity()):
        self.start_bridges=start_bridges; self.identity=identity; self.topics=identity.topics
        self.CMD_VEL=self.topics.cmd_vel; self.ODOM=self.topics.odom; self.SCAN=self.topics.range_scan; self.IMAGE=self.topics.camera_image; self.POSE=self.topics.pose
        self.processes=[]; self.bridge_processes={}; self.node=self.publisher=self.executor=self.thread=None; self.started=False; self._rclpy_registered=False; self._lock=threading.RLock()
        self.middleware_ready=False
    def start(self):
        import rclpy
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry as RosOdometry
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import Image, LaserScan
        from tf2_msgs.msg import TFMessage
        if self.started: return
        if self.start_bridges: self._start_bridges()
        global _RCLPY_USERS, _RCLPY_INITIALIZED_BY_SDK
        with _RCLPY_LOCK:
            if not rclpy.ok():
                rclpy.init()
                _RCLPY_INITIALIZED_BY_SDK=True
            _RCLPY_USERS+=1
            self._rclpy_registered=True
        node_name="ground_robot_sdk" if not self.identity.namespace else f"ground_robot_sdk_{self.identity.robot_name}"
        self.node=Node(node_name); self.publisher=self.node.create_publisher(Twist,self.CMD_VEL,QoSProfile(depth=10)); qos=QoSProfile(depth=5,reliability=ReliabilityPolicy.BEST_EFFORT)
        self.node.create_subscription(RosOdometry,self.ODOM,self._on_odom,qos); self.node.create_subscription(LaserScan,self.SCAN,self._on_scan,qos); self.node.create_subscription(Image,self.IMAGE,self._on_image,qos); self.node.create_subscription(TFMessage,self.POSE,self._on_pose,qos)
        self.executor=MultiThreadedExecutor(num_threads=2); self.executor.add_node(self.node); self.thread=threading.Thread(target=self._spin,daemon=True); self.thread.start(); self.started=True; self.middleware_ready=True
    def close(self):
        if not self.started: return
        # Publish a final stop while the ROS context is still usable, then
        # prevent every later timer/worker publish before destroying the node.
        try: self.set_velocity(Twist2D())
        except Exception: pass
        with self._lock: self.started=False; self.middleware_ready=False
        try: self.executor.shutdown()
        except Exception: pass
        if self.thread: self.thread.join(3)
        try: self.node.destroy_node()
        except Exception: pass
        for process in self.processes:
            if process.poll() is None: os.killpg(process.pid,signal.SIGTERM)
        self.processes=[]
        global _RCLPY_USERS, _RCLPY_INITIALIZED_BY_SDK
        with _RCLPY_LOCK:
            if self._rclpy_registered:
                _RCLPY_USERS=max(0,_RCLPY_USERS-1); self._rclpy_registered=False
            if _RCLPY_USERS==0 and _RCLPY_INITIALIZED_BY_SDK:
                import rclpy
                if rclpy.ok(): rclpy.shutdown()
                _RCLPY_INITIALIZED_BY_SDK=False
    def set_velocity(self, command: Twist2D):
        with self._lock:
            if not self.started or not self.publisher: return
            import rclpy
            if not rclpy.ok(): return
            from geometry_msgs.msg import Twist
            message=Twist(); message.linear.x=command.linear; message.angular.z=command.angular
            try: self.publisher.publish(message)
            except Exception:
                # SIGINT can invalidate rclpy before Python gets to execute a
                # context manager's cleanup. Suppress only that shutdown race.
                if not rclpy.ok(): return
                raise
    def _spin(self):
        try: self.executor.spin()
        except Exception: pass  # rclpy may be stopped by SIGINT before close().
    def _on_odom(self,msg):
        q=msg.pose.pose.orientation; yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)); stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9
        values=(msg.pose.pose.position.x,msg.pose.pose.position.y,yaw,msg.twist.twist.linear.x,msg.twist.twist.angular.z)
        if not all(math.isfinite(float(v)) for v in values): return
        self._odometry_callback(Odometry(Pose2D(float(values[0]),float(values[1]),float(values[2])),Twist2D(float(values[3]),float(values[4])),stamp))
    def _on_scan(self,msg):
        if not msg.ranges or not math.isfinite(msg.range_min) or not math.isfinite(msg.range_max) or msg.range_min < 0 or msg.range_max <= msg.range_min: return
        values=[]
        for value in msg.ranges:
            value=float(value)
            if math.isinf(value) and value > 0: value=float(msg.range_max)
            values.append(value if math.isfinite(value) and msg.range_min <= value <= msg.range_max else float("nan"))
        valid=[value for value in values if math.isfinite(value)]
        if not valid: return
        center=len(msg.ranges)//2; central=[]
        for index in range(max(0,center-1),min(len(msg.ranges),center+2)):
            value=float(msg.ranges[index])
            if math.isinf(value) and value > 0: value=float(msg.range_max)
            if math.isfinite(value) and msg.range_min <= value <= msg.range_max: central.append(value)
        if not central: return
        stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9
        self._range_callback(RangeScan(tuple(values),min(central),min(valid),stamp,float(msg.angle_min),float(msg.angle_increment),float(msg.range_min),float(msg.range_max)))
    def _on_pose(self,msg):
        if not getattr(msg,"transforms",None): return
        candidates=list(msg.transforms)
        def child_name(transform):
            return (transform.child_frame_id or "").lstrip("/")
        # Never fall back to an arbitrary link or another model.  A missing
        # exact model transform means ground truth is unavailable.
        preferred=[t for t in candidates if child_name(t) == self.identity.robot_name]
        if not preferred: return
        transform=preferred[0]
        p=transform.transform.translation; q=transform.transform.rotation
        yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
        if not all(math.isfinite(float(v)) for v in (p.x,p.y,yaw)): return
        stamp=transform.header.stamp.sec+transform.header.stamp.nanosec/1e9
        self._ground_truth_callback(GroundTruthPose(Pose2D(float(p.x),float(p.y),yaw),stamp,transform.header.frame_id or "world",transform.child_frame_id))
    def _on_image(self,msg):
        import cv2, numpy as np
        channels=3 if msg.encoding in ("rgb8","bgr8") else 1; raw=np.frombuffer(msg.data,dtype=np.uint8).reshape(msg.height,msg.step)[:,:msg.width*channels].reshape(msg.height,msg.width,channels)
        if msg.encoding=="rgb8": raw=cv2.cvtColor(raw,cv2.COLOR_RGB2BGR)
        ok,data=cv2.imencode(".jpg",raw); stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9
        self._camera_callback(CameraFrame(stamp,msg.width,msg.height,"jpeg",bool(ok),data.tobytes() if ok else None))
    def _start_bridges(self):
        base=["ros2","run","ros_gz_bridge","parameter_bridge"]
        # Keep required feedback streams in separate processes. Besides
        # isolating failures, this lets the live validation harness prove that
        # losing one safety stream causes a fail-closed navigation result.
        specs={
            "motion":base+[f"{self.CMD_VEL}@geometry_msgs/msg/Twist]ignition.msgs.Twist",f"{self.ODOM}@nav_msgs/msg/Odometry[ignition.msgs.Odometry"],
            "range":base+[f"{self.SCAN}@sensor_msgs/msg/LaserScan[ignition.msgs.LaserScan"],
            "ground_truth":base+[f"{self.POSE}@tf2_msgs/msg/TFMessage[ignition.msgs.Pose_V"],
            "camera":["ros2","run","ros_gz_image","image_bridge",self.IMAGE],
        }
        self.bridge_processes={name:subprocess.Popen(args,start_new_session=True) for name,args in specs.items()}
        self.processes=list(self.bridge_processes.values())
