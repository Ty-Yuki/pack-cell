"""ワールド(SDF)・アームごとの吸着プラグイン(xacro)・コントローラ設定・品物と箱のメッシュを生成する。

DetachableJoint は(親リンク x 子モデル)の組ごとに定義が必要なため、手書きせず生成する。
品物は在庫スロットごとに1モデル。出荷後は WorldManager が棚へ戻して再利用する(補充)。
usage: gen_world.py <out_dir> [--seed N]
"""
import argparse, math, pathlib, random, re, sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "sim"))
from pack_cell import layout as L          # noqa: E402
from pack_cell import world_spec as W      # noqa: E402
from pack_cell.core import catalog         # noqa: E402
import meshes                              # noqa: E402

CAT = catalog.load()
MESH_DIR = {}                              # SKU -> 生成した見た目のメッシュ [(OBJ, 透明度)](段ボール箱の本体は "carton"、フラップは名前)

STOCK_CONTROLLERS = "/opt/ros/jazzy/share/ur_simulation_gz/config/ur_controllers.yaml"
BBOX_CAMS = {"cam_shelf_l", "cam_shelf_r"}     # 棚の知覚は 3D バウンディングボックスカメラ(ラベル付きの品の位置・向き・寸法)


def _inertia(m, sx, sy, sz):
    return (f"<inertial><mass>{m}</mass><inertia><ixx>{m * (sy * sy + sz * sz) / 12}</ixx><iyy>{m * (sx * sx + sz * sz) / 12}</iyy>"
            f"<izz>{m * (sx * sx + sy * sy) / 12}</izz><ixy>0</ixy><ixz>0</ixz><iyz>0</iyz></inertia></inertial>")


def _mat(rgb, a=1.0):
    c = f"{rgb[0]} {rgb[1]} {rgb[2]} {a}"
    return f"<material><ambient>{c}</ambient><diffuse>{c}</diffuse></material>"


def _boxgeom(s):
    return f"<geometry><box><size>{s[0]} {s[1]} {s[2]}</size></box></geometry>"


SURF = "<surface><friction><ode><mu>0.8</mu><mu2>0.8</mu2></ode></friction><contact><ode><kp>1e6</kp><kd>100</kd><max_vel>0.1</max_vel><min_depth>0.0005</min_depth></ode></contact></surface>"


def static_box(name, xyz, size, rgb):
    return f"""
    <model name="{name}"><static>true</static><pose>{xyz[0]} {xyz[1]} {xyz[2]} 0 0 0</pose><link name="link">
      <collision name="c">{_boxgeom(size)}{SURF}</collision>
      <visual name="v">{_boxgeom(size)}{_mat(rgb)}</visual>
    </link></model>"""


def pedestal(name, xy):
    return f"""
    <model name="{name}"><static>true</static><pose>{xy[0]} {xy[1]} {L.BASE_Z / 2} 0 0 0</pose><link name="link">
      <collision name="c"><geometry><cylinder><radius>0.11</radius><length>{L.BASE_Z}</length></cylinder></geometry></collision>
      <visual name="v"><geometry><cylinder><radius>0.11</radius><length>{L.BASE_Z}</length></cylinder></geometry>{_mat((0.3, 0.32, 0.36))}</visual>
    </link></model>"""


def _box(sx, sy, sz):
    return f"<geometry><box><size>{sx} {sy} {sz}</size></box></geometry>"


def _cyl(r, l):
    return f"<geometry><cylinder><radius>{r}</radius><length>{l}</length></cylinder></geometry>"


def item_sdf(it, tex_dir, cat):
    """品物: 実寸の外形で衝突(直方体、缶・カップは円柱)、見た目は形状ごと。
    知覚用のラベル番号は、外形と同じ大きさの見えない形状1つだけに付ける(見た目の部品が複数あると、カメラが回転なしの外接箱を返すため)。"""
    sku = cat[it.sku]
    sx, sy, sz = it.size
    geom = _cyl(sx / 2, sz) if sku.round else _boxgeom(it.size)
    return f"""
    <model name="{it.name}"><pose>{it.x} {it.y} {it.z} 0 0 {it.yaw}</pose><link name="link">
      {_inertia(it.mass, sx, sy, sz)}
      <collision name="c">{geom}{SURF}</collision>
      {meshes.visuals(MESH_DIR[it.sku])}
      <visual name="bbox_proxy">{geom}<transparency>1.0</transparency>
        <plugin filename="gz-sim-label-system" name="gz::sim::systems::Label"><label>{it.label}</label></plugin></visual>
    </link></model>"""


