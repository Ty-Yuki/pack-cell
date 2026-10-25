"""Gazebo(gz-transport)との直接インターフェース: モデル姿勢・吸着/封函(DetachableJoint)・姿勢の再設定・カメラ画像。

ROS ブリッジ経由だと Pose_V の名前が落ちるため、Python バインディングで名前付きのまま扱う。
"""
import math, os, threading, time

import numpy as np
from gz.transport13 import Node
from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.empty_pb2 import Empty
from gz.msgs10.image_pb2 import Image
from gz.msgs10.pose_pb2 import Pose
from gz.msgs10.pose_v_pb2 import Pose_V
from gz.msgs10.world_stats_pb2 import WorldStatistics
from gz.msgs10.annotated_oriented_3d_box_v_pb2 import AnnotatedOriented3DBox_V
from gz.msgs10.entity_plugin_v_pb2 import EntityPlugin_V

from pack_cell import layout as L


# 吸着ジョイントを最初の吸着時にプラグインとして追加する方式(PACK_LAZY_JOINTS=1)。起動時の溶接がなくなるが、
# 実行中のサービス要求が不安定(応答がない・即座に失敗する)だったので、既定では URDF に入れておく方式を使う。
LAZY_JOINTS = os.environ.get("PACK_LAZY_JOINTS") == "1"


def yaw_of(q):
    """クォータニオン(w,x,y,z) -> yaw。"""
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def tilt_of(q):
    """鉛直からの傾き[rad](品物の z 軸と世界 z 軸のなす角)。"""
    w, x, y, z = q
    return math.acos(max(-1.0, min(1.0, 1 - 2 * (x * x + y * y))))


