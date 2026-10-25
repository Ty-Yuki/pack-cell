"""品物・段ボール箱の見た目のメッシュ(OBJ + MTL + テクスチャ)を寸法から生成する。

衝突判定と知覚用の形状は実寸の直方体・円柱のまま(gen_world.py)で、ここで作るのは見た目だけ。
面ごとに材質を分け、商品ラベル(assets/labels/)を UV で貼る。原点は外形の中心、x が品の W(長辺)方向。
正面はカメラ・梱包台の側の -y 面。
透明・半透明の部品は透明度ごとに別の OBJ に分け、透明度は SDF の <transparency> で指定する
(Gazebo は OBJ の材質の d を反映しないため)。build() は [(OBJ のパス, 透明度)] を返す。
"""
import math, pathlib, shutil

LABELS = pathlib.Path(__file__).resolve().parents[1] / "assets" / "labels"


class Mesh:
    def __init__(self):
        self.v, self.vt, self.vn = [], [], []
        self.faces = {}          # 材質 -> [[v, v, v]]
        self.mats = {}           # 材質 -> dict(kd, tex, t, ks, ns)

    def mat(self, name, kd=(0.8, 0.8, 0.8), tex=None, t=0.0, ks=0.1, ns=30):
        """t: 透明度(0 = 不透明)。ks・ns: 光沢の強さと鋭さ。"""
        self.mats[name] = {"kd": kd, "tex": tex, "t": t, "ks": ks, "ns": ns}
        self.faces.setdefault(name, [])
        return name

    def _v(self, p, uv, n):
        self.v.append(tuple(p)); self.vt.append(tuple(uv)); self.vn.append(tuple(n))
        return len(self.v)

    def tri(self, m, ps, uvs, ns):
        self.faces.setdefault(m, []).append([self._v(p, uv, n) for p, uv, n in zip(ps, uvs, ns)])

    def quad(self, m, ps, uvs, n=None):
        """4点(外から見て反時計回り)。n を省略すると面の法線。"""
        if n is None:
            n = _normal(ps[0], ps[1], ps[2])
        ns = n if isinstance(n[0], (list, tuple)) else [n] * 4
        self.tri(m, [ps[0], ps[1], ps[2]], [uvs[0], uvs[1], uvs[2]], [ns[0], ns[1], ns[2]])
        self.tri(m, [ps[0], ps[2], ps[3]], [uvs[0], uvs[2], uvs[3]], [ns[0], ns[2], ns[3]])

    def append(self, other, fp, fn, flip=False, mat_map=None):
        """other の三角形を座標変換 fp(点)・fn(法線)して取り込む。flip: 鏡映を含む変換なら True(面の向きを保つ)。"""
        mat_map = mat_map or {}
        for name, fs in other.faces.items():
            name2 = mat_map.get(name, name)
            if name2 not in self.mats:
                self.mats[name2] = dict(other.mats[name])
            for tri in fs:
                idx = tri[::-1] if flip else tri
                self.faces.setdefault(name2, []).append(
                    [self._v(fp(other.v[k - 1]), other.vt[k - 1], fn(other.vn[k - 1])) for k in idx])

    def save(self, out, name):
        out.mkdir(parents=True, exist_ok=True)
        groups = {}
        for m, fs in self.faces.items():
            if fs:
                groups.setdefault(round(self.mats[m]["t"], 2), []).append(m)
        res = []
        for t, mats in sorted(groups.items()):
            stem = name if t == 0 else f"{name}_t{int(t * 100)}"
            with open(out / f"{stem}.mtl", "w") as f:
                for m in mats:
                    a = self.mats[m]
                    f.write(f"newmtl {m}\nKa 0.25 0.25 0.25\nKd {a['kd'][0]} {a['kd'][1]} {a['kd'][2]}\n"
                            f"Ks {a['ks']} {a['ks']} {a['ks']}\nNs {a['ns']}\nd 1.0\nillum 2\n")
                    if a["tex"]:
                        shutil.copy(LABELS / a["tex"], out / a["tex"])
                        f.write(f"map_Kd {a['tex']}\n")
                    f.write("\n")
            used = sorted({k for m in mats for tri in self.faces[m] for k in tri})
            remap = {k: i + 1 for i, k in enumerate(used)}
            with open(out / f"{stem}.obj", "w") as f:
                f.write(f"mtllib {stem}.mtl\n")
                for k in used:
                    p = self.v[k - 1]
                    f.write(f"v {p[0]:.5f} {p[1]:.5f} {p[2]:.5f}\n")
                for k in used:
                    f.write("vt {:.5f} {:.5f}\n".format(*self.vt[k - 1]))
                for k in used:
                    f.write("vn {:.4f} {:.4f} {:.4f}\n".format(*self.vn[k - 1]))
                for m in mats:
                    f.write(f"usemtl {m}\n")
                    for tri in self.faces[m]:
                        a, b, c = (remap[k] for k in tri)
                        f.write(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}\n")
            res.append((out / f"{stem}.obj", t))
        return res


def _normal(a, b, c):
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    return _unit((u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]))


def _unit(v):
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / s for x in v)


ALL6 = ("+x", "-x", "+y", "-y", "+z", "-z")


