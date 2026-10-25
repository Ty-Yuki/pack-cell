# 商品ラベル画像の生成プロンプト

実物を撮影せずに、日本のネットスーパーの品らしい見た目にするための画像です。生成した画像は、こちらで自作の形状（実寸）にテクスチャとして貼り、商品名などの**日本語の文字はフォントで重ねます**。

## 共通のお願い

- **ブランドはすべて架空**です。実在の商品・ロゴ・キャラクターに似せないでください。
- 画像には**文字・数字・ロゴ・バーコード・透かしを入れない**でください（文字はこちらで入れます）。
- **真正面から見た平面の印刷デザイン**にしてください。斜めから見た図、立体の商品写真、影、背景、机などは不要です。画像の端まで印刷面にしてください（余白なし）。
- 照明は均一にしてください（テクスチャとして貼ったときに、シミュレータ側の照明が当たるため）。
- 下の各品目の**縦横比**に合わせ、長辺 2048px 程度の PNG でください。比率が合わない場合は、少し大きめに作っていただければこちらで切り抜きます。
- ファイル名は `<id>_<面>.png`（例: `milk_front.png`）にして、`assets/labels_src/` に置いてください。
- 各プロンプトの「文字を入れる場所」は、こちらで商品名を重ねる予定の位置です。そこは絵柄を控えめにしてください。
- 生成サービスの利用規約で、出力画像を公開動画・公開リポジトリに使えることを確認してください。

### 全品目共通で末尾に付ける文（スタイルの統一用）

```
Flat orthographic packaging print artwork, front-on view, no perspective, no 3D object, no shadow, no background scene,
full-bleed design that fills the entire canvas, evenly lit like a scanned printed label, realistic Japanese supermarket
packaging aesthetic, high-quality commercial print, crisp vector-like shapes combined with photographic food imagery,
NO text, NO letters, NO numbers, NO logos, NO barcodes, NO watermark.
```

（ネガティブプロンプトが使える場合: `text, letters, words, numbers, logo, brand name, barcode, watermark, signature, perspective, 3d render, product mockup, shadow, table, hand, frame, border`）

## 生成するときの比率（標準の比率しか選べない場合）

いただいた画像は `scripts/prepare_labels.py` で、切り抜き・回転・継ぎ足しをして貼る面の比率に合わせます。生成サービスで選べる比率は、次の表のものを選んでください。

| 品目と面 | 選ぶ比率 | こちらでの加工 | 状態 |
|---|---|---|---|
| 牛乳・低脂肪乳（正面・側面） | 9:16 | 左右を少し切る | 受領済み |
| 卵の帯 | 16:9 | 帯を切り出し、中央の無地部分を横に伸ばす | 受領済み |
| 豆腐のふた | 4:3 | 上下を継ぎ足す | 受領済み |
| 食パンの袋 | 1:1 | 少し切る | 受領済み |
| 米袋（表） | 2:3（縦長） | 90°回して袋の長辺に合わせる | 受領済み |
| ポテトチップス | 3:4（縦長） | 90°回す | 受領済み |
| アイス（側面） | 16:9 | 左右を鏡映しで継ぎ足して一周巻く | 受領済み |
| アイス（ふた） | 1:1 | そのまま | 受領済み |
| 水2L・缶詰 | 16:9 | 左右を鏡映しで継ぎ足して一周巻く | 受領済み |
| **台所用洗剤（正面）** | **9:16（縦長）** | 少し切る | **作り直しのお願い**（下記） |
| 段ボール箱・ガムテープ | 16:9 | 切り抜く | 任意 |

### 作り直しのお願い: 台所用洗剤 `detergent_front`（9:16 の縦長で）

横長で届いた版は、縦長のボトルに合わせるために上下を大きく継ぎ足していて、レモンと皿が下端で切れています。次のプロンプトで、**9:16（縦長）**を選んで生成してください。

```
Tall vertical front label for a Japanese kitchen dish detergent bottle, portrait 9:16 composition.
Upper half: a clean, smooth fresh lime-green gradient area with only a few tiny sparkles and small soap bubbles,
kept very calm and empty (a product name will be added there later).
Lower half: a realistic splash of clear water and soap bubbles rising from the bottom, a fresh lemon slice and a
half lemon, and the rim of a sparkling clean white plate, all fully inside the frame (nothing cut off at the edges).
Flat orthographic packaging print artwork, front-on view, no perspective, no 3D bottle, no shadow, no background scene,
full-bleed design that fills the entire canvas, evenly lit like a scanned printed label, NO text, NO letters,
NO numbers, NO logos, NO barcodes, NO watermark.
```

