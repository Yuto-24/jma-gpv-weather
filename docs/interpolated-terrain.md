# TOPO.MSM_5K の実験的 opt-in 利用

## 判断

`TOPO.MSM_5K`を、推定QNHの**実験的な明示 opt-in 地形ソース**として採用します。production既定値にはせず、Pzs取得失敗時や地形未指定時のfallbackにも使用しません。

採用理由は、JMBSCが公式配布物を無償公開し、SHA-256、格子仕様、CC BY 4.0の利用条件を固定できること、Lsurfと同じ等緯度経度格子なので補間経路が単純で再現可能なことです。一方、配布値はモデル計算格子からMSM GPV格子へすでに内挿されており、ネイティブLambert格子のPzsとは同一ではありません。海岸・山岳での差とQNHへの影響をPzsに対して未測定であるため、production利用の根拠にはしません。

Issue #6のPzs取得・検証方針は維持します。この実験的採用はIssue #6を置換せず、同Issueをcloseする根拠にもなりません。

## 公式配布物とmanifest

正本のmetadataは[`manifests/topo-msm-5k-2025-05-20.json`](../manifests/topo-msm-5k-2025-05-20.json)です。

- 配布一覧：<https://www.jmbsc.or.jp/jp/online/c-onlineGsd.html>
- 配布ZIP：`chikeidata_joho648.zip`
  - SHA-256: `6251a2494d8ac0ce6a26ee7c8a8dabc854010c5e9173d5b963ba4880791d242e`
- 内包ZIP：`202505_MSM地形データ.zip`
  - SHA-256: `06f678659f8d01b7fc44fe3736da51d78cd398358eca51a34dedea8c9eec75bb`
- `TOPO.MSM_5K`
  - SHA-256: `6ce16ae3781399dad2d618220d81fc41938aa54d693174c33ba347ad976f5250`
- `LANDSEA.MSM_5K`
  - SHA-256: `322bbb1a4086174f7ac813accc391118a1fcd7d13c3135878f52e716f6870483`
- ライセンス：CC BY 4.0、商用利用可
- 更新モデルの運用開始：2025-05-20 00UTC初期値

