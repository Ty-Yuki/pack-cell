"""実行ログと録画から、説明付きの動画(1920x1080, 30fps)を作る。

usage: ./scripts/docker_run.sh python3 scripts/make_video.py <run名> [--speed 2] [--out logs/video/<run名>_final.mp4]
  入力: logs/runs/<run名>.json(イベント), logs/video/<run名>_cam_overview.mp4(+.json)
"""
import argparse, json, subprocess, sys
sys.path.insert(0, "/ws/src")
import numpy as np
from PIL import Image

from pack_cell.core import catalog
from pack_cell.viz import compose
from pack_cell.viz.clip import Cam
from pack_cell.viz.compose import Story, draw_frame, W, H

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--speed", type=float, default=2.0)
ap.add_argument("--title", default="言葉で頼める箱詰めセル — LLM制約抽出 × 物理検証 × 2腕協調")
ap.add_argument("--out")
ap.add_argument("--from-t", type=float, default=None, help="開始 sim 時刻(省略時は録画の先頭)")
ap.add_argument("--to-t", type=float, default=None)
ap.add_argument("--mid-h", type=int, default=250, help="中段(ライン状況・実際・計画)の高さ [px]")
ap.add_argument("--over-crop", nargs=3, type=float, default=None, help="見本用: 俯瞰の切り抜き(左 上 右、幅に対する比)")
ap.add_argument("--stills", nargs="+", type=float, default=None, help="動画ではなく、この時刻(動画の秒)の静止画を logs/still_<run>_<秒>.png に書き出す")
a = ap.parse_args()

compose.LAYOUT["mid_h"] = a.mid_h
compose.LAYOUT["over_crop"] = a.over_crop
run = json.load(open(f"/ws/logs/runs/{a.run}.json"))
story = Story(run["events"], catalog.load())


over, box = (Cam(f"/ws/logs/video/{a.run}_{c}.mp4") for c in ("cam_overview", "cam_box"))
out = a.out or f"/ws/logs/video/{a.run}_final.mp4"
enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", "30",
                        "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", out],
                       stdin=subprocess.PIPE)
t_begin = a.from_t if a.from_t is not None else min(over.t0, story.t_first() - 0.5)
t_stop = a.to_t if a.to_t is not None else story.t_end + 3.0
label = f"{a.speed:g}倍速" if a.speed != 1 else "等倍"
if a.stills:
    enc.kill()
    for sec in sorted(a.stills):
        t = t_begin + sec * a.speed
        draw_frame(story, t, over.at(t), a.title, label, box_img=box.at(t)).save(f"/ws/logs/still_{a.run}_m{a.mid_h}_{sec:g}.png")
        print("still", sec)
    sys.exit(0)
k = 0
while True:
    t = t_begin + k * a.speed / 30.0
    if t > t_stop or over.ended:
        break
    enc.stdin.write(draw_frame(story, t, over.at(t), a.title, label, box_img=box.at(t)).tobytes())
    k += 1
enc.stdin.close(); enc.wait()
for c in (over, box):
    c.close()
print(f"{out}: {k} frames ({k / 30:.1f}s)")
