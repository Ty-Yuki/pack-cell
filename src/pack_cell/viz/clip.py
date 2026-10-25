"""録画(mp4 + 時刻の json)を sim 時刻で引く。"""
import json, subprocess

from PIL import Image


class Cam:
    """録画を sim 時刻で前から順に読む(戻れない。戻るときは作り直す)。録画の前の時刻は最初のフレーム、後は最後のフレーム。"""

    def __init__(self, path):
        self.path = path
        try:
            meta = json.load(open(path + ".json"))
        except FileNotFoundError:
            self.dec = None
            return
        self.t0, self.fps = meta["t0"], meta["fps"]
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip().split(",")
        self.w, self.h = int(probe[0]), int(probe[1])
        self.dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                    stdout=subprocess.PIPE)
        self.idx, self.frame, self.ended = -1, None, False

    def at(self, t):
        if self.dec is None:
            return None
        want = max(0, int((t - self.t0) * self.fps))
        while self.idx < want and not self.ended:
            buf = self.dec.stdout.read(self.w * self.h * 3)
            if len(buf) < self.w * self.h * 3:
                self.ended = True
                break
            self.frame, self.idx = buf, self.idx + 1
        return Image.frombuffer("RGB", (self.w, self.h), self.frame) if self.frame else None

    def close(self):
        if self.dec:
            self.dec.kill()
