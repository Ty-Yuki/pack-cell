"""UR5e 1台の制御(関節軌道の実行・吸着スキル)。目標はワールド座標(吸着面の位置 + ツールの yaw)で受ける。

1台につき1スレッドから使う前提(ノードごとに専用の Executor を持つ)。
- 上下の短い動き(接近・離脱)は直線補間(デカルト空間)
- 水平の長い移動は、搬送高さでの関節空間補間(ベース付近の特異点を通らない)
"""
import math, os, time

import numpy as np
import rclpy
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from control_msgs.action import FollowJointTrajectory
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from pack_cell import layout as L
from pack_cell.robot.ur_kin import fk, ik, HOME, TOOL_DOWN

JOINTS = ["shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
          "wrist_1_joint", "wrist_2_joint", "wrist_3_joint"]
TCP = 0.26                 # tool0 -> 吸着面(URDF の suction_joint と一致させる)。箱(26cm)の底を吸っても手首が縁に当たらない長さ
TRANSPORT_Z = 1.05         # 何も持たないときの搬送高さ(吸着面が箱の縁・棚の品より上を通る)
CLEAR_Z = 0.99             # 品を持つときは、品の底がこの高さより上を通る(箱の縁 0.96・棚の最も高い品 0.93)
CARTON_Z = 1.03            # 箱を運ぶときの吸着面(箱の内底)の高さ: 箱の底が棚の縦置きの品(洗剤 0.93m)より上を通る
SPEED = 1.2                # rad/s
SLOW = 0.35                # 置く直前の速度倍率


def _rz(t):
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


DEBUG = bool(os.environ.get("PACK_DEBUG"))
# 肩の旋回角は、1周分の窓 [PAN_LO, PAN_LO + 2π) に必ず収める。窓の境目はベースから見て真後ろ(コンベヤと反対側。
# そこには何もなく、通る必要がない)。「現在値に近い等価角」で選ぶと角度が巻き付き、可動範囲で折り返したときに
# 逆回りの経路になって箱や相手と当たった。窓に収めれば、上流側と下流側の棚の行き来は必ずベースの前(梱包位置の上空)
# を通る(スケジューラは共有ゾーンを取ってから移る)。(home の旋回角 ≒ 方位 - 0.39 rad)
PAN_LO = -math.pi - 0.39
WRIST3_MAX = 2 * math.pi - 0.4


def normalize(q, seed):
    """IK の解を可動範囲内に正規化する: 肩の旋回は窓の中、ほかの関節はシードに最も近い等価角(手首3は ±2π 内)。"""
    q = np.array(q, float)
    for i in range(1, 6):
        q[i] = seed[i] + wrap(q[i] - seed[i])
    q[0] = PAN_LO + (q[0] - PAN_LO) % (2 * math.pi)
    while q[5] > WRIST3_MAX:
        q[5] -= 2 * math.pi
    while q[5] < -WRIST3_MAX:
        q[5] += 2 * math.pi
    return q


class ArmError(RuntimeError):
    pass


