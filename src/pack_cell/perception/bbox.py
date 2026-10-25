"""棚の天井カメラ(Gazebo の 3D バウンディングボックスカメラ)の出力 → 品物の中心(ワールド座標)・向き・寸法。

ラベル番号(world_spec が各品に振る)で品を見分ける。カメラに映っていない品(腕で隠れた・欠品)は出てこない。
真下向きのカメラでは、出力の座標系はカメラの位置を原点に z 軸まわりに 90° 回った向きになる(check_perception で真値と照合)。
"""
import math
from dataclasses import dataclass

from pack_cell import layout as L


@dataclass
class Detection:
    label: int
    xyz: tuple          # 外形の中心(ワールド)
    yaw: float          # 品の x 軸(W)の向き(ワールド)
    size: tuple         # 外形の寸法(品の座標系)


def _cam_pose(cam):
    x, y, z, r, p, yw = (float(v) for v in L.CAMERAS[cam][0].split())
    return (x, y, z), yw


def to_world(msg, cam):
    (cx, cy, cz), cyaw = _cam_pose(cam)
    c, s = math.cos(cyaw), math.sin(cyaw)
    out = []
    for b in msg.annotated_box:
        ob = b.box
        # 出力系 (u, v, w) → ワールド: (v, -u, w) をカメラの yaw だけ回してカメラの位置へ
        u, v, w = ob.center.x, ob.center.y, ob.center.z
        lx, ly = v, -u
        wx, wy = cx + c * lx - s * ly, cy + s * lx + c * ly
        q = ob.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) - math.pi / 2 + cyaw
        yaw = (yaw + math.pi) % (2 * math.pi) - math.pi
        out.append(Detection(int(b.label), (wx, wy, cz + w), yaw, (ob.boxSize.x, ob.boxSize.y, ob.boxSize.z)))
    return out