def carton_sdf(name, xyz, yaw, size="L"):
    """上面が開いた段ボール箱と4枚のフラップ(関節の位置制御で開閉。当たり判定なし)。長いフラップの表にテープ
    (開いている間はフラップと壁の間に隠れ、閉じると現れる)。"""
    ox, oy, oz = L.box_outer(size)
    w, T = L.BOX_WALL, L.FLAP_T
    parts = [((0, 0, -oz / 2 + w / 2), (ox, oy, w)),
             ((0, oy / 2 - w / 2, 0), (ox, w, oz)), ((0, -oy / 2 + w / 2, 0), (ox, w, oz)),
             ((ox / 2 - w / 2, 0, 0), (w, oy - 2 * w, oz)), ((-ox / 2 + w / 2, 0, 0), (w, oy - 2 * w, oz))]
    geo = "".join(f'<collision name="c{i}"><pose>{p[0]} {p[1]} {p[2]} 0 0 0</pose>{_boxgeom(s)}{SURF}</collision>'
                  for i, (p, s) in enumerate(parts)) + meshes.visuals(MESH_DIR[f"carton_{size}"])
    links = joints = plugins = ""
    for f, sgn in L.FLAPS.items():
        hinge, axis, (L_, W_) = _flap_geom(size)[f]
        tape = ""
        if f.startswith("fy"):
            inward = -1 if hinge[1] > 0 else 1
            tape = (f'<visual name="tape"><pose>0 {inward * (L_ - 0.012)} {T + 0.0004} 0 0 0</pose>'
                    f'<geometry><box><size>{W_ + 0.002} 0.024 0.0008</size></box></geometry>{_mat((0.66, 0.50, 0.30))}</visual>')
        fsize = (L_, W_, T) if axis[1] else (W_, L_, T)
        links += (f'<link name="{f}"><pose relative_to="link">{hinge[0]} {hinge[1]} {hinge[2]} 0 0 0</pose>{_inertia(0.02, *fsize)}'
                  f'{meshes.visuals(MESH_DIR[f"{size}_{f}"], prefix="v")}{tape}</link>')
        joints += (f'<joint name="{f}_j" type="revolute"><parent>link</parent><child>{f}</child>'
                   f'<axis><xyz>{axis[0]} {axis[1]} {axis[2]}</xyz><limit><lower>-5</lower><upper>5</upper></limit></axis></joint>')
        plugins += (f'<plugin filename="gz-sim-joint-position-controller-system" name="gz::sim::systems::JointPositionController">'
                    f'<joint_name>{f}_j</joint_name><use_velocity_commands>true</use_velocity_commands><p_gain>4</p_gain>'
                    f'<initial_position>{sgn * L.FLAP_OPEN}</initial_position><cmd_max>2.5</cmd_max><cmd_min>-2.5</cmd_min></plugin>')
    return f"""
    <model name="{name}"><pose>{xyz[0]} {xyz[1]} {xyz[2]} 0 0 {yaw}</pose><link name="link">
      {_inertia(L.BOX_MASS * ox * oy / 0.1408, ox, oy, oz)}{geo}
    </link>{links}{joints}{plugins}</model>"""


def _flap_geom(size="L"):
    """フラップ: 名前 -> (ヒンジの位置(箱の中心から), 回転軸, (内向きの長さ, 幅))。ヒンジは外側の上の角。
    短いフラップ(±x)は壁の上、長いフラップ(±y)はその上に重なるよう厚み分だけ高い。"""
    ox, oy, oz = L.box_outer(size)
    T = L.FLAP_T
    return {"fx0": ((ox / 2 + T, 0, oz / 2), (0, 1, 0), (oy / 2 - 0.01, oy - 0.004)),
            "fx1": ((-ox / 2 - T, 0, oz / 2), (0, 1, 0), (oy / 2 - 0.01, oy - 0.004)),
            "fy0": ((0, oy / 2 + T, oz / 2 + T), (1, 0, 0), (oy / 2, ox)),
            "fy1": ((0, -oy / 2 - T, oz / 2 + T), (1, 0, 0), (oy / 2, ox))}


def _flap_mesh_args(f, size="L"):
    """フラップのメッシュ(ヒンジ原点の座標系): 閉じた状態で内向きに L、厚み T は上。"""
    hinge, axis, (L_, W_) = _flap_geom(size)[f]
    T = L.FLAP_T
    if axis[1]:
        inward = -1 if hinge[0] > 0 else 1
        return (inward * L_ / 2, 0, T / 2), (L_, W_, T)
    inward = -1 if hinge[1] > 0 else 1
    return (0, inward * L_ / 2, T / 2), (W_, L_, T)