class Arm(Node):
    def __init__(self, name):
        super().__init__(f"{name}_ctrl", parameter_overrides=[Parameter("use_sim_time", value=True)])
        cfg = L.ARMS[name]
        self.name = name
        self.base = np.array([*cfg["xy"], L.BASE_Z])
        self.yaw = cfg["yaw"]
        self.q = None
        self.tool_yaw = 0.0          # 吸着面のワールド yaw(ツール x 軸の向き)
        self.carry_z = None          # 品を持っている間の搬送高さ(Skills が設定)
        self.exec = SingleThreadedExecutor(); self.exec.add_node(self)
        self.create_subscription(JointState, f"/{name}/joint_states", self._js, 10)
        self.ac = ActionClient(self, FollowJointTrajectory,
                               f"/{name}/scaled_joint_trajectory_controller/follow_joint_trajectory")
        if not self.spin_until(lambda: self.q is not None, timeout=30):
            raise ArmError(f"{name}: joint_states が来ない")

    # ---- 座標変換 ----
    def to_local(self, p):
        d = np.asarray(p, float) - self.base
        c, s = math.cos(-self.yaw), math.sin(-self.yaw)
        return np.array([c * d[0] - s * d[1], s * d[0] + c * d[1], d[2]])

    def to_world(self, p):
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return self.base + np.array([c * p[0] - s * p[1], s * p[0] + c * p[1], p[2]])

    def _target(self, tip_world, tool_yaw):
        """吸着面のワールド位置・yaw -> base 系での tool0 の位置と姿勢。"""
        R = _rz(tool_yaw - self.yaw) @ TOOL_DOWN
        return self.to_local(tip_world) + R[:, 2] * -TCP, R

    def tip_world(self, q=None):
        T = fk(self.q if q is None else q)
        return self.to_world(T[:3, 3] + T[:3, 2] * TCP)

    def solve(self, tip_world, tool_yaw, seed):
        pos, R = self._target(tip_world, tool_yaw)
        q = ik(pos, R, seed)
        return None if q is None else normalize(q, seed)

    def reachable(self, tip_world, tool_yaw=0.0):
        return self._ik_far(tip_world, tool_yaw) is not None

    def _ik_far(self, tip_world, tool_yaw, seed=None):
        """遠い目標: 肩の旋回角を目標の方位に合わせたシードから解く(肘が上の解を得やすい)。
        肩を反転させて頭越しに折り返す解(旋回が目標と逆を向き、上腕がコンベヤの上空に張り出す)は採らない。"""
        seed = np.array(HOME if seed is None else seed, float)
        loc = self.to_local(tip_world)
        az = math.atan2(loc[1], loc[0])
        cur = self.to_local(self.tip_world(seed))
        s = seed.copy()
        s[0] += wrap(az - math.atan2(cur[1], cur[0]))
        s[0] = PAN_LO + (s[0] - PAN_LO) % (2 * math.pi)            # 窓の中のシードから解く
        home = np.array([az - 0.39, *HOME[1:]])
        home[0] = PAN_LO + (home[0] - PAN_LO) % (2 * math.pi)
        fallback = None
        for cand in (s, np.array([s[0], *HOME[1:]]), home):
            q = self.solve(tip_world, tool_yaw, cand)
            if q is None:
                continue
            if abs(wrap(q[0] - (az - 0.39))) < 1.2 or np.hypot(loc[0], loc[1]) < 0.15:
                return q
            fallback = fallback if fallback is not None else q
        return fallback

    def nearest_tool_yaw(self, yaw, period=math.pi):
        """吸着面は対称なので、目標 yaw(周期 period)のうち現在に最も近いものを選ぶ。"""
        return self.tool_yaw + wrap((yaw - self.tool_yaw) * (2 * math.pi / period)) * (period / (2 * math.pi))

    # ---- 実行系 ----
    def _js(self, m):
        d = dict(zip(m.name, m.position))
        if all(j in d for j in JOINTS):
            self.q = np.array([d[j] for j in JOINTS])

    def spin_until(self, cond, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout and not cond():
            self.exec.spin_once(timeout_sec=0.05)
        return cond()

    def sim_sleep(self, sec):
        t0 = self.get_clock().now()
        self.spin_until(lambda: (self.get_clock().now() - t0) >= Duration(seconds=sec), timeout=sec * 5 + 5)

    def move_joints(self, waypoints, speed=SPEED):
        pts, t, prev = [], 0.0, self.q
        for w in waypoints:
            t += max(0.2, float(np.max(np.abs(np.asarray(w) - prev))) / speed)
            p = JointTrajectoryPoint(); p.positions = [float(x) for x in w]
            p.time_from_start = Duration(seconds=t).to_msg()
            pts.append(p); prev = np.asarray(w)
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = JOINTS; goal.trajectory.points = pts
        if not self.ac.wait_for_server(timeout_sec=10):
            raise ArmError(f"{self.name}: 軌道アクションに接続できない")
        fut = self.ac.send_goal_async(goal)
        self.spin_until(fut.done); gh = fut.result()
        if gh is None or not gh.accepted:
            raise ArmError(f"{self.name}: 軌道が拒否された")
        res = gh.get_result_async()
        self.spin_until(res.done, timeout=t * 4 + 10)
        if not res.done() or res.result().result.error_code != 0:
            code = res.result().result.error_code if res.done() else "timeout"
            if DEBUG:
                print(f"[{self.name}] 失敗時 q={np.round(self.q, 2)} 目標={np.round(prev, 2)}", flush=True)
            raise ArmError(f"{self.name}: 軌道の実行に失敗 ({code})")
        self.spin_until(lambda: float(np.max(np.abs(self.q - prev))) < 0.02, timeout=3.0)

    def line(self, p1, tool_yaw=None, n=8, speed=SPEED):
        """現在の吸着面 -> p1 を直線で(IK の直前解をシードにして連続性を保つ)。"""
        tool_yaw = self.tool_yaw if tool_yaw is None else tool_yaw
        p0, y0 = self.tip_world(), self.tool_yaw
        q, out = self.q.copy(), []
        for s in np.linspace(0, 1, n + 1)[1:]:
            q = self.solve((1 - s) * p0 + s * np.asarray(p1), y0 + s * (tool_yaw - y0), q)
            if q is None:
                break
            out.append(q)
        if q is None:
            # 途中で解けない(特異姿勢の近く) → 終点を直接解き、関節の変化が小さければ関節空間で動く
            q_end = self._ik_far(np.asarray(p1), tool_yaw, self.q)
            if q_end is None or float(np.max(np.abs(q_end - self.q))) > 0.6:     # 大きく変わる解は別の姿勢への切り替え(危険)
                raise ArmError(f"{self.name}: IK失敗(直線) {np.round(p1, 3)}")
            out = [q_end]
            speed = min(speed, 0.6)
        self.move_joints(out, speed)
        self.tool_yaw = tool_yaw

    def transfer(self, p1, tool_yaw=None, speed=SPEED, z=None):
        """搬送高さ z(既定 TRANSPORT_Z)まで上がり、関節空間で p1 の上空へ移り、p1 の高さまで下りる。"""
        tool_yaw = self.tool_yaw if tool_yaw is None else tool_yaw
        Z = self.hold_z() if z is None else z
        cur = self.tip_world()
        if abs(cur[2] - Z) > 0.01:
            self.line((cur[0], cur[1], Z), n=4, speed=speed)
        above = np.array([p1[0], p1[1], Z])
        q1 = self._ik_far(above, tool_yaw, self.q)
        if q1 is None:
            raise ArmError(f"{self.name}: IK失敗(搬送) {np.round(above, 3)}")
        # 関節空間の補間で高さが沈まないよう、途中点も搬送高さで解き直す
        mids = []
        for s in (0.33, 0.66):
            qm = (1 - s) * self.q + s * q1
            pm = self.tip_world(qm)
            qm2 = self.solve((pm[0], pm[1], Z), self.tool_yaw + s * (tool_yaw - self.tool_yaw), qm)
            mids.append(qm if qm2 is None else qm2)
        if DEBUG:
            print(f"[{self.name}] transfer q0={np.round(self.q, 2)} mids={[np.round(m, 2).tolist() for m in mids]} q1={np.round(q1, 2)}", flush=True)
        self.move_joints(mids + [q1], speed)
        self.tool_yaw = tool_yaw
        if abs(p1[2] - Z) > 0.01:
            self.line(p1, n=6, speed=speed)

    def park_point(self, side):
        """自陣の待機点(共有ゾーンの外)。ベースの斜め後ろ 0.39m の、side 側(-1: 上流, +1: 下流)。
        ベースに近すぎると IK が腕を頭越しに折り返す姿勢を選び、肘が梱包位置の上空に出てしまう。"""
        p = self.to_world(np.array([-0.28, 0.33, 0.0]))
        q = self.to_world(np.array([-0.28, -0.33, 0.0]))
        p = p if (p[0] > q[0]) == (side > 0) else q
        return np.array([p[0], p[1], self.hold_z()])

    def hold_z(self):
        """今の搬送高さ(持っている品が高いほど高く)。"""
        return max(TRANSPORT_Z, self.carry_z or 0.0)

    def park_side(self):
        """起動時の待機点の側(上流側。スケジューラの at_side の初期値と合わせる)。"""
        return -1

    def home(self):
        """自陣の待機点へ。起動直後(真上向き)からでも確実に移れるよう、関節空間で直接動く。"""
        target = self.park_point(self.park_side())
        q = self._ik_far(target, self.yaw, HOME)
        if q is None:
            raise ArmError(f"{self.name}: 待機姿勢の IK 失敗")
        for i in range(5):
            try:
                pre = self.q.copy()
                pre[0], pre[5] = q[0], q[5]          # まず真上を向いたまま旋回し、それから腕を下ろす
                self.move_joints([pre, q] if float(np.linalg.norm(self.tip_world() - target)) > 0.05 else [q], speed=0.8)
            except ArmError as e:
                self.get_logger().warn(f"home 再試行 {i + 1}: {e}")
            self.sim_sleep(0.5)
            if float(np.linalg.norm(self.tip_world() - target)) < 0.01:      # 実際に着いたかを確かめる
                break
            self.get_logger().warn(f"home 未到達(試行 {i + 1}) 誤差 {np.linalg.norm(self.tip_world() - target):.3f}m")
        else:
            raise ArmError(f"{self.name}: 待機姿勢へ移れない")
        self.tool_yaw = self.yaw

    def staging_point(self, side):
        """共有ゾーンの外で、品物を持って待つ点。side: -1(上流側の棚の上) / +1(下流側の棚の上)。ベースの真横やや前 0.40m(反対側へ移るときの旋回を小さくする)。
        ベースに近すぎると腕を折りたたむ姿勢になり、肘が梱包位置の上空に出る。"""
        back = 1 if self.base[1] > 0 else -1
        return np.array([side * 0.40, self.base[1] - back * 0.04, self.hold_z()])
