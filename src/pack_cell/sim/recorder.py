"""gz-transport のカメラ画像を ffmpeg にパイプして mp4 に録画する。

動画の時間軸はシミュレーション時間(カメラ画像のヘッダ時刻)。実フレームレートが揺れても、
一定 fps になるようにフレームを複製・間引くので、ログの sim 時刻から字幕の位置を計算できる。

重い書き込み(ffmpeg へのパイプ)は専用スレッドで行う。gz-transport のコールバックを塞ぐと、
同じプロセスのポーズ・時計の受信まで遅れ、制御側が古い観測で動いてしまうため。
"""
import queue, subprocess, threading
from gz.transport13 import Node
from gz.msgs10.image_pb2 import Image


class Recorder:
    def __init__(self, topic="/cam_overview/image", path="/ws/logs/video.mp4", fps=30):
        self.topic, self.path, self.fps = topic, path, fps
        self._node = Node()
        self._q = queue.Queue(maxsize=120)
        self._ff, self._thread = None, None
        self._running = False
        self.t0 = None          # 動画 0 秒に対応する sim 時刻
        self._k = 0             # 書き出し済みの tick 数
        self.frames, self.dropped = 0, 0

    def _on_img(self, msg):
        # コールバックは最小限: キューに積むだけ(満杯なら間引く)
        if not self._running:
            return
        try:
            self._q.put_nowait((msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9, msg.width, msg.height, bytes(msg.data)))
        except queue.Full:
            self.dropped += 1

    def _writer(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            st, w, h, data = item
            if self._ff is None:
                self.t0 = st
                self._ff = subprocess.Popen(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                     "-s", f"{w}x{h}", "-r", str(self.fps), "-i", "-",
                     "-an", "-c:v", "libx264", "-preset", "veryfast", "-threads", "4", "-pix_fmt", "yuv420p", "-crf", "20", self.path],
                    stdin=subprocess.PIPE)
            n = int((st - self.t0) * self.fps) + 1      # この時刻までに必要な tick 数
            for _ in range(max(n - self._k, 0)):
                self._ff.stdin.write(data); self._k += 1; self.frames += 1

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._writer, daemon=True); self._thread.start()
        if not self._node.subscribe(Image, self.topic, self._on_img):
            raise RuntimeError("カメラトピックの購読に失敗")

    def stop(self):
        self._running = False
        self._q.put(None); self._thread.join(timeout=120)
        if self._ff:
            self._ff.stdin.close(); self._ff.wait()
        return self.frames
