"""生成 AI で作った商品ラベルの元画像(assets/labels_src/)を、貼る面の縦横比にそろえ、日本語の商品名を重ねる。

元画像は拡張子・サイズ・比率がまちまち(生成サービスで指定できる比率が 1:1, 4:3, 16:9 などに限られる)なので、
品目ごとに「切り抜き・回転・継ぎ足し(鏡映し/端の色)」で目標の比率に合わせる。
出力: assets/labels/<品目>_<面>.jpg(長辺 1024px)と、確認用の一覧 logs/labels_out.png

usage: ./scripts/docker_run.sh python3 scripts/prepare_labels.py
"""
import pathlib

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC, OUT = ROOT / "assets/labels_src", ROOT / "assets/labels"
FONT_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
SERIF = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"
LONG = 1024


def load(name):
    for ext in (".png", ".jpeg", ".jpg", ".webp"):
        p = SRC / f"{name}{ext}"
        if p.exists():
            return Image.open(p).convert("RGB")
    return None


def trim(im, thr=12):
    """周りの白い(ほぼ一様な)余白を切る。"""
    bg = Image.new("RGB", im.size, im.getpixel((2, 2)))
    diff = ImageOps.grayscale(Image.fromarray(__import__("numpy").abs(
        __import__("numpy").asarray(im, int) - __import__("numpy").asarray(bg, int)).astype("uint8")))
    box = diff.point(lambda v: 255 if v > thr else 0).getbbox()
    return im.crop(box) if box else im


def crop_ratio(im, ratio, cx=0.5, cy=0.5):
    """縦横比 ratio(幅/高さ)になるよう、中心 (cx, cy) の位置で切り抜く。"""
    w, h = im.size
    if w / h > ratio:
        nw = int(h * ratio)
        x0 = int(min(max(cx * w - nw / 2, 0), w - nw))
        return im.crop((x0, 0, x0 + nw, h))
    nh = int(w / ratio)
    y0 = int(min(max(cy * h - nh / 2, 0), h - nh))
    return im.crop((0, y0, w, y0 + nh))


def content_box(im, light=235, frac=0.5):
    """白っぽくない画素が行・列の frac 以上を占める範囲(=絵柄の本体)で切り抜く。"""
    import numpy as np
    a = np.asarray(im.convert("L"))
    dark = a < light
    rows = np.where(dark.mean(axis=1) > frac)[0]
    cols = np.where(dark.mean(axis=0) > frac)[0]
    if len(rows) == 0 or len(cols) == 0:
        return im
    x0, x1, y0, y1 = cols[0], cols[-1] + 1, rows[0], rows[-1] + 1
    # 端から 8% 以内にある白い枠線(白い列・行)は、その内側まで削る
    def edge(m, lo, hi):
        n = hi - lo
        k = int(n * 0.08)
        head = [i for i in range(lo, lo + k) if m[i] > 235]
        tail = [i for i in range(hi - k, hi) if m[i] > 235]
        return (head[-1] + 1 if head else lo), (tail[0] if tail else hi)
    x0, x1 = edge(a[y0:y1].mean(axis=0), x0, x1)
    y0, y1 = edge(a[:, x0:x1].mean(axis=1), y0, y1)
    return im.crop((x0, y0, x1, y1))


def inset(im, f):
    """縁を f(割合)だけ内側へ切る。"""
    w, h = im.size
    dx, dy = int(w * f), int(h * f)
    return im.crop((dx, dy, w - dx, h - dy))


def mirror_wrap(im, ratio):
    """円柱に一周巻くラベル: 左右を鏡映しで継ぎ足して横長にする(継ぎ目が連続する)。"""
    w, h = im.size
    tw = int(h * ratio)
    out = Image.new("RGB", (tw, h))
    tiles = [im, ImageOps.mirror(im)]
    x, k = (tw - w) // 2, 0
    # 中央に元画像、外側へ交互に鏡映し
    out.paste(im, (x, 0))
    left, right, k = x, x + w, 1
    while left > 0 or right < tw:
        t = tiles[k % 2]
        out.paste(t, (left - w, 0)); out.paste(t, (right, 0))
        left -= w; right += w; k += 1
    return out