# ---- 部品 ----
def box(m, mats, c, s, uvs=None):
    """直方体。mats: 面(+x,-x,+y,-y,+z,-z) -> 材質。テクスチャは各面全体に貼る(画像の上 = +z、上面と底面は +y)。
    uvs: 面 -> (u0, v0, u1, v1) で貼る範囲を変えられる。"""
    x, y, z = c
    a, b, h = s[0] / 2, s[1] / 2, s[2] / 2
    P = lambda i, j, k: (x + i * a, y + j * b, z + k * h)
    faces = {
        "+y": [P(1, 1, -1), P(-1, 1, -1), P(-1, 1, 1), P(1, 1, 1)],
        "-y": [P(-1, -1, -1), P(1, -1, -1), P(1, -1, 1), P(-1, -1, 1)],
        "+x": [P(1, -1, -1), P(1, 1, -1), P(1, 1, 1), P(1, -1, 1)],
        "-x": [P(-1, 1, -1), P(-1, -1, -1), P(-1, -1, 1), P(-1, 1, 1)],
        "+z": [P(-1, -1, 1), P(1, -1, 1), P(1, 1, 1), P(-1, 1, 1)],
        "-z": [P(1, 1, -1), P(-1, 1, -1), P(-1, -1, -1), P(1, -1, -1)],
    }
    for k, ps in faces.items():
        if mats.get(k):
            u0, v0, u1, v1 = (uvs or {}).get(k, (0, 0, 1, 1))
            m.quad(mats[k], ps, [(u0, v0), (u1, v0), (u1, v1), (u0, v1)])


def prism(m, profile, z0, z1, side, top=None, bot=None, side_fn=None, smooth=True):
    """断面 profile(xy の閉曲線、反時計回り)を z0〜z1 に押し出す。側面の UV は (周長の位置, z)。
    side_fn(法線, p, q, u0, u1) -> (材質, uv4) または None で、側面の材質と UV を面ごとに決められる。"""
    n = len(profile)
    per = [0.0]
    for i in range(n):
        per.append(per[-1] + math.dist(profile[i], profile[(i + 1) % n]))
    L = per[-1]
    norms = []
    for i in range(n):
        p0, p1 = profile[i - 1], profile[(i + 1) % n]
        norms.append(_unit((p1[1] - p0[1], -(p1[0] - p0[0]), 0.0)))
    for i in range(n):
        j = (i + 1) % n
        p, q = profile[i], profile[j]
        ps = [(p[0], p[1], z0), (q[0], q[1], z0), (q[0], q[1], z1), (p[0], p[1], z1)]
        u0, u1 = per[i] / L, per[i + 1] / L
        mat, uv = side, [(u0, 0), (u1, 0), (u1, 1), (u0, 1)]
        if side_fn:
            r = side_fn(_unit((q[1] - p[1], -(q[0] - p[0]))), p, q, u0, u1)
            if r:
                mat, uv = r
        m.quad(mat, ps, uv, [norms[i], norms[j], norms[j], norms[i]] if smooth else None)
    xs = [p[0] for p in profile]; ys = [p[1] for p in profile]
    cx, cy = sum(xs) / n, sum(ys) / n
    w, h = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    uvf = lambda p: ((p[0] - min(xs)) / w, (p[1] - min(ys)) / h)
    for zz, mm, up in ((z1, top, True), (z0, bot, False)):
        if not mm:
            continue
        for i in range(n):
            a, b = profile[i], profile[(i + 1) % n]
            pts = [(cx, cy), a, b] if up else [(cx, cy), b, a]
            m.tri(mm, [(p[0], p[1], zz) for p in pts], [uvf(p) for p in pts], [(0, 0, 1 if up else -1)] * 3)


def extrude_y(m, profile_xz, y0, y1, side, front=None, back=None, side_fn=None, smooth=True):
    """xz 断面(反時計回り)を y0〜y1 に押し出す。front は -y 側、back は +y 側の面(UV は x, z)。"""
    t = Mesh()
    prism(t, profile_xz, y0, y1, side, top=back, bot=front, side_fn=side_fn, smooth=smooth)
    m.append(t, lambda p: (p[0], p[2], p[1]), lambda n: (n[0], n[2], n[1]), flip=True)


def extrude_x(m, profile_yz, x0, x1, side, side_fn=None, smooth=True, cap0=None, cap1=None):
    """yz 断面(反時計回り)を x0〜x1 に押し出す。側面の UV は (周長の位置, x)。"""
    t = Mesh()
    prism(t, profile_yz, x0, x1, side, top=cap1, bot=cap0, side_fn=side_fn, smooth=smooth)
    m.append(t, lambda p: (p[2], p[0], p[1]), lambda n: (n[2], n[0], n[1]))


