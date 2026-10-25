"""出荷レーンの制御: コンベヤ(区間ごとのベルト)・製函機(空箱を出す)・封函機(フラップを閉じる)。

箱は物理のベルトで運ぶ(TrackController。加減速を抑えているので中身はずれない)。止めたい位置の手前で
ベルトを止め、減速の距離で目標に着く。空箱は梱包位置で位置決め(ストッパーと幅寄せの代わりに姿勢を合わせる)する。
アームの実行とは別のスレッドで動き、空箱が梱包位置に着いたら on_ready を呼ぶ。
箱は3サイズ。注文ごとに計画器が選んだサイズを request() で頼むと、製函機がそのサイズの箱を作って待機位置へ送る。
"""
import math, threading, time

from gz.msgs10.double_pb2 import Double

from pack_cell import layout as L, world_spec as WS


def _d(v):
    m = Double()
    m.data = float(v)
    return m


class Line:
    POLL = 0.02

    def __init__(self, world, emit=None):
        self.world, self.gz = world, world.gz
        self.emit = emit or (lambda kind, **kw: None)
        node = self.gz._node
        self.belt_pub = {b: node.advertise(f"/model/belt_{b}/link/link/track_cmd_vel", Double) for b in L.BELTS}
        self.flap_pub = {(c, f): node.advertise(f"/model/{c}/joint/{f}_j/0/cmd_pos", Double)
                         for c in world.boxes for f in L.FLAPS}
        self.lock = threading.RLock()
        self.used = set()                       # 製函機が出した箱
        self.requests = []                      # 製函を頼まれたサイズ(注文の順)
        self.waiting = None                     # 待機位置の箱
        self.at_pack = None                     # 梱包位置の箱
        self.feeding = False                    # 空箱を梱包位置へ運んでいる途中
        self.outfeed = threading.Lock()         # 出口コンベヤ(封函機・出荷)を使えるのは1箱ずつ
        self.threads = []
        time.sleep(0.5)                         # advertise の伝播待ち

    # ---- 低水準 ----
    def belts(self, names, v):
        for _ in range(2):
            for b in names:
                self.belt_pub[b].publish(_d(v))
            time.sleep(0.03)

    def x(self, box):
        return self.gz.pose(box)[0][0]

    def flaps(self, box, names, closed):
        for _ in range(2):
            for f in names:
                self.flap_pub[(box, f)].publish(_d(0.0 if closed else L.FLAPS[f] * L.FLAP_OPEN))
            time.sleep(0.03)

    def _run_until(self, belts, box, target, stop_also=()):
        """belts を動かし、box が target の手前(減速の距離)に来たら belts と stop_also を止める。"""
        stop_d = L.BELT_SPEED ** 2 / (2 * L.BELT_ACC) + L.BELT_SPEED * 0.05
        self.belts(belts, L.BELT_SPEED)
        t0 = time.time()
        while self.x(box) < target - stop_d:
            if time.time() - t0 > 60:
                raise RuntimeError(f"{box} が {target:.2f} に着かない(x={self.x(box):.2f})")
            time.sleep(self.POLL)
        self.belts(list(belts) + list(stop_also), 0.0)
        self.gz.sim_sleep(L.BELT_SPEED / L.BELT_ACC + 0.3)

    # ---- 工程 ----
    def request(self, size):
        """次の注文の箱のサイズを頼む。待機位置が空いたら製函機が作って送る(注文の順)。"""
        with self.lock:
            self.requests.append(size)
            start = len(self.requests) == 1 and self.waiting is None and not getattr(self, "erecting", False)
        if start:
            self.start_async(self.erect)

    def erect(self):
        """製函機が頼まれたサイズの空箱を出し、入口コンベヤで待機位置まで運ぶ。"""
        with self.lock:
            if not self.requests or self.waiting is not None or getattr(self, "erecting", False):
                return None
            size = self.requests.pop(0)
            box = next((b for b in self.world.boxes if self.world.box_size[b] == size and b not in self.used), None)
            if box is None:
                self.emit("line", step="erect", box=None, info=f"{size} の空箱の在庫なし")
                return None
            self.used.add(box)
            self.erecting = True
        try:
            self.gz.set_pose(box, WS.box_pose(L.ERECTOR_X, size)[:3], 0.0)
            self.gz.sim_sleep(0.5)
            self.emit("line", step="erect", box=box, size=size)
            self._run_until(["infeed"], box, L.WAIT_X)
            with self.lock:
                self.waiting = box
        finally:
            self.erecting = False
        return box

    def feed(self, on_ready=None, ship=None):
        """待機位置の空箱を梱包位置へ。ship: 梱包位置にある詰め終わった箱(同時に送り出し、封函機で止める)。"""
        t0 = time.time()
        while self.waiting is None:                 # 製函機がまだ作っている(注文の解釈・計画の直後など)
            if time.time() - t0 > 120:
                raise RuntimeError("待機位置に空箱が来ない")
            time.sleep(0.1)
        box = self.waiting
        self.emit("line", step="feed", box=box, ship=ship)
        belts = ["infeed", "pack"] + (["outfeed"] if ship else [])
        self.belts(belts, L.BELT_SPEED)
        stop_d = L.BELT_SPEED ** 2 / (2 * L.BELT_ACC) + L.BELT_SPEED * 0.05
        t0, fed, sealed_at = time.time(), False, False
        while not fed or (ship and not sealed_at):
            if time.time() - t0 > 60:
                raise RuntimeError("コンベヤの搬送が終わらない")
            if not fed and self.x(box) >= L.PACK_X - stop_d:
                self.belts(["infeed", "pack"], 0.0)
                fed = True
            if ship and not sealed_at and self.x(ship) >= L.SEAL_X - stop_d:
                self.belts(["outfeed"], 0.0)
                sealed_at = True
            time.sleep(self.POLL)
        self.gz.sim_sleep(L.BELT_SPEED / L.BELT_ACC + 0.3)
        # 位置決め: 空箱を梱包位置にぴったり合わせる
        self.gz.set_pose(box, WS.box_pose(L.PACK_X, self.world.box_size[box])[:3], 0.0)
        self.gz.sim_sleep(0.3)
        with self.lock:
            self.waiting, self.at_pack, self.feeding = None, box, False
        self.emit("line", step="ready", box=box)
        if on_ready:
            on_ready(box)
        return box

    def seal_and_ship(self, box, on_shipped=None):
        """封函機の位置の箱: 短いフラップ → 長いフラップ(テープ面が上になる)の順に閉じ、出荷口まで流す。"""
        self.emit("line", step="seal", box=box)
        self.flaps(box, ["fx0", "fx1"], closed=True)
        self.gz.sim_sleep(2.2)
        self.flaps(box, ["fy0", "fy1"], closed=True)
        self.gz.sim_sleep(2.4)
        self.emit("line", step="sealed", box=box)
        self._run_until(["outfeed"], box, L.EXIT_X)
        self.emit("line", step="shipped", box=box)
        if on_shipped:
            on_shipped(box)

    # ---- まとめ ----
    def start_cycle(self, on_ready=None, on_shipped=None):
        """裏で cycle() を始める(すでに空箱を運んでいる途中なら何もしない)。"""
        with self.lock:
            if self.feeding:
                return None
            self.feeding = True
        return self.start_async(self.cycle, on_ready=on_ready, on_shipped=on_shipped)

    def start_async(self, fn, *args, **kw):
        t = threading.Thread(target=self._guard, args=(fn, args, kw), daemon=True)
        t.start()
        self.threads.append(t)
        return t

    def _guard(self, fn, args, kw):
        try:
            fn(*args, **kw)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.emit("error", arm=None, message=f"ライン: {e}")

    def cycle(self, on_ready=None, on_shipped=None):
        """1サイクル: (梱包位置の箱を送り出しつつ)次の空箱を梱包位置へ → 次の空箱を製函 → 送り出した箱を封函・出荷。"""
        with self.lock:
            ship, self.at_pack = self.at_pack, None
            self.feeding = True
        if ship:
            self.outfeed.acquire()                  # 前の箱の出荷が終わるまで、次の箱を出口コンベヤに送らない
        try:
            try:
                self.feed(on_ready=on_ready, ship=ship)
            finally:
                self.feeding = False
            threads = [self.start_async(self.erect)]      # 次の注文のサイズが頼まれていれば作る
            if ship:
                self.seal_and_ship(ship, on_shipped=on_shipped)
            for t in threads:
                t.join()
        finally:
            if ship:
                self.outfeed.release()

    def finish(self, on_shipped=None):
        """最後の箱: 梱包位置から封函機へ送り、封函・出荷する(次の空箱は入れない)。"""
        box = self.at_pack
        if box is None:
            return
        with self.lock:
            self.at_pack = None
        with self.outfeed:
            self.emit("line", step="feed", box=None, ship=box)
            self._run_until(["pack", "outfeed"], box, L.SEAL_X)
            self.seal_and_ship(box, on_shipped=on_shipped)

    def join(self):
        for t in self.threads:
            t.join()