def conveyor_sdf(name, x0, x1):
    """ベルトコンベヤの1区間(TrackController でベルト面を動かす)と、両脇の枠・脚。
    区間ごとに別々に動く・止まるので、見た目でも分かれているように、両端にローラーを付けて境目に隙間を空ける
    (当たり判定は隙間なし。箱が境目に引っかからない)。梱包区間はレールの色を変える。"""
    Lx, W, top = x1 - x0, L.BELT_W, L.TABLE_TOP
    cx = (x0 + x1) / 2
    gap = 0.018                                    # 見た目の境目の隙間(片側)
    vL = Lx - 2 * gap
    rail = (0.30, 0.42, 0.62) if name == "belt_pack" else (0.70, 0.72, 0.75)
    frame = ""
    for sy in (1, -1):
        frame += (f'<visual name="rail{sy + 1}"><pose>0 {sy * (W / 2 + 0.02)} {0.012} 0 0 0</pose>{_box(vL, 0.03, 0.07)}'
                  f'{_mat(rail)}</visual>')
        for k, lx in enumerate((-vL / 2 + 0.05, vL / 2 - 0.05)):
            frame += (f'<visual name="leg{sy + 1}{k}"><pose>{lx} {sy * (W / 2 + 0.02)} {-(top - 0.05) / 2 - 0.02} 0 0 0</pose>'
                      f'{_box(0.04, 0.04, top - 0.05)}{_mat((0.45, 0.47, 0.50))}</visual>')
    ends = "".join(f'<visual name="end{k}"><pose>{sx * (vL / 2 - 0.02)} 0 0 1.5708 0 0</pose>{_cyl(0.021, W + 0.01)}'
                   f'{_mat((0.62, 0.64, 0.68))}</visual>' for k, sx in enumerate((-1, 1)))
    n = max(1, round(vL / 0.25))
    rollers = "".join(f'<visual name="roll{k}"><pose>{-vL / 2 + vL * k / n} 0 -0.028 1.5708 0 0</pose>'
                      f'{_cyl(0.012, W)}{_mat((0.55, 0.56, 0.58))}</visual>' for k in range(n + 1))
    return f"""
    <model name="{name}"><static>true</static><pose>{cx} 0 {top - 0.02} 0 0 0</pose><link name="link">
      <collision name="c">{_box(Lx, W, 0.04)}<surface><friction><ode><mu>0.9</mu><mu2>0.9</mu2></ode></friction></surface></collision>
      <visual name="belt"><pose>0 0 -0.001 0 0 0</pose>{_box(vL - 0.04, W, 0.04)}<material><ambient>0.16 0.17 0.19 1</ambient><diffuse>0.16 0.17 0.19 1</diffuse><specular>0.05 0.05 0.05 1</specular></material></visual>
      {ends}{frame}{rollers}
    </link>
      <plugin filename="gz-sim-track-controller-system" name="gz::sim::systems::TrackController"><link>link</link>
        <max_acceleration>{L.BELT_ACC}</max_acceleration><min_acceleration>-{L.BELT_ACC}</min_acceleration></plugin>
    </model>"""