def circle(r, n=40, ry=None, c=(0, 0)):
    ry = ry or r
    return [(c[0] + r * math.cos(2 * math.pi * i / n), c[1] + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def superellipse(a, b, e=3.0, n=48):
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        c, s = math.cos(t), math.sin(t)
        out.append((a * math.copysign(abs(c) ** (2 / e), c), b * math.copysign(abs(s) ** (2 / e), s)))
    return out


def rounded_rect(w, d, r, n=6):
    pts = []
    for cx, cy, a0 in ((w / 2 - r, d / 2 - r, 0), (-w / 2 + r, d / 2 - r, 90), (-w / 2 + r, -d / 2 + r, 180), (w / 2 - r, -d / 2 + r, 270)):
        for k in range(n + 1):
            a = math.radians(a0 + 90 * k / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def frustum(m, r0, r1, z0, z1, mat, n=40, uv=False, inward=False):
    """円錐台の側面(z0 で半径 r0 → z1 で半径 r1)。z0 < z1 なら外向き、z0 > z1 なら内向き(すり鉢の内壁)。
    inward で向きを反転する。uv=True なら一周でテクスチャを貼る。"""
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        P = lambda r, a, z: (r * math.cos(a), r * math.sin(a), z)
        ps = [P(r0, a0, z0), P(r0, a1, z0), P(r1, a1, z1), P(r1, a0, z1)]
        uvs = [(i / n, 0), ((i + 1) / n, 0), ((i + 1) / n, 1), (i / n, 1)] if uv else [(0, 0)] * 4
        if inward:
            ps, uvs = ps[::-1], uvs[::-1]
        fn = _normal(ps[0], ps[1], ps[2]) if r0 > 1e-6 else _normal(ps[1], ps[2], ps[3])
        ns = []
        for a in (a0, a1, a1, a0)[::-1 if inward else 1]:
            # 側面のなめらかな法線(放射方向と z 成分)を、面の向きにそろえる
            k = fn[0] * math.cos(a) + fn[1] * math.sin(a)
            ns.append(_unit((k * math.cos(a), k * math.sin(a), fn[2])))
        m.quad(mat, ps, uvs, ns)


def annulus(m, r_in, r_out, z, mat, up=True, n=40, tex_r=None, c=(0, 0)):
    """水平な輪(r_in = 0 なら円盤)。tex_r: その半径の円にテクスチャ全体を合わせる。"""
    uvf = (lambda p: (0.5 + (p[0] - c[0]) / (2 * tex_r), 0.5 + (p[1] - c[1]) / (2 * tex_r))) if tex_r else (lambda p: (0, 0))
    P = lambda r, a: (c[0] + r * math.cos(a), c[1] + r * math.sin(a), z)
    nn = (0, 0, 1 if up else -1)
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        if r_in <= 1e-6:
            pts = [(c[0], c[1], z), P(r_out, a0), P(r_out, a1)]
            if not up:
                pts = pts[::-1]
            m.tri(mat, pts, [uvf(p) for p in pts], [nn] * 3)
        else:
            ps = [P(r_in, a0), P(r_out, a0), P(r_out, a1), P(r_in, a1)]
            if not up:
                ps = ps[::-1]
            m.quad(mat, ps, [uvf(p) for p in ps], nn)


def torus(m, R, r, z, mat, nu=40, nv=8):
    def P(ii, jj):
        u, v = 2 * math.pi * ii / nu, 2 * math.pi * jj / nv
        n = (math.cos(v) * math.cos(u), math.cos(v) * math.sin(u), math.sin(v))
        return ((R + r * math.cos(v)) * math.cos(u), (R + r * math.cos(v)) * math.sin(u), z + r * math.sin(v)), n
    for i in range(nu):
        for j in range(nv):
            q = [P(i, j), P(i + 1, j), P(i + 1, j + 1), P(i, j + 1)]
            m.quad(mat, [p for p, _ in q], [(0, 0)] * 4, [n for _, n in q])


def ellipsoid(m, c, r, mat, nu=18, nv=10, z_min=-1.0, z_max=1.0):
    """楕円体。z_min〜z_max(-1〜1、緯度の sin)で半球なども作れる。"""
    ph0, ph1 = math.asin(z_min), math.asin(z_max)

    def P(ii, jj):
        th, ph = 2 * math.pi * ii / nu, ph0 + (ph1 - ph0) * jj / nv
        n = (math.cos(ph) * math.cos(th), math.cos(ph) * math.sin(th), math.sin(ph))
        return (c[0] + r[0] * n[0], c[1] + r[1] * n[1], c[2] + r[2] * n[2]), _unit((n[0] / r[0], n[1] / r[1], n[2] / r[2]))
    for i in range(nu):
        for j in range(nv):
            q = [P(i, j), P(i + 1, j), P(i + 1, j + 1), P(i, j + 1)]
            m.quad(mat, [p for p, _ in q], [(0, 0)] * 4, [n for _, n in q])


def bag_surface(m, x0, x1, D, h_top, h_bot, top_mat, bot_mat, shape, nx=30, ny=20):
    """袋の上下の曲面(x0〜x1、y は ±D/2、縁で z = 0 に閉じる)。shape(u, v) -> 0〜1 の膨らみ。
    上面・底面とも画像の上が +y になるよう UV を貼る(底面は下から見て裏返らないよう u を反転)。"""
    def P(u, v, sgn, h):
        z = sgn * h * shape(u, v)
        return (x0 + u * (x1 - x0), -D / 2 + v * D, z)
    for sgn, mat, h in ((1, top_mat, h_top), (-1, bot_mat, h_bot)):
        for i in range(nx):
            for j in range(ny):
                us = [(i / nx, j / ny), ((i + 1) / nx, j / ny), ((i + 1) / nx, (j + 1) / ny), (i / nx, (j + 1) / ny)]
                ps = [P(u, v, sgn, h) for u, v in us]
                # なめらかな法線(数値微分)
                ns = []
                for u, v in us:
                    e = 1e-3
                    pu = [a - b for a, b in zip(P(min(u + e, 1), v, sgn, h), P(max(u - e, 0), v, sgn, h))]
                    pv = [a - b for a, b in zip(P(u, min(v + e, 1), sgn, h), P(u, max(v - e, 0), sgn, h))]
                    nrm = _unit((pu[1] * pv[2] - pu[2] * pv[1], pu[2] * pv[0] - pu[0] * pv[2], pu[0] * pv[1] - pu[1] * pv[0]))
                    ns.append(nrm if sgn > 0 else tuple(-x for x in nrm))
                if sgn > 0:
                    m.quad(mat, ps, us, ns)
                else:
                    m.quad(mat, ps[::-1], [(1 - u, v) for u, v in us[::-1]], ns[::-1])


def crimp(m, x0, x1, D, th, mat, teeth=16):
    """袋の端の圧着部(外側の縁がぎざぎざの薄い帯)。x0 が袋の側、x1 が外側。"""
    sgn = 1 if x1 > x0 else -1
    pts = [(x0, -D / 2), (x0, D / 2)]
    for k in range(teeth * 2 + 1):
        y = D / 2 - D * k / (teeth * 2)
        pts.append((x1 if k % 2 == 0 else x1 - sgn * 0.003, y))
    prism(m, pts[::-1] if sgn > 0 else pts, -th / 2, th / 2, mat, top=mat, bot=mat, smooth=False)


# ---- 品目ごとの形 ----
def build(sku, out):
    W, D, H = sku.size
    m = Mesh()
    {"gable": _gable, "bottle": _bottle, "can": _can, "cup": _cup, "pet_lying": _pet, "egg_pack": _egg,
     "bread": _bread, "bag": _bag}.get(sku.shape, _box)(m, sku, W, D, H)
    return m.save(out, sku.id)


def _box(m, sku, W, D, H):
    if sku.id == "tofu":
        return _tofu(m, sku, W, D, H)
    b = m.mat("body", sku.color)
    box(m, {k: b for k in ALL6}, (0, 0, 0), sku.size)


def _gable(m, sku, W, D, H):
    """屋根型の紙パック: 胴(正面・背面に表ラベル、左右に側面ラベル)+ ブランドの色の屋根 + 高めの合わせ目。"""
    sid = sku.id
    fin = 0.016
    roof = D / 2 * 0.9
    body = H - roof - fin
    zb = -H / 2 + body
    front, side = m.mat("front", tex=f"{sid}_front.jpg", ks=0.05), m.mat("side", tex=f"{sid}_side.jpg", ks=0.05)
    paper = m.mat("paper", (0.96, 0.96, 0.95), ks=0.05)
    roofc = m.mat("roof", sku.label_color, ks=0.08)
    box(m, {"+y": front, "-y": front, "+x": side, "-x": side, "-z": paper}, (0, 0, (-H / 2 + zb) / 2), (W, D, body))
    a, b = W / 2, D / 2
    m.quad(roofc, [(a, b, zb), (-a, b, zb), (-a, 0, zb + roof), (a, 0, zb + roof)], [(0, 0)] * 4)
    m.quad(roofc, [(-a, -b, zb), (a, -b, zb), (a, 0, zb + roof), (-a, 0, zb + roof)], [(0, 0)] * 4)
    for sx in (1, -1):                                   # 妻(三角)は少し内側へ折れ込む
        pts = [(sx * a, -b, zb), (sx * a, b, zb), (sx * a * 0.93, 0, zb + roof)]
        if sx < 0:
            pts = pts[::-1]
        m.tri(roofc, pts, [(0, 0)] * 3, [_normal(*pts)] * 3)
    box(m, {k: roofc for k in ALL6}, (0, 0, zb + roof + fin / 2 - 0.001), (W * 1.0, 0.004, fin + 0.002))


def _bottle(m, sku, W, D, H):
    """台所用洗剤: 平たい楕円の半透明ボトル、正面と背面にラベル、注ぎ口付きの開閉キャップ。"""
    sid = sku.id
    shoulder, neck, cap = 0.022, 0.005, 0.024
    zb1 = H / 2 - shoulder - neck - cap
    body = m.mat("body", (0.40, 0.78, 0.42), t=0.25, ks=0.6, ns=90)
    front = m.mat("front", tex=f"{sid}_front.jpg", ks=0.3, ns=60)
    back = m.mat("back", tex=f"{sid}_back.jpg", ks=0.3, ns=60)
    capc = m.mat("cap", (0.96, 0.96, 0.95), ks=0.4, ns=60)
    nozzle = m.mat("nozzle", sku.label_color, ks=0.4, ns=60)
    prof = superellipse(W / 2, D / 2, e=3.0, n=60)
    lab_z0, lab_z1 = -H / 2 + 0.010, zb1 - 0.004
    lab = [(x * 1.006, y * 1.006) for x, y in prof]
    xf = 0.0                                             # ラベルを貼る面(法線が ±y 寄り)の x の範囲
    for i, p in enumerate(lab):
        q = lab[(i + 1) % len(lab)]
        if abs(_unit((q[1] - p[1], -(q[0] - p[0])))[1]) >= 0.6:
            xf = max(xf, abs(p[0]), abs(q[0]))

    def side_fn(n, p, q, u0, u1):
        if abs(n[1]) < 0.6:
            return None
        if n[1] < 0:                                     # 正面(-y): 左が -x
            us = [0.5 + p[0] / (2 * xf), 0.5 + q[0] / (2 * xf)]
            mat = front
        else:                                            # 背面(+y): 左が +x
            us = [0.5 - p[0] / (2 * xf), 0.5 - q[0] / (2 * xf)]
            mat = back
        us = [min(1, max(0, u)) for u in us]
        return mat, [(us[0], 0), (us[1], 0), (us[1], 1), (us[0], 1)]
    prism(m, prof, -H / 2, lab_z0, body, bot=body)
    prism(m, lab, lab_z0, lab_z1, body, side_fn=side_fn)
    prism(m, prof, lab_z1, zb1, body)
    # 肩: 楕円から円へすぼまる
    n = len(prof)
    rn = 0.010
    for i in range(n):
        j = (i + 1) % n
        t0, t1 = 2 * math.pi * i / n, 2 * math.pi * j / n
        ps = [(prof[i][0], prof[i][1], zb1), (prof[j][0], prof[j][1], zb1),
              (rn * math.cos(t1), rn * math.sin(t1), zb1 + shoulder), (rn * math.cos(t0), rn * math.sin(t0), zb1 + shoulder)]
        m.quad(body, ps, [(0, 0)] * 4)
    frustum(m, rn, rn, zb1 + shoulder, zb1 + shoulder + neck, body)
    zc = zb1 + shoulder + neck
    rc = 0.0155
    annulus(m, rn, rc, zc, capc, up=False)
    frustum(m, rc, rc, zc, zc + cap * 0.55, capc)                # キャップの胴
    frustum(m, rc, 0.010, zc + cap * 0.55, zc + cap * 0.8, capc)
    annulus(m, 0.0035, 0.010, zc + cap * 0.8, capc)
    frustum(m, 0.0045, 0.0028, zc + cap * 0.8, H / 2, nozzle)   # 注ぎ口
    annulus(m, 0, 0.0028, H / 2, nozzle, n=16)
    box(m, {k: capc for k in ALL6}, (rc - 0.001, 0, zc + cap * 0.62), (0.006, 0.012, 0.004))   # 開閉のつまみ


def _can(m, sku, W, D, H):
    """サバ缶: 一周の巻きラベル + 銀色のふたと底(巻き締めの縁・一段下がったパネル・同心円の溝・プルタブ)。"""
    sid = sku.id
    lab = m.mat("label", tex=f"{sid}_label.jpg", ks=0.35, ns=60)
    metal = m.mat("metal", (0.66, 0.67, 0.70), ks=1.0, ns=140)
    dark = m.mat("metal_dark", (0.48, 0.49, 0.52), ks=0.9, ns=120)
    r = W / 2
    rim = 0.0035
    frustum(m, r * 0.985, r * 0.985, -H / 2 + rim, H / 2 - rim, lab, uv=True)
    rp = r - rim * 1.5                                   # パネルの半径
    for sgn in (1, -1):
        torus(m, r - rim / 2, rim / 2, sgn * (H / 2 - rim / 2), metal)                 # 巻き締めの縁
        z_top, z_in = sgn * (H / 2 - rim * 0.2), sgn * (H / 2 - rim * 1.3)
        frustum(m, r - rim, rp, z_top, z_in, metal, inward=sgn < 0)                     # 縁から一段下がる内壁
        rings = [(0.0, 0.32, metal), (0.32, 0.38, dark), (0.38, 0.66, metal), (0.66, 0.72, dark), (0.72, 1.0, metal)]
        for a0, a1, mt in rings:
            annulus(m, rp * a0, rp * a1, z_in, mt, up=sgn > 0)
    zt = H / 2 - rim * 1.3
    # プルタブ: 輪と、中心のリベットへ伸びる板
    torus(m, 0.0065, 0.0011, zt + 0.0012, metal, nu=24, nv=6)
    box(m, {"+z": metal, "+x": metal, "-x": metal, "+y": metal, "-y": metal}, (-0.008, 0, zt + 0.0008), (0.010, 0.006, 0.0012))
    annulus(m, 0, 0.0022, zt + 0.0015, dark, n=16)
    # 開け口の刻み線(パネル上の円弧の暗い線)
    annulus(m, rp * 0.88, rp * 0.91, zt + 0.0002, dark)


def _cup(m, sku, W, D, H):
    """アイスのカップ: すぼまった胴に一周のラベル、縁にかぶさるふた(上面がわずかに膨らむ)。"""
    sid = sku.id
    lab = m.mat("label", tex=f"{sid}_side.jpg", ks=0.25, ns=50)
    lid = m.mat("lid", tex=f"{sid}_lid.jpg", ks=0.25, ns=50)
    rim = m.mat("rim", sku.label_color, ks=0.4, ns=60)
    bottom = m.mat("bottom", (0.95, 0.95, 0.93))
    r = W / 2
    lip = 0.007
    frustum(m, r * 0.84, r * 0.955, -H / 2, H / 2 - lip, lab, uv=True)
    annulus(m, 0, r * 0.84, -H / 2, bottom, up=False)
    annulus(m, r * 0.955, r, H / 2 - lip, rim, up=False)          # ふたの縁の裏
    frustum(m, r, r, H / 2 - lip, H / 2 - 0.002, rim)              # ふたの縁
    frustum(m, r, r * 0.975, H / 2 - 0.002, H / 2 - 0.0012, rim)
    rr, dome = r * 0.975, 0.0012
    n, nr = 48, 6
    for k in range(nr):                                            # ふたの上面(中心がわずかに高い)
        ra, rb = rr * k / nr, rr * (k + 1) / nr
        za = H / 2 - 0.0012 + dome * (1 - (ra / rr) ** 2) + 0.0012
        zb = H / 2 - 0.0012 + dome * (1 - (rb / rr) ** 2) + 0.0012 * (k + 1 < nr)
        for i in range(n):
            a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
            P = lambda q, a, z: (q * math.cos(a), q * math.sin(a), z)
            uvf = lambda p: (0.5 + p[0] / (2 * rr), 0.5 + p[1] / (2 * rr))
            if k == 0:
                pts = [(0, 0, za), P(rb, a0, zb), P(rb, a1, zb)]
                m.tri(lid, pts, [uvf(p) for p in pts], [(0, 0, 1)] * 3)
            else:
                ps = [P(ra, a0, za), P(rb, a0, zb), P(rb, a1, zb), P(ra, a1, za)]
                m.quad(lid, ps, [uvf(p) for p in ps], (0, 0, 1))


def _pet(m, sku, W, D, H):
    """横置きの角型ペット(2L): 透明なボトルに水、胴の横溝、中央に一周のラベル、肩から口へすぼまる、キャップ。+x 側が口。"""
    sid = sku.id
    clear = m.mat("bottle", (0.85, 0.92, 0.97), t=0.6, ks=0.9, ns=120)
    water = m.mat("water", (0.55, 0.76, 0.92), t=0.75, ks=0.6, ns=80)
    lab = m.mat("label", tex=f"{sid}_label.jpg", ks=0.3, ns=60)
    capc = m.mat("cap", (0.15, 0.38, 0.82), ks=0.4, ns=60)
    body = W * 0.78
    x0 = -W / 2
    rc = min(D, H) * 0.2
    prof = rounded_rect(D, H, rc)
    groove = rounded_rect(D * 0.95, H * 0.95, rc * 0.95)
    segs = [(0.00, 0.05, prof), (0.05, 0.08, groove), (0.08, 0.13, prof), (0.13, 0.16, groove), (0.16, 0.21, prof),
            (0.21, 0.79, None), (0.79, 0.84, prof), (0.84, 0.87, groove), (0.87, 0.92, prof), (0.92, 0.95, groove), (0.95, 1.00, prof)]
    for a, b, pr in segs:
        xa, xb = x0 + a * body, x0 + b * body
        if pr is None:
            extrude_x(m, [(y * 1.006, z * 1.006) for y, z in prof], xa, xb, lab)
        else:
            extrude_x(m, pr, xa, xb, clear, cap0=clear if a == 0 else None)
            if pr is groove:                                         # 溝の段差
                for xx, sg in ((xa, 1), (xb, -1)):
                    ring = Mesh()
                    for i in range(len(prof)):
                        j = (i + 1) % len(prof)
                        ps = [(groove[i][0], groove[i][1]), (groove[j][0], groove[j][1]), (prof[j][0], prof[j][1]), (prof[i][0], prof[i][1])]
                        ring.quad("c", [(p[0], p[1], 0) for p in ps] if sg < 0 else [(p[0], p[1], 0) for p in ps[::-1]], [(0, 0)] * 4)
                    m.append(ring, lambda p, xx=xx: (xx + p[2], p[0], p[1]), lambda n: (n[2], n[0], n[1]), mat_map={"c": clear})
    extrude_x(m, rounded_rect(D * 0.9, H * 0.9, rc * 0.9), x0 + 0.005, x0 + body - 0.004, water, cap0=water, cap1=water)
    # 肩(角から丸へすぼまる)・口・キャップ
    sh = Mesh()
    n = len(prof)
    rn, L = 0.0135, W * 0.13
    for i in range(n):
        j = (i + 1) % n
        t0, t1 = math.atan2(prof[i][1], prof[i][0]), math.atan2(prof[j][1], prof[j][0])
        if t1 < t0 - math.pi:
            t1 += 2 * math.pi
        ps = [(prof[i][0], prof[i][1], 0), (prof[j][0], prof[j][1], 0),
              (rn * math.cos(t1), rn * math.sin(t1), L), (rn * math.cos(t0), rn * math.sin(t0), L)]
        sh.quad("c", ps, [(0, 0)] * 4)
    frustum(sh, rn, rn, L, L + W * 0.025, "c")
    torus(sh, rn + 0.001, 0.0015, L + W * 0.025, "c", nu=24, nv=6)   # 口のつば
    frustum(sh, 0.0165, 0.0165, L + W * 0.03, W * 0.22, "k")
    annulus(sh, 0, 0.0165, W * 0.22, "k")
    annulus(sh, rn, 0.0165, L + W * 0.03, "k", up=False)
    m.append(sh, lambda p: (x0 + body + p[2], p[0], p[1]), lambda n: (n[2], n[0], n[1]), mat_map={"c": clear, "k": capc})


def _egg(m, sku, W, D, H):
    """卵10個パック: 卵ごとに膨らんだ透明のふた(上)とくぼみ(下)、中ほどの平たいつば、上面を横切る帯ラベル。"""
    sid = sku.id
    clear = m.mat("pack", (0.92, 0.96, 0.96), t=0.6, ks=0.9, ns=120)
    egg = m.mat("egg", (0.93, 0.80, 0.62), ks=0.25)
    band = m.mat("band", tex=f"{sid}_band.jpg", ks=0.05)
    re, rz = 0.0205, min(0.028, (H - 0.008) / 2)            # 卵の横の半径・縦の半径
    ze = -H / 2 + 0.004 + rz                                 # 卵の中心(=つばの高さ)
    rc = min(W / 10.6, D / 4.3)                              # くぼみの半径
    cells = [((i - 2) * W / 5.25, (j - 0.5) * D / 2.05) for i in range(5) for j in range(2)]
    box(m, {k: clear for k in ALL6}, (0, 0, ze), (W, D, 0.0015))
    for cx, cy in cells:
        ellipsoid(m, (cx, cy, ze), (rc, rc, ze + H / 2), clear, z_min=-1.0, z_max=-0.02)
        ellipsoid(m, (cx, cy, ze), (rc, rc, H / 2 - ze - 0.001), clear, z_min=0.02, z_max=1.0)
        ellipsoid(m, (cx, cy, ze), (re, re, rz), egg)
    box(m, {"+z": band}, (0, 0, H / 2 - 0.0004), (W * 0.97, D * 0.40, 0.0008))


def _tofu(m, sku, W, D, H):
    """豆腐: 半透明の容器(上の縁につば)から白い豆腐が見え、上面に印刷フィルム。"""
    film = m.mat("film", tex="tofu_top.jpg", ks=0.35, ns=60)
    tray = m.mat("tray", (0.93, 0.95, 0.96), t=0.5, ks=0.6, ns=80)
    tofu = m.mat("tofu", (0.97, 0.96, 0.90), ks=0.15)
    flange, top = 0.005, H / 2 - 0.0015
    prism(m, rounded_rect(W - 2 * flange, D - 2 * flange, 0.010), -H / 2, top - 0.001, tray, bot=tray)
    prism(m, rounded_rect(W, D, 0.012), top - 0.001, top, tray, bot=tray)                    # つば
    prism(m, rounded_rect(W, D, 0.012), top, H / 2, film, top=film)                         # フィルム
    box(m, {k: tofu for k in ALL6}, (0, 0, -H / 2 + 0.001 + (H - 0.005) / 2), (W - 2 * flange - 0.004, D - 2 * flange - 0.004, H - 0.005))


def _bread(m, sku, W, D, H):
    """6枚切りの食パン(角食): 角の丸い直方体で上がわずかに盛り上がる。半透明の袋に入り、+x の端を絞って留め具で閉じる。
    正面(-y)は袋の印刷、ほかの面はクラストの色が袋越しに見える。"""
    face = m.mat("bag_print", tex="bread_bag.jpg", ks=0.35, ns=60)
    crust_top = m.mat("crust_top", (0.72, 0.45, 0.22), ks=0.1)
    crust = m.mat("crust", (0.86, 0.66, 0.42), ks=0.1)
    bag = m.mat("bag", (0.97, 0.97, 0.97), t=0.65, ks=0.8, ns=100)
    clip = m.mat("clip", (0.20, 0.45, 0.85), ks=0.3)
    tail = 0.013
    w, h = W - tail - 0.003, H - 0.003
    rb, rt, rise = 0.008, 0.022, 0.012                   # 下の角・上の角の丸み、上辺の盛り上がり
    cx = -tail / 2
    prof = []
    for k in range(5):                                   # 右下
        a = -math.pi / 2 + math.pi / 2 * k / 4
        prof.append((w / 2 - rb + rb * math.cos(a), -h / 2 + rb + rb * math.sin(a)))
    for k in range(7):                                   # 右上
        a = math.pi / 2 * k / 6
        prof.append((w / 2 - rt + rt * math.cos(a), h / 2 - rise - rt + rt * math.sin(a)))
    for k in range(1, 12):                               # 上辺(ゆるい弧)
        u = k / 12
        prof.append(((w / 2 - rt) * (1 - 2 * u), h / 2 - rise + rise * math.sin(math.pi * u)))
    for k in range(7):                                   # 左上
        a = math.pi / 2 + math.pi / 2 * k / 6
        prof.append((-w / 2 + rt + rt * math.cos(a), h / 2 - rise - rt + rt * math.sin(a)))
    for k in range(5):                                   # 左下
        a = math.pi + math.pi / 2 * k / 4
        prof.append((-w / 2 + rb + rb * math.cos(a), -h / 2 + rb + rb * math.sin(a)))
    prof = [(x + cx, z - 0.0015) for x, z in prof]

    def side_fn(n, p, q, u0, u1):
        return (crust_top if n[1] > 0.5 else crust), [(0, 0)] * 4
    extrude_y(m, prof, -D / 2 + 0.002, D / 2 - 0.002, crust, front=face, back=crust, side_fn=side_fn)
    big = [(cx + (x - cx) * 1.012, z * 1.012) for x, z in prof]
    extrude_y(m, big, -D / 2 + 0.001, D / 2 - 0.0005, bag, front=bag, back=bag)
    # 絞った端(+x): 袋が細くまとまり、留め具で閉じる
    xr = max(x for x, _ in big)
    sh = Mesh()
    frustum(sh, 0.035, 0.005, 0, tail * 0.55, "b", n=24)
    frustum(sh, 0.005, 0.011, tail * 0.55, tail - 0.0005, "b", n=24)
    annulus(sh, 0, 0.011, tail - 0.0005, "b", n=24)
    m.append(sh, lambda p: (xr - 0.001 + p[2], p[0], p[1] * 0.8 - 0.004), lambda n: _unit((n[2], n[0], n[1] / 0.8)), mat_map={"b": bag})
    box(m, {k: clip for k in ALL6}, (xr - 0.001 + tail * 0.55, 0, -0.004), (0.003, 0.020, 0.016))


def _bag(m, sku, W, D, H):
    """袋もの。米: 厚みのあるクッション状で上は平ら、端は細いシールで取っ手の穴、裏面も印刷。
    ポテチ: 大きく膨らんだ光沢の袋、端はぎざぎざの圧着。"""
    sid = sku.id
    if sid == "rice":
        top = m.mat("top", tex="rice_front.jpg", ks=0.15, ns=40)
        bot = m.mat("bottom", tex="rice_back.jpg", ks=0.15, ns=40)
        seal = m.mat("seal", sku.color, ks=0.15)
        hole = m.mat("hole", (0.20, 0.18, 0.16))
        sw = 0.016
        x0, x1 = -W / 2 + sw, W / 2 - sw

        def shape(u, v):                                  # 中央は平らで縁が丸い
            e = min(u * (x1 - x0), (1 - u) * (x1 - x0), v * D, (1 - v) * D) / 0.035
            return math.sin(math.pi / 2 * min(1.0, e)) ** 0.8
        bag_surface(m, x0, x1, D, H / 2, H / 2 * 0.85, top, bot, shape)
        for sx in (1, -1):
            box(m, {k: seal for k in ALL6}, (sx * (W / 2 - sw / 2 - 0.0005), 0, 0), (sw + 0.001, D * 0.98, 0.003))
        hp = circle(0.0035, 16, ry=0.012, c=(W / 2 - sw / 2, 0))
        prism(m, hp, -0.0017, 0.0017, hole, top=hole, bot=hole)           # 取っ手の穴(+x のシール)
    else:
        top = m.mat("top", tex=f"{sid}_front.jpg", ks=0.75, ns=90)
        bot = m.mat("bottom", sku.color, ks=0.75, ns=90)
        seal = m.mat("seal", tuple(c * 0.9 for c in sku.color), ks=0.8, ns=90)
        sw = 0.016
        x0, x1 = -W / 2 + sw, W / 2 - sw

        def shape(u, v):                                  # 空気で膨らんだ枕形
            return (max(0.0, 1 - abs(2 * u - 1) ** 2.5) ** 0.55) * (max(0.0, 1 - abs(2 * v - 1) ** 3) ** 0.45)
        bag_surface(m, x0 - 0.001, x1 + 0.001, D * 0.97, H / 2, H / 2 * 0.95, top, bot, shape)
        crimp(m, x1, W / 2, D * 0.97, 0.002, seal)
        crimp(m, x0, -W / 2, D * 0.97, 0.002, seal)


# ---- 段ボール箱・蓋 ----
def carton(out, size, wall):
    """上が開いた段ボール箱の本体: 長い側面に印刷入りのクラフト、短い側面と内側は無地。フラップは別リンク(flap())。"""
    m = Mesh()
    ox, oy, oz = size
    side = m.mat("side", tex="carton_side.jpg", ks=0.03)
    plain = m.mat("plain", tex="carton_plain.jpg", ks=0.03)
    inner = m.mat("inner", (0.70, 0.55, 0.38), ks=0.02)
    edge = m.mat("edge", (0.62, 0.48, 0.33), ks=0.02)
    sv = oy / ox                                         # 短い側面の幅の比(無地を正方形で貼る)
    box(m, {"+y": side, "-y": side, "+x": plain, "-x": plain, "-z": plain}, (0, 0, 0), (ox, oy, oz),
        uvs={"+x": (0, 0, sv, oz / ox), "-x": (0, 0, sv, oz / ox), "-z": (0, 0, 1, sv)})
    # 内側(壁の内面と底): 内向きの面
    ix, iy = ox / 2 - wall, oy / 2 - wall
    zf, zt = -oz / 2 + wall, oz / 2
    m.quad(inner, [(-ix, -iy, zf), (ix, -iy, zf), (ix, iy, zf), (-ix, iy, zf)], [(0, 0)] * 4)
    m.quad(inner, [(ix, iy, zf), (-ix, iy, zf), (-ix, iy, zt), (ix, iy, zt)][::-1], [(0, 0)] * 4)
    m.quad(inner, [(-ix, -iy, zf), (ix, -iy, zf), (ix, -iy, zt), (-ix, -iy, zt)][::-1], [(0, 0)] * 4)
    m.quad(inner, [(ix, -iy, zf), (ix, iy, zf), (ix, iy, zt), (ix, -iy, zt)][::-1], [(0, 0)] * 4)
    m.quad(inner, [(-ix, iy, zf), (-ix, -iy, zf), (-ix, -iy, zt), (-ix, iy, zt)][::-1], [(0, 0)] * 4)
    # 壁の上の切り口
    for c, s in (((0, oy / 2 - wall / 2, zt), (ox, wall, 0.001)), ((0, -oy / 2 + wall / 2, zt), (ox, wall, 0.001)),
                 ((ox / 2 - wall / 2, 0, zt), (wall, oy, 0.001)), ((-ox / 2 + wall / 2, 0, zt), (wall, oy, 0.001))):
        box(m, {"+z": edge}, c, s)
    return m.save(out, "carton")


def flap(out, name, c, s):
    """フラップ1枚(ヒンジ原点の座標系で中心 c・寸法 s の板)。両面とも無地のクラフト、切り口は濃い色。"""
    m = Mesh()
    plain = m.mat("plain", tex="carton_plain.jpg", ks=0.03)
    edge = m.mat("edge", (0.62, 0.48, 0.33), ks=0.02)
    u = (s[0] / 0.44, s[1] / 0.44)
    box(m, {"+z": plain, "-z": plain, "+x": edge, "-x": edge, "+y": edge, "-y": edge}, c, s,
        uvs={"+z": (0, 0, u[0], u[1]), "-z": (0.3, 0.3, 0.3 + u[0], 0.3 + u[1])})
    return m.save(out, name)


def visuals(parts, prefix="v"):
    """build() などの結果 [(OBJ, 透明度)] を SDF の <visual> 群にする。"""
    out = []
    for k, (obj, t) in enumerate(parts):
        tr = f"<transparency>{t}</transparency>" if t else ""
        out.append(f'<visual name="{prefix}{k}"><geometry><mesh><uri>{obj}</uri></mesh></geometry>{tr}</visual>')
    return "".join(out)
