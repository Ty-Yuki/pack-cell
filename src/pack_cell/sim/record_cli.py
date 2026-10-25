"""録画を別プロセスで行う。制御プロセスと GIL・CPU を取り合わないようにするため。

usage: python3 -m pack_cell.sim.record_cli <camera> <out.mp4>
SIGINT/SIGTERM で停止し、<out.mp4>.json に t0(動画0秒の sim 時刻)とフレーム数を書く。
"""
import json, os, signal, sys, threading

from pack_cell.sim.recorder import Recorder

cam, out = sys.argv[1], sys.argv[2]
rec, done = Recorder(topic=f"/{cam}/image", path=out), threading.Event()
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: done.set())
rec.start()
done.wait()
frames = rec.stop()
with open(out + ".json", "w") as f:
    json.dump({"camera": cam, "t0": rec.t0, "frames": frames, "dropped": rec.dropped, "fps": rec.fps}, f)
os._exit(0)   # gz-transport の終了時デストラクタが segfault するため即時終了