def machine_sdf():
    """製函機(上流端の筐体。組み立てた空箱が出てくる)と封函機(フラップを閉じてテープを貼るヘッドと両脇のガイド)。見た目のみ。"""
    top = L.TABLE_TOP
    steel, dark, kraft = (0.55, 0.62, 0.70), (0.25, 0.27, 0.30), (0.72, 0.56, 0.38)
    v = []
    ex0, ex1 = -1.95, ERECTOR_X1
    cx, w = (ex0 + ex1) / 2, ex1 - ex0
    v += [("er_top", (cx, 0, 1.40), (w, 0.62, 0.04), steel), ("er_back", (ex0, 0, 1.03), (0.04, 0.62, 0.70), steel),
          ("er_l", (cx, 0.31, 1.03), (w, 0.04, 0.70), steel), ("er_r", (cx, -0.31, 1.03), (w, 0.04, 0.70), steel),
          ("er_base_l", (cx, 0.31, top / 2), (w, 0.04, top), dark), ("er_base_r", (cx, -0.31, top / 2), (w, 0.04, top), dark),
          ("er_panel", (cx, -0.335, 1.15), (0.20, 0.01, 0.14), (0.15, 0.55, 0.30)),
          ("blanks", (cx - 0.05, 0.62, 0.30), (0.62, 0.50, 0.60), kraft)]           # 平たく畳んだ段ボールの束
    sx0, sx1 = L.SEAL_X - 0.28, L.SEAL_X + 0.28
    scx, sw = (sx0 + sx1) / 2, sx1 - sx0
    v += [("se_head", (scx, 0, 1.12), (sw, 0.46, 0.16), steel), ("se_head_front", (sx0 - 0.02, 0, 1.12), (0.04, 0.40, 0.12), dark),
          ("se_col_l", (scx, 0.27, (top + 1.04) / 2), (0.08, 0.06, 1.04 - top), steel),
          ("se_col_r", (scx, -0.27, (top + 1.04) / 2), (0.08, 0.06, 1.04 - top), steel),
          ("se_guide_l", (scx, 0.215, top + 0.13), (sw, 0.02, 0.10), dark), ("se_guide_r", (scx, -0.215, top + 0.13), (sw, 0.02, 0.10), dark),
          ("se_base_l", (scx, 0.27, top / 2), (0.08, 0.06, top), dark), ("se_base_r", (scx, -0.27, top / 2), (0.08, 0.06, top), dark),
          ("se_panel", (scx, -0.245, 1.12), (0.16, 0.01, 0.10), (0.15, 0.55, 0.30))]
    out = "".join(f'<visual name="{n}"><pose>{p[0]} {p[1]} {p[2]} 0 0 0</pose>{_box(*s)}{_mat(c)}</visual>' for n, p, s, c in v)
    roll = (f'<visual name="tape_roll"><pose>{scx + 0.05} 0 1.24 1.5708 0 0</pose>{_cyl(0.07, 0.05)}{_mat((0.66, 0.50, 0.30))}</visual>')
    return f"""
    <model name="machines"><static>true</static><link name="link">{out}{roll}</link></model>"""


ERECTOR_X1 = -1.15                         # 製函機の筐体の下流端(箱の出口)


def _dj(parent_link, child, prefix):
    return f"""
      <plugin filename="gz-sim-detachable-joint-system" name="gz::sim::systems::DetachableJoint">
        <parent_link>{parent_link}</parent_link><child_model>{child}</child_model><child_link>link</child_link>
        <attach_topic>{prefix}/attach/{child}</attach_topic><detach_topic>{prefix}/detach/{child}</detach_topic>
        <output_topic>{prefix}/state/{child}</output_topic>
      </plugin>"""


def camera_sdf(name, pose, w, h, fov, hz):
    if name in BBOX_CAMS:
        sensor = (f'<sensor name="cam" type="boundingbox_camera"><always_on>1</always_on><update_rate>{hz}</update_rate>'
                  f'<topic>/{name}/boxes</topic><camera><box_type>3d</box_type><horizontal_fov>{fov}</horizontal_fov>'
                  f'<image><width>{w}</width><height>{h}</height></image><clip><near>0.05</near><far>20</far></clip></camera></sensor>')
    else:
        sensor = (f'<sensor name="cam" type="camera"><always_on>1</always_on><update_rate>{hz}</update_rate><topic>/{name}/image</topic>'
                  f'<camera><horizontal_fov>{fov}</horizontal_fov><image><width>{w}</width><height>{h}</height></image>'
                  f'<clip><near>0.05</near><far>20</far></clip></camera></sensor>')
    return f"""
    <model name="{name}"><static>true</static><pose>{pose}</pose><link name="link">{sensor}</link></model>"""