---

## 優先度 高（日本らしさの決め手。自作形状に貼る）

### 1. 卵パック `egg`（10個入り 26×10.5×6.5cm）

透明なパックに挟む**紙の帯ラベル**です。卵本体は色で表現するので不要です。

- `egg_band.png`: 縦横比 **26:6（約 4.3:1、横長）**
```
A long horizontal paper band label for a Japanese 10-pack of fresh brown eggs. Warm cream background with a soft
watercolor illustration of a sunny countryside farm: a small red barn, green rolling hills, and two plump hens on the
left side; three glossy brown eggs nestled in straw on the right side. Thin orange decorative border stripes along the
top and bottom edges. Leave the central 40% of the band as a calm, lightly textured cream area for a product name.
```
文字を入れる場所: 中央（「朝採れたまご 10個入」と架空の農場名）

### 2. 豆腐 `tofu`（11×8.5×4cm）

容器にかぶせる**フィルムのふた**（上面）です。

- `tofu_top.png`: 縦横比 **11:8.5（約 1.3:1）**
```
Top film lid print for a Japanese silken tofu package. Clean white background with a subtle pale blue water ripple
pattern. On the right side, a realistic photograph-style image of a smooth white block of silken tofu with a few green
soybeans and a single green leaf beside it. A deep indigo vertical band on the far left edge with a traditional Japanese
wave (seigaiha) pattern. Leave the center-left area mostly white and calm for a product name.
```
文字を入れる場所: 中央左（「絹とうふ」と架空の銘柄）

### 3. 牛乳 1L `milk`（屋根型 7×7×19.5cm）

屋根型の紙パックの**胴の側面**です（4面のうち正面と側面の2種類）。

- `milk_front.png`: 縦横比 **7:16（縦長、約 0.44:1）**
```
Front side panel of a Japanese 1-liter gable-top milk carton. Top two thirds: crisp white background with a fresh blue
sky and a green pasture at the bottom of the panel, two black-and-white Holstein cows grazing, gentle sunlight. A bold
royal blue horizontal stripe across the lower third with a thin white wave line. Leave a large clean white area in the
upper middle for a vertical product name.
```
- `milk_side.png`: 縦横比 **7:16**
```
Side panel of the same Japanese milk carton design. Mostly clean white background, a royal blue stripe at the bottom
matching the front panel, a small simple illustration of a milk bottle and a glass of milk near the top, and a large
empty white rectangular area in the middle (for a nutrition table to be added later).
```
文字を入れる場所: 正面の上〜中央（「みどり牧場 牛乳」）、側面の中央（成分表）

### 4. 低脂肪乳 1L `lowfat_milk`（牛乳と同じ形）

- `lowfat_milk_front.png` / `lowfat_milk_side.png`: 縦横比 **7:16**
```
(milk_front / milk_side と同じ文に、次を追加)
Color variant: replace the royal blue with fresh turquoise and light green, add a subtle pattern of small leaves, lighter
and healthier feeling than the regular milk.
```
文字を入れる場所: 牛乳と同じ（「みどり牧場 低脂肪乳」）

### 5. 食パン `bread`（6枚切りの袋 13×12×12cm）

パンの袋の**印刷**です。袋の中のパンは別に色を付けます。可能なら背景を透明にしてください（できなければ純白の背景で構いません）。

- `bread_bag.png`: 縦横比 **13:12（ほぼ正方形）**、可能なら背景透明
```
Print design for a Japanese sliced white bread (shokupan) plastic bag, front face. Transparent or pure white background
(the plastic). A wide warm golden-yellow band across the middle third with a soft wheat-ear illustration on both ends,
and a small red and blue ribbon accent. Above the band, a realistic photographic image of a fluffy square slice of
Japanese milk bread with a golden crust. Leave the band center clear for a product name.
```
文字を入れる場所: 中央の帯（「朝のしあわせ 食パン 6枚切」）

### 6. 米 2kg `rice`（平袋 33×22×6cm）