def pad_to(im, ratio, color=None, blur_edge=True):
    """足りない方向に、端の色(またはぼかした端)で継ぎ足して縦横比を合わせる。"""
    w, h = im.size
    if abs(w / h - ratio) < 1e-3:
        return im
    if w / h > ratio:
        nh = int(w / ratio); out = Image.new("RGB", (w, nh), color or im.getpixel((w // 2, 2)))
        if blur_edge and color is None:
            top = im.crop((0, 0, w, max(2, h // 20))).resize((w, (nh - h) // 2 + 2)).filter(ImageFilter.GaussianBlur(8))
            bot = im.crop((0, h - max(2, h // 20), w, h)).resize((w, nh - h - (nh - h) // 2 + 2)).filter(ImageFilter.GaussianBlur(8))
            out.paste(top, (0, 0)); out.paste(bot, (0, (nh - h) // 2 + h - 1))
        out.paste(im, (0, (nh - h) // 2))
        return out
    nw = int(h * ratio); out = Image.new("RGB", (nw, h), color or im.getpixel((2, h // 2)))
    out.paste(im, ((nw - w) // 2, 0))
    return out


def stretch_middle(im, ratio, x0f, x1f):
    """横長の帯: 中央の無地の部分(x0f〜x1f)だけを横に伸ばして ratio にする。"""
    w, h = im.size
    tw = int(h * ratio)
    a, b = int(x0f * w), int(x1f * w)
    mid = im.crop((a, 0, b, h)).resize((tw - (w - (b - a)), h))
    out = Image.new("RGB", (tw, h))
    out.paste(im.crop((0, 0, a, h)), (0, 0)); out.paste(mid, (a, 0)); out.paste(im.crop((b, 0, w, h)), (a + mid.width, 0))
    return out


def text(im, s, xy, size_f, font=FONT_B, fill=(255, 255, 255), stroke=None, vertical=False, anchor="mm"):
    """画像の幅に対する割合で文字を置く。vertical: 縦書き(1文字ずつ下へ)。"""
    d = ImageDraw.Draw(im)
    W, H = im.size
    size = int(size_f * W)
    f = ImageFont.truetype(font, size)
    x, y = xy[0] * W, xy[1] * H
    kw = dict(font=f, fill=fill)
    if stroke:
        kw.update(stroke_width=max(2, size // 14), stroke_fill=stroke)
    if vertical:
        n = len(s)
        for i, ch in enumerate(s):
            d.text((x, y + (i - (n - 1) / 2) * size * 1.05), ch, anchor="mm", **kw)
    else:
        d.text((x, y), s, anchor=anchor, **kw)
    return im


def small_table(im, box_f, lines, size_f=0.045):
    """成分表など: 白い枠の中に小さい文字の行を並べる。"""
    d = ImageDraw.Draw(im)
    W, H = im.size
    x0, y0, x1, y1 = box_f[0] * W, box_f[1] * H, box_f[2] * W, box_f[3] * H
    f = ImageFont.truetype(FONT_R, int(size_f * W))
    step = (y1 - y0) / (len(lines) + 1)
    for i, (k, v) in enumerate(lines):
        y = y0 + step * (i + 0.8)
        d.text((x0 + 0.04 * W, y), k, font=f, fill=(40, 40, 40), anchor="lm")
        d.text((x1 - 0.04 * W, y), v, font=f, fill=(40, 40, 40), anchor="rm")
        d.line((x0 + 0.03 * W, y + step / 2, x1 - 0.03 * W, y + step / 2), fill=(170, 170, 170), width=1)
    return im


def fit(im):
    w, h = im.size
    s = LONG / max(w, h)
    return im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


# 品目ごとの加工。返り値: {出力名: 画像}。ratio は貼る面の 幅/高さ(品物の座標系で、W 方向を横にした向き)
def make_all():
    out = {}
    if (im := load("milk_front")):
        im = crop_ratio(trim(im), 7 / 16, cy=0.5)
        text(im, "みどり牧場", (0.5, 0.40), 0.16, fill=(20, 60, 140))
        text(im, "牛乳", (0.5, 0.50), 0.30, fill=(20, 60, 140))
        text(im, "成分無調整 1000ml", (0.5, 0.835), 0.075, fill=(255, 255, 255))
        out["milk_front"] = im
    if (im := load("milk_side")):
        im = crop_ratio(trim(im), 7 / 16)
        small_table(im, (0.08, 0.36, 0.92, 0.68), [("種類別", "牛乳"), ("無脂乳固形分", "8.3%"), ("乳脂肪分", "3.5%"),
                                                   ("内容量", "1000ml"), ("要冷蔵", "10℃以下")], 0.055)
        out["milk_side"] = im
    if (im := load("lowfat_milk_front")):
        im = crop_ratio(trim(im), 7 / 16)
        text(im, "みどり牧場", (0.5, 0.40), 0.16, fill=(10, 110, 110))
        text(im, "低脂肪乳", (0.5, 0.50), 0.22, fill=(10, 110, 110))
        text(im, "乳脂肪分1.0% 1000ml", (0.5, 0.835), 0.07, fill=(255, 255, 255), stroke=(10, 110, 110))
        out["lowfat_milk_front"] = im
    if (im := load("lowfat_milk_side")):
        im = crop_ratio(trim(im), 7 / 16)
        small_table(im, (0.08, 0.36, 0.92, 0.68), [("種類別", "加工乳"), ("無脂乳固形分", "8.5%"), ("乳脂肪分", "1.0%"),
                                                   ("内容量", "1000ml"), ("要冷蔵", "10℃以下")], 0.055)
        out["lowfat_milk_side"] = im
    if (im := load("egg_band")):
        band = trim(im)                                   # 白い余白を除いて帯だけ
        band = stretch_middle(band, 26 / 6, 0.40, 0.60)
        text(band, "朝採れたまご", (0.5, 0.42), 0.075, fill=(150, 70, 20), font=SERIF)
        text(band, "Mサイズ 10個入", (0.5, 0.72), 0.035, fill=(150, 70, 20))
        out["egg_band"] = band
    if (im := load("tofu_top")):
        im = pad_to(trim(im), 11 / 8.5)
        text(im, "絹とうふ", (0.40, 0.42), 0.10, fill=(25, 45, 110), font=SERIF)
        text(im, "国産大豆100% 300g", (0.40, 0.60), 0.040, fill=(25, 45, 110))
        out["tofu_top"] = im
    if (im := load("bread_bag")):
        im = crop_ratio(im, 13 / 12)
        text(im, "朝のしあわせ食パン", (0.5, 0.645), 0.075, fill=(150, 40, 20), stroke=(255, 245, 210))
        text(im, "6枚切", (0.5, 0.735), 0.05, fill=(150, 40, 20))
        out["bread_bag"] = im
    if (im := load("rice_front")):
        im = trim(im)
        im = im.rotate(90, expand=True)                   # 図柄は縦長(袋の長辺が縦) → 品物の W(33cm)方向へ
        im = crop_ratio(im, 33 / 22)
        # 縦書きの銘柄は回す前の向きで入れたいので、回した画像を一度戻して書き、また回す
        im = im.rotate(-90, expand=True)
        text(im, "田園の米", (0.55, 0.30), 0.13, fill=(120, 20, 20), font=SERIF, vertical=True, stroke=(255, 250, 235))
        text(im, "精米 2kg", (0.55, 0.58), 0.06, fill=(120, 20, 20), stroke=(255, 250, 235))
        out["rice_front"] = im.rotate(90, expand=True)
    if (im := load("chips_front")):
        im = trim(im)
        text(im, "カリッと", (0.5, 0.17), 0.12, fill=(200, 30, 20), stroke=(255, 255, 255))
        text(im, "うすしお", (0.5, 0.29), 0.16, fill=(200, 30, 20), stroke=(255, 255, 255))
        im = crop_ratio(im.rotate(90, expand=True), 25 / 17)
        out["chips_front"] = im
    if (im := load("ice_side")):
        im = inset(content_box(im), 0.004)                # 帯の内側の細い白枠まで除く
        im = mirror_wrap(im, (3.14159 * 9.5) / 6)
        out["ice_side"] = im
    if (im := load("ice_lid")):
        im = crop_ratio(im, 1.0)
        text(im, "濃厚バニラ", (0.5, 0.20), 0.09, fill=(110, 20, 40), font=SERIF, stroke=(255, 245, 225))
        out["ice_lid"] = im
    # 一周巻くラベルは、先に継ぎ足してから中央にだけ文字を入れる(鏡映しの部分で文字が裏返らないように)
    if (im := load("water_label")):
        im = mirror_wrap(inset(trim(im), 0.005), 42 / 10)
        text(im, "やまびこ天然水", (0.5, 0.45), 0.024, fill=(20, 70, 150), font=SERIF, stroke=(255, 255, 255))
        text(im, "2L", (0.5, 0.62), 0.02, fill=(20, 70, 150), stroke=(255, 255, 255))
        out["water_label"] = im
    if (im := load("canned_label")):
        im = mirror_wrap(inset(trim(im), 0.005), (3.14159 * 8.5) / 4.5)
        text(im, "さば水煮", (0.5, 0.25), 0.022, fill=(255, 255, 255), font=SERIF)
        out["canned_label"] = im
    if (im := load("detergent_front")):
        # 横長の図柄しかない場合の仮: レモンと皿の部分を下に置き、上を背景色で継ぎ足した縦長にする
        im = trim(im)
        w, h = im.size
        if w > h:
            art = crop_ratio(im, 0.85, cx=0.42)
            canvas = Image.new("RGB", (art.width, int(art.width / (6 / 15))), art.getpixel((art.width // 2, 4)))
            grad = art.crop((0, 0, art.width, 8)).resize((art.width, canvas.height - art.height + 8)).filter(ImageFilter.GaussianBlur(4))
            canvas.paste(grad, (0, 0)); canvas.paste(art, (0, canvas.height - art.height))
            im = canvas
        else:
            im = crop_ratio(im, 6 / 15, cx=0.56)          # 縦長の版: レモンと皿が収まる位置で左右を切る
        text(im, "ピカリン", (0.5, 0.17), 0.22, fill=(255, 255, 255), stroke=(20, 110, 40))
        text(im, "台所用洗剤", (0.5, 0.29), 0.12, fill=(20, 90, 30))
        text(im, "レモンの香り", (0.5, 0.37), 0.08, fill=(20, 90, 30))
        out["detergent_front"] = im
    if (im := load("rice_back")):
        im = trim(im).rotate(90, expand=True)             # 表と同じ向き(袋の長辺を横)にそろえる
        out["rice_back"] = crop_ratio(im, 33 / 22)
    out["detergent_back"] = detergent_back()
    out["carton_side"] = kraft(1024, 605, print_logo=True)
    out["carton_plain"] = kraft(1024, 1024)
    out["carton_top"] = carton_top()
    return out


def kraft(w, h, print_logo=False, seed=0):
    """段ボールの質感(クラフト紙の色むら + 縦の波目 + 繊維)を画像処理で作る。"""
    import numpy as np
    rng = np.random.default_rng(seed)
    base = np.array([176, 136, 92], float)
    noise = rng.normal(0, 1, (h // 8 + 1, w // 8 + 1))
    noise = np.kron(noise, np.ones((8, 8)))[:h, :w]
    img = np.zeros((h, w, 3)) + base
    img += (noise * 6)[..., None]
    img += rng.normal(0, 4, (h, w))[..., None]              # 繊維の細かいざらつき
    x = np.arange(w)
    img += (np.sin(x / w * 2 * np.pi * 60) * 3.0)[None, :, None]   # 波目(フルート)
    im = Image.fromarray(np.clip(img, 0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(0.6))
    if print_logo:                                         # 架空のネットスーパーの一色刷り
        d = ImageDraw.Draw(im)
        green = (40, 90, 50)
        cx, cy = int(w * 0.80), int(h * 0.30)
        d.rounded_rectangle([cx - 70, cy - 22, cx + 30, cy + 22], 6, outline=green, width=5)   # トラックの荷台
        d.rectangle([cx + 30, cy - 8, cx + 62, cy + 22], outline=green, width=5)               # 運転席
        for wx in (cx - 45, cx + 40):
            d.ellipse([wx - 12, cy + 16, wx + 12, cy + 40], outline=green, width=5)
        d.ellipse([cx - 40, cy - 14, cx - 10, cy + 10], outline=green, width=4)                # 葉のマーク
        f = ImageFont.truetype(FONT_B, 34)
        d.text((cx - 4, cy + 70), "みどりマート ネットスーパー", font=f, fill=green, anchor="mm")
        f2 = ImageFont.truetype(FONT_R, 22)
        d.text((int(w * 0.18), int(h * 0.85)), "天地無用・ワレモノ注意", font=f2, fill=(150, 40, 40), anchor="mm")
    return im


def carton_top():
    """閉じた箱の上面: 2枚のフラップの合わせ目と、その上のガムテープ。"""
    im = kraft(1024, 745, seed=1)
    d = ImageDraw.Draw(im)
    w, h = im.size
    d.line([(0, h // 2), (w, h // 2)], fill=(120, 90, 60), width=3)          # フラップの合わせ目
    tape_h = int(h * 0.16)
    import numpy as np
    tape = np.zeros((tape_h, w, 3)) + np.array([206, 178, 128], float)
    tape += np.random.default_rng(2).normal(0, 3, (tape_h, w))[..., None]
    tape[:4] -= 25; tape[-4:] -= 25                                            # テープの縁
    im.paste(Image.fromarray(np.clip(tape, 0, 255).astype("uint8")), (0, h // 2 - tape_h // 2))
    return im


def detergent_back():
    """洗剤の裏ラベル(使用量・注意書き)。表と同じ緑の帯。"""
    w, h = 400, 1000
    im = Image.new("RGB", (w, h), (246, 250, 240))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 70], fill=(120, 190, 60)); d.rectangle([0, h - 70, w, h], fill=(120, 190, 60))
    f = ImageFont.truetype(FONT_B, 34); f2 = ImageFont.truetype(FONT_R, 24)
    d.text((w // 2, 140), "ピカリン", font=f, fill=(20, 90, 30), anchor="mm")
    lines = ["品名: 台所用合成洗剤", "用途: 食器・調理用具用", "液性: 中性", "使用量の目安:", "水1Lに対して1.5ml",
             "", "使用上の注意", "・用途外に使わない", "・子供の手の届く所に", "  置かない", "", "内容量 300ml"]
    for i, t in enumerate(lines):
        d.text((30, 220 + i * 52), t, font=f2, fill=(50, 60, 50))
    return im


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    res = make_all()
    for k, im in res.items():
        fit(im).save(OUT / f"{k}.jpg", quality=90)
    # 確認用の一覧
    cell = 300
    cols = 5
    rows = (len(res) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 26)), "white")
    d = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT_R, 18)
    for i, (k, im) in enumerate(res.items()):
        t = im.copy(); t.thumbnail((cell - 8, cell - 8))
        x, y = (i % cols) * cell, (i // cols) * (cell + 26)
        sheet.paste(t, (x + 4, y + 4)); d.text((x + 4, y + cell + 2), f"{k} {im.width / im.height:.2f}", font=f, fill="black")
    (ROOT / "logs").mkdir(exist_ok=True)
    sheet.save(ROOT / "logs/labels_out.png")
    print("\n".join(f"{k}: {im.size} ratio {im.width / im.height:.2f}" for k, im in res.items()))