def world_sdf(spec, tex_dir):
    m = []
    for name, (x0, x1) in L.BELTS.items():
        m.append(conveyor_sdf(f"belt_{name}", x0, x1))
    m.append(machine_sdf())
    for arm, cfg in L.ARMS.items():
        m.append(pedestal(f"pedestal_{arm}", cfg["xy"]))
    x0, x1 = L.SHELF_X
    for sh in ("L", "R"):
        sgn = L.SHELF_SIGN[sh]
        rgb = (0.62, 0.48, 0.32) if sh == "L" else (0.40, 0.52, 0.66)
        for side in (-1, 1):
            for k, (y0, y1, top) in L.SHELF_STEPS.items():
                m.append(static_box(f"shelf_{sh}{side + 1}_{k}", (side * (x0 + x1) / 2, sgn * (y0 + y1) / 2, top / 2), (x1 - x0, y1 - y0, top), rgb))
    # 仮置き台(ArmB の下流側の棚の奥の段に敷いた黄色いトレイ)
    y0, y1, top = L.SHELF_STEPS["back"]
    m.append(f"""
    <model name="buffer_tray"><static>true</static><pose>{(x0 + x1) / 2} {-(y0 + y1) / 2} {top + 0.001} 0 0 0</pose><link name="link">
      <visual name="v">{_box(x1 - x0 - 0.02, y1 - y0 - 0.02, 0.002)}{_mat((0.92, 0.78, 0.25))}</visual></link></model>""")
    for c, args in L.CAMERAS.items():
        m.append(camera_sdf(c, *args))
    m += [item_sdf(it, tex_dir, CAT) for it in spec.items]
    m += [carton_sdf(b.name, (b.x, b.y, b.z), b.yaw, b.size) for b in spec.boxes]
    return f"""<?xml version="1.0"?>
<sdf version="1.9">
  <world name="{L.WORLD}">
    <physics name="1ms" type="ode"><max_step_size>0.001</max_step_size><real_time_factor>1.0</real_time_factor></physics>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>
    <scene><ambient>0.55 0.55 0.55 1</ambient><background>0.85 0.88 0.92 1</background><shadows>true</shadows></scene>
    <light type="directional" name="sun"><cast_shadows>true</cast_shadows><pose>0 0 10 0 0 0</pose>
      <diffuse>0.85 0.85 0.85 1</diffuse><specular>0.1 0.1 0.1 1</specular><direction>-0.3 0.2 -0.9</direction></light>
    <light type="point" name="fill"><cast_shadows>false</cast_shadows><pose>0 0 2.6 0 0 0</pose>
      <diffuse>0.35 0.35 0.35 1</diffuse><attenuation><range>8</range><constant>0.6</constant><linear>0.05</linear></attenuation></light>
    <model name="ground"><static>true</static><link name="link">
      <collision name="c"><geometry><plane><normal>0 0 1</normal><size>16 16</size></plane></geometry></collision>
      <visual name="v"><geometry><plane><normal>0 0 1</normal><size>16 16</size></plane></geometry>{_mat((0.78, 0.78, 0.76))}</visual>
    </link></model>
    {''.join(m)}
  </world>
</sdf>
"""


def attach_xacro(arm, names):
    plugins = "".join(f"<gazebo>{_dj('suction_link', n, f'/{arm}')}\n  </gazebo>" for n in names)
    return f'<?xml version="1.0"?>\n<robot xmlns:xacro="http://wiki.ros.org/xacro">{plugins}\n</robot>\n'


def controllers_yaml(arm):
    """素の ur_controllers.yaml のトップレベルキーを /<arm>/ 名前空間付きに書き換える。"""
    src = pathlib.Path(STOCK_CONTROLLERS).read_text()
    return re.sub(r"^([A-Za-z_][A-Za-z0-9_]*):", rf"/{arm}/\1:", src, flags=re.M)


BRIDGE = """- ros_topic_name: /clock
  gz_topic_name: /clock
  ros_type_name: rosgraph_msgs/msg/Clock
  gz_type_name: gz.msgs.Clock
  direction: GZ_TO_ROS
"""  # 姿勢・attach/detach・カメラは gz-transport を Python から直接使う(pack_cell/sim/gzif.py)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--attach", default="all", help="all(既定) | none(PACK_LAZY_JOINTS=1 で実行中に追加する場合) | items  ※切り分け用")
    a = ap.parse_args()
    out = pathlib.Path(a.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    spec = W.build(random.Random(a.seed))
    for sku in CAT:                        # 品物の見た目(実寸のメッシュ + 商品ラベル)
        MESH_DIR[sku.id] = meshes.build(sku, out / "models" / sku.id)
    for size in L.BOX_ORDER:               # 箱(サイズごと)の本体とフラップ
        d = out / "models" / f"carton_{size}"
        MESH_DIR[f"carton_{size}"] = meshes.carton(d, L.box_outer(size), L.BOX_WALL)
        for f in L.FLAPS:
            MESH_DIR[f"{size}_{f}"] = meshes.flap(d, f, *_flap_mesh_args(f, size))
    (out / "world.sdf").write_text(world_sdf(spec, out / "textures"))
    (out / "bridge.yaml").write_text(BRIDGE)
    (out / "spec.yaml").write_text(yaml.safe_dump(spec.to_dict(), allow_unicode=True, sort_keys=False))
    for arm in L.ARMS:
        names = spec.graspable(arm)
        if a.attach == "none":
            names = []
        elif a.attach == "items":
            names = [it.name for it in spec.items]
        (out / f"attach_{arm}.xacro").write_text(attach_xacro(arm, names))
        (out / f"{arm}_controllers.yaml").write_text(controllers_yaml(arm))
    print(f"generated: {len(spec.items)} items, {len(spec.boxes)} boxes -> {out}")
