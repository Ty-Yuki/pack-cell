"""UR5e 2台(arm_a, arm_b) + 梱包台・棚・品物・箱のセルを起動する(ヘッドレス既定)。
usage: ros2 launch /ws/sim/launch/cell.launch.py [gui:=true]
事前に sim/gen_world.py で $SIM_BUILD (既定 /ws/sim/build) を生成しておく。
"""
import os, sys
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution, IfElseSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

sys.path.insert(0, "/ws/src")
from pack_cell import layout as L

BUILD = os.environ.get("SIM_BUILD", "/ws/sim/build")


def arm_nodes(arm, cfg):
    desc = Command([
        PathJoinSubstitution([FindExecutable(name="xacro")]), " /ws/sim/urdf/ur5e_suction.urdf.xacro",
        f" name:={arm} ur_type:=ur5e safety_limits:=true safety_pos_margin:=0.15 safety_k_position:=20",
        f" tf_prefix:=\"\" ros_namespace:={arm}",
        f" simulation_controllers:={BUILD}/{arm}_controllers.yaml",
        f" attach_plugins:={BUILD}/attach_{arm}.xacro",
    ])
    x, y = cfg["xy"]
    return [
        Node(package="robot_state_publisher", executable="robot_state_publisher", namespace=arm, output="log",
             parameters=[{"use_sim_time": True, "robot_description": desc, "frame_prefix": f"{arm}/"}]),
        Node(package="ros_gz_sim", executable="create", output="log",
             arguments=["-string", desc, "-name", arm, "-allow_renaming", "false",
                        "-x", str(x), "-y", str(y), "-z", str(L.BASE_Z), "-Y", str(cfg["yaw"])]),
        Node(package="controller_manager", executable="spawner",
             arguments=["joint_state_broadcaster", "-c", f"/{arm}/controller_manager"]),
        Node(package="controller_manager", executable="spawner",
             arguments=["scaled_joint_trajectory_controller", "-c", f"/{arm}/controller_manager"]),
    ]


def generate_launch_description():
    gui = LaunchConfiguration("gui")
    gz = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([FindPackageShare("ros_gz_sim"), "/launch/gz_sim.launch.py"]),
        launch_arguments={"gz_args": IfElseSubstitution(
            gui, if_value=f" -r -v 3 {BUILD}/world.sdf", else_value=f" -s -r -v 3 --headless-rendering {BUILD}/world.sdf")}.items())
    nodes = [DeclareLaunchArgument("gui", default_value="false"), gz,
             Node(package="ros_gz_bridge", executable="parameter_bridge", output="log",
                  parameters=[{"config_file": f"{BUILD}/bridge.yaml"}])]
    for arm, cfg in L.ARMS.items():
        nodes += arm_nodes(arm, cfg)
    return LaunchDescription(nodes)