class GzIf:
    def __init__(self, world=L.WORLD):
        self.world = world
        self._node = Node()
        self._lock = threading.Lock()
        self._poses = {}          # name -> ((x,y,z), (w,x,y,z))
        self._sim_t = 0.0
        self._pubs = {}
        self._imgs = {}
        self._joints = set()      # 作成済みの吸着ジョイント (holder, child)
        self._svc = Node()        # サービス要求用(購読のコールバックで混む共有ノードと分ける。発見に時間がかかるので使い回す)
        self._svc_lock = threading.Lock()   # 2台が同時に要求すると応答が返らないことがあったので、1つずつ送る
        self._ids = {}
        # 静止中のモデルは dynamic_pose に出てこないため、全モデルの pose/info も購読する(低頻度)。
        for topic in (f"/world/{world}/pose/info", f"/world/{world}/dynamic_pose/info"):
            if not self._node.subscribe(Pose_V, topic, self._on_pose):
                raise RuntimeError(f"{topic} の購読に失敗")
        # sim 時刻は world の stats(10Hz)から取る。/clock(1kHz)は Python のコールバックが GIL を占有するため使わない。
        if not self._node.subscribe(WorldStatistics, f"/world/{world}/stats", self._on_stats):
            raise RuntimeError("world stats の購読に失敗")

    # ---- 観測 ----
    def _on_pose(self, msg):
        with self._lock:
            for p in msg.pose:
                if p.id and p.name not in self._ids:
                    self._ids[p.name] = p.id
                self._poses[p.name] = ((p.position.x, p.position.y, p.position.z),
                                       (p.orientation.w, p.orientation.x, p.orientation.y, p.orientation.z))

    def _on_stats(self, msg):
        with self._lock:
            self._sim_t = msg.sim_time.sec + msg.sim_time.nsec * 1e-9

    def wait_ready(self, names, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if all(n in self._poses for n in names):
                    return True
            time.sleep(0.1)
        return False

    def sim_time(self):
        with self._lock:
            return self._sim_t

    def sim_sleep(self, sec):
        t0 = self.sim_time()
        deadline = time.time() + sec * 5 + 5
        while self.sim_time() - t0 < sec and time.time() < deadline:
            time.sleep(0.02)

    def pose(self, name):
        with self._lock:
            return self._poses.get(name)

    def pos(self, name):
        p = self.pose(name)
        return None if p is None else np.array(p[0])

    # ---- カメラ ----
    def subscribe_camera(self, cam):
        topic = f"/{cam}/image"

        def cb(msg, cam=cam):
            img = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 3)
            st = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
            with self._lock:
                self._imgs[cam] = (st, img)
        if not self._node.subscribe(Image, topic, cb):
            raise RuntimeError(f"{topic} の購読に失敗")

    def subscribe_boxes(self, cam):
        """3D バウンディングボックスカメラ(/<cam>/boxes)。"""
        def cb(msg, cam=cam):
            st = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
            with self._lock:
                self._imgs[cam + ":boxes"] = (st, msg)
        if not self._node.subscribe(AnnotatedOriented3DBox_V, f"/{cam}/boxes", cb):
            raise RuntimeError(f"/{cam}/boxes の購読に失敗")

    def boxes(self, cam, newer_than=None, timeout=10.0):
        return self.image(cam + ":boxes", newer_than, timeout)

    def image(self, cam, newer_than=None, timeout=10.0):
        """最新画像(RGB)。newer_than(sim時刻)を指定すると、それより新しい画像が来るまで待つ。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                v = self._imgs.get(cam)
            if v is not None and (newer_than is None or v[0] > newer_than):
                return v
            time.sleep(0.03)
        return None

    # ---- 操作 ----
    def _pub(self, topic):
        if topic not in self._pubs:
            self._pubs[topic] = self._node.advertise(topic, Empty)
            time.sleep(0.3)  # advertise の伝播待ち
        return self._pubs[topic]

    def model_id(self, name):
        """モデルのエンティティ ID(姿勢メッセージに含まれる)。"""
        t0 = time.time()
        while name not in self._ids and time.time() - t0 < 10:
            time.sleep(0.05)
        return self._ids[name]

    def attach(self, holder, child, parent_link="suction_link"):
        """holder(アーム)に child を吸着する。
        吸着ジョイント(DetachableJoint)は、その組で最初に吸着するときにプラグインとして追加する。プラグインは追加した瞬間に
        今の相対姿勢で溶接するので、それがそのまま吸着になる。2回目以降は attach トピックで付け直す。
        (最初から URDF に入れておくと、ロード直後に全組が溶接され、解除の取りこぼしでアームが品に溶接されたまま残った)"""
        if not LAZY_JOINTS or (holder, child) in self._joints:
            self._pub(f"/{holder}/attach/{child}").publish(Empty())
            return
        req = EntityPlugin_V()
        req.entity.id = self.model_id(holder)
        p = req.plugins.add()
        p.name, p.filename = "gz::sim::systems::DetachableJoint", "gz-sim-detachable-joint-system"
        p.innerxml = (f"<parent_link>{parent_link}</parent_link><child_model>{child}</child_model><child_link>link</child_link>"
                      f"<attach_topic>/{holder}/attach/{child}</attach_topic><detach_topic>/{holder}/detach/{child}</detach_topic>"
                      f"<output_topic>/{holder}/state/{child}</output_topic>")
        for _ in range(3):
            with self._svc_lock:
                ok, rep = self._svc.request(f"/world/{self.world}/entity/system/add", req, EntityPlugin_V, Boolean, 10000)
            if ok and rep.data:
                break
        if not (ok and rep.data):
            raise RuntimeError(f"吸着ジョイントを追加できない: {holder}(id={req.entity.id}) → {child} 応答={ok},{rep.data if ok else None}")
        self._joints.add((holder, child))
        self._pub(f"/{holder}/detach/{child}")            # 解除トピックを先に用意しておく(発見の待ち時間)

    def detach(self, holder, child):
        if not LAZY_JOINTS or (holder, child) in self._joints:
            self._pub(f"/{holder}/detach/{child}").publish(Empty())

    def set_pose(self, name, xyz, yaw=0.0, timeout_ms=2000):
        return self.set_poses([(name, xyz, yaw)], timeout_ms)

    def set_poses(self, items, timeout_ms=3000):
        """[(name, (x,y,z), yaw), ...] を 1 回のサービス呼び出しで反映する。"""
        req = Pose_V()
        for name, xyz, yaw in items:
            p = req.pose.add(); p.name = name
            p.position.x, p.position.y, p.position.z = xyz
            p.orientation.w, p.orientation.z = math.cos(yaw / 2), math.sin(yaw / 2)
        ok, rep = self._node.request(f"/world/{self.world}/set_pose_vector", req, Pose_V, Boolean, timeout_ms)
        return bool(ok and rep.data)

    def release_all(self, pairs, rounds=3):
        """DetachableJoint はロード直後に子モデルを溶接した状態で始まるため、最初に必ず解除する。pairs: [(holder, child)]
        トピックを作った直後の1通目は、受信側の購読が確立する前で取りこぼすことがある(残るとアームが品に溶接されたまま
        動けない)。全組に一巡送ったあと、もう一度送り直す。"""
        for _ in range(rounds):
            for h, c in pairs:
                self.detach(h, c)
            time.sleep(2.0)
