"""UR5e の順/逆運動学(numpy のみ)。MoveIt を使わず、固定位置間の動作に必要な最小限を自前実装する。

座標系: base_link (URDF) は DH の base を z軸まわりに π 回した系。ここでは base_link 系で目標を受ける。
"""
import numpy as np

# UR5e 公称 DH
A = np.array([0, -0.425, -0.3922, 0, 0, 0])
D = np.array([0.1625, 0, 0, 0.1333, 0.0997, 0.0996])
ALPHA = np.array([np.pi / 2, 0, 0, np.pi / 2, -np.pi / 2, 0])
HOME = np.array([0.0, -np.pi / 2, np.pi / 2, -np.pi / 2, -np.pi / 2, 0.0])

RZ_PI = np.diag([-1.0, -1.0, 1.0, 1.0])  # base_link <- DH base
TOOL_DOWN = np.diag([1.0, -1.0, -1.0])    # 吸着面を真下に向ける姿勢(x前方, z下)


def _dh(theta, d, a, alpha):
    ct, st, ca, sa = np.cos(theta), np.sin(theta), np.cos(alpha), np.sin(alpha)
    return np.array([[ct, -st * ca, st * sa, a * ct],
                     [st, ct * ca, -ct * sa, a * st],
                     [0, sa, ca, d],
                     [0, 0, 0, 1]])


def fk(q):
    """関節角6 -> base_link 系での tool0 の 4x4 同次変換。"""
    T = np.eye(4)
    for i in range(6):
        T = T @ _dh(q[i], D[i], A[i], ALPHA[i])
    return RZ_PI @ T


def _rot_err(Rt, Rc):
    # 回転誤差ベクトル (Rt Rc^T の軸角)
    Re = Rt @ Rc.T
    v = 0.5 * np.array([Re[2, 1] - Re[1, 2], Re[0, 2] - Re[2, 0], Re[1, 0] - Re[0, 1]])
    return v


def _jac(q, eps=1e-6):
    T0 = fk(q)
    J = np.zeros((6, 6))
    for i in range(6):
        dq = q.copy(); dq[i] += eps
        T1 = fk(dq)
        J[:3, i] = (T1[:3, 3] - T0[:3, 3]) / eps
        J[3:, i] = _rot_err(T1[:3, :3], T0[:3, :3]) / eps
    return J


def ik(pos, R, seed, iters=200, tol=1e-5, damping=0.05):
    """減衰最小二乗(DLS)。seed に近い解を返す。収束しなければ None。"""
    q = np.array(seed, dtype=float)
    for _ in range(iters):
        T = fk(q)
        e = np.concatenate([np.asarray(pos) - T[:3, 3], _rot_err(R, T[:3, :3])])
        if np.linalg.norm(e) < tol:
            return q
        J = _jac(q)
        q = q + J.T @ np.linalg.solve(J @ J.T + damping ** 2 * np.eye(6), e)
    T = fk(q)
    e = np.concatenate([np.asarray(pos) - T[:3, 3], _rot_err(R, T[:3, :3])])
    return q if np.linalg.norm(e) < 1e-3 else None