- `rice_front.png`: 縦横比 **33:22（3:2）**
```
Front of a Japanese 2 kg rice bag. Off-white kraft paper texture background. A large watercolor illustration of golden
terraced rice fields at sunset with distant mountains in the upper half, and a close-up photographic image of shiny
white cooked rice grains in a black lacquer bowl at the lower right. A vertical crimson band on the left side with a
subtle traditional Japanese hemp-leaf (asanoha) pattern. Leave a large calm area at the upper center for a vertical
calligraphic product name.
```
- `rice_back.png`: 縦横比 **3:2**
```
Back of the same Japanese rice bag: plain off-white kraft paper texture, a thin crimson border, a small illustration of
rice ears in one corner, and a large empty area for a table of product information.
```
文字を入れる場所: 正面の上中央（「田園の米 2kg」）、裏面（表示欄）

---

## 優先度 中（公開スキャンに良い代わりがなければ使う）

### 7. ポテトチップス `chips`（袋 25×17×6cm）

- `chips_front.png`: 縦横比 **25:17（約 1.47:1）**
```
Front of a Japanese potato chips foil snack bag. Glossy metallic golden-yellow background with a burst of light rays
from the center, a pile of crispy wavy potato chips photographed realistically in the lower half, a few flakes of sea
salt and a small sprig of parsley, a red diagonal ribbon across the upper left corner. Leave the upper center clear for
a big playful product name.
```
文字を入れる場所: 上中央（「カリッと うすしお」）

### 8. アイス `ice`（カップ φ9.5×6cm）

- `ice_side.png`: 縦横比 **30:6（5:1、横長。カップの側面に巻く）**
```
Wrap-around side label for a Japanese premium vanilla ice cream cup. Deep burgundy background with a soft cream-colored
wave across the middle, realistic photographic scoops of vanilla ice cream with visible vanilla bean specks repeated
twice along the length, a few vanilla flowers. Seamless left and right edges so it can wrap around a cylinder.
```
- `ice_lid.png`: 縦横比 **1:1（ふたの上面、円形に切り抜く）**
```
Round lid top of the same vanilla ice cream cup, square canvas with the design centered in a circle: burgundy outer ring,
cream center with a single photographic scoop of vanilla ice cream and a vanilla flower, empty area at the top of the
circle for a product name.
```
文字を入れる場所: ふたの上部（「濃厚バニラ」）

### 9. 水 2L `water`（横置きの角型ペット 31×10.5×10.5cm）

- `water_label.png`: 縦横比 **42:10（約 4.2:1、横長。胴に一周巻く）**
```
Wrap-around label for a Japanese 2-liter natural mineral water PET bottle. Clear sky-blue gradient background, a
photographic image of snowy mountain peaks and a clear forest stream, fresh green beech leaves at the edges, subtle
water droplets. Seamless left and right edges for wrapping. Leave the center calm for a product name.
```
文字を入れる場所: 中央（「やまびこ天然水 2L」）

### 10. 缶詰 `canned`（サバ缶 φ8.5×4.5cm）

- `canned_label.png`: 縦横比 **27:4.5（6:1、横長。胴に一周巻く）**
```
Wrap-around label for a Japanese canned mackerel (saba mizuni) tin. Deep navy blue background with a realistic
photographic image of cooked mackerel fillets on a small white plate, a stylized silver fish illustration and ocean wave
lines. Seamless left and right edges for wrapping around a short cylinder.
```
文字を入れる場所: 中央（「さば水煮」）

### 11. 台所用洗剤 `detergent`（ボトル 6×4.5×21cm）

- `detergent_front.png`: 縦横比 **6:15（縦長、0.4:1）**
```
Front label for a Japanese kitchen dish detergent bottle. Fresh lime-green background with a realistic splash of clear
water and bubbles, a sliced lemon and a sparkling clean white plate at the bottom. Leave the upper half clear for a
vertical product name.
```
文字を入れる場所: 上半分（「ピカリン 台所用洗剤」）

---

## あれば嬉しいもの

### 12. 段ボール箱 `carton`（44×32×26cm）

- `carton_side.png`: 縦横比 **44:26（約 1.7:1）**
```
Side of a brown corrugated cardboard shipping box, realistic kraft paper texture with subtle flute lines and slight
natural color variation, a simple one-color dark green printed illustration of a delivery truck and a leaf in one corner
(fictional online supermarket), nothing else.
```
- `carton_tape.png`: 縦横比 **8:1（横長、ガムテープ）**
```
A strip of beige kraft packing tape texture, slightly glossy with subtle paper fibers, plain without any print.
```