`2025-05-20`は配布ファイル内のversion文字列ではありません。[気象庁の実施日時通知](https://www.data.jma.go.jp/suishin/oshirase/pdf/20250509.pdf)に基づく、更新モデル地形の運用開始日です。

出典表示は次の情報を保持します。

> 東京大学准教授 山崎大氏作成の MERIT-DEM を気象庁が低解像度化した地形データ（CC BY 4.0）。生成する cache は九州範囲への切り出しと NPZ 圧縮を行った加工物です。

## 格子とvalidator

両ファイルはheaderのないbig-endian IEEE754 float32です。

- shape：505行×481列
- size：`481 × 505 × 4 = 971,620 bytes`
- 先頭：47.6°N、120°E
- 列方向：東向き、0.0625°間隔
- 行方向：南向き、0.05°間隔
- `TOPO`：地表ジオポテンシャル高度（m）
- `LANDSEA`：水域0、陸域1の割合

専用validatorはmanifest、ファイル名、size、SHA-256、big-endianとして解釈した値域、shape、格子方向、九州被覆、有限値を確認します。`LANDSEA`は必須で、欠落時にcacheを生成しません。Pzs validatorとcache loaderはこの形式を受け入れません。

## Cache生成

配布ZIPを手動で取得・展開し、内包する2ファイルを指定します。

```bash
msm-weather prepare-interpolated-terrain \
  --topography /path/to/TOPO.MSM_5K \
  --landsea /path/to/LANDSEA.MSM_5K \
  --source-manifest manifests/topo-msm-5k-2025-05-20.json \
  --output data/static/interpolated-model-terrain/v1/terrain.npz
```

cacheは九州範囲と補間用の1格子haloを切り出してNPZ圧縮する加工物です。NPZ metadataとJSON sidecarには、次を含む完全なprovenanceを保存します。

- `terrain_source_type=interpolated_msm_gpv_topography`
- 公式配布ページ・archive URL
- outer ZIP、inner ZIP、TOPO、LANDSEA、source manifestのSHA-256
- `terrain_model_version=2025-05-20`と根拠
- `terrain_license=CC-BY-4.0`とattribution
- source artifactの加工有無とcacheの加工内容
- `interpolated_from_model_grid=true`
- 元格子と補間方式

## 明示 opt-in

Pzs cacheとは別のCLI flagを指定します。

```bash
msm-weather query-qnh \
  --time 2026-07-27T12:00:00Z \
  --run 2026-07-27T12:00:00Z \
  --lat 31.877 --lon 131.449 --elevation-m-msl 6 \
  --interpolated-terrain-cache \
  data/static/interpolated-model-terrain/v1/terrain.npz
```

`--terrain-cache`はPzs専用です。両flagは排他的で、cache形式の自動判定はしません。どちらも指定しなければ従来どおり`MODEL_TERRAIN_UNAVAILABLE`です。

Python APIでもproviderを明示的に生成またはloadし、`MsmClient.prepare_run(..., terrain_provider=provider)`へ渡します。clientが自動取得・自動fallbackすることはありません。

## Warningと海岸診断

このproviderを使ったQNHには常に次を付けます。

- `INTERPOLATED_MODEL_TERRAIN`
- `ESTIMATED_QNH_NOT_OFFICIAL`
- `NOT_FOR_OPERATIONAL_USE`

補間地点の`LANDSEA`値は`terrain_land_fraction`としてQNH valuesとprovenanceへ記録します。値が`0.05 < land_fraction < 0.95`なら、海岸の混合格子として`COASTAL_MIXED_LAND_FRACTION`も付けます。このthresholdは品質保証境界ではなく、追加確認を促す診断基準です。

## 固定Run検証

次のopt-in試験は、公式TOPO/LANDSEAとRISH固定Run `2026-07-27 12Z / Lsurf FH00-15`を使い、宮崎空港付近（31.877°N、131.449°E、標高6m）を検証します。

```bash
MSM_TOPO_5K=/path/to/TOPO.MSM_5K \
MSM_LANDSEA_5K=/path/to/LANDSEA.MSM_5K \
MSM_RUN_REAL_DATA=1 \
pytest -q -s -m real_data \
  tests/test_interpolated_terrain_real_data.py
```

2026-07-30に公式配布ZIPと全内包SHA-256を再検証し、[GitHub Actions run 30468895143](https://github.com/Yuto-24/jma-msm-wind-kyushu/actions/runs/30468895143)で2件の実データ試験が成功しました。

- TOPO地形高度：`31.3286265886 m`
- LANDSEA陸比：`0.5061216165`
- 海岸判定：`true`（`COASTAL_MIXED_LAND_FRACTION`）
- 推定QNH：`1012.9807482038 hPa`
- warning：`INTERPOLATED_MODEL_TERRAIN`、`ESTIMATED_QNH_NOT_OFFICIAL`、`NOT_FOR_OPERATIONAL_USE`、海岸混合警告

地形高度を変えたQNH感度は次のとおりです。

| 地形offset | QNH | baseline差 |
|---:|---:|---:|
| -50 m | 1007.3344156677 hPa | -5.6463325361 hPa |
| -25 m | 1010.1543874699 hPa | -2.8263607339 hPa |
| -10 m | 1011.8494365560 hPa | -1.1313116478 hPa |
| 0 m | 1012.9807482038 hPa | 0 hPa |
| +10 m | 1014.1130837873 hPa | +1.1323355835 hPa |
| +25 m | 1015.8135085363 hPa | +2.8327603325 hPa |
| +50 m | 1018.6526791446 hPa | +5.6719309408 hPa |

この地点では地形高度誤差10mあたり約1.13hPa、50mで約5.66hPaのQNH差となり、海岸・山岳で内挿済み地形を明示する必要性を支持します。

## 制約と次の判断

Pzsが未入手のため、現時点では宮崎空港・山岳・海岸・平野の代表点におけるPzsとの差、およびそのQNH差を測定できません。したがって次を禁止します。

- Pzs、ネイティブLambert地形、817×661格子として表示すること
- TOPOから擬似Pzsを逆生成すること
- production既定値にすること
- Pzs、外部DEM、MSLP取得失敗時のfallbackにすること

Pzs入手後、代表点でPzs/TOPOの地形高度とQNH差を比較し、山岳・海岸の許容条件を定義できた場合に限り、実験的扱いを再評価します。
