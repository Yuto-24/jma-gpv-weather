# MSMモデル地形（Pzs）の取得・検証・更新方針

## 正本

推定QNHに使用するモデル地形の正本は、一般財団法人気象業務支援センター（JMBSC）の公式窓口から取得した、2025年5月20日00UTC以降のMSMモデル面地形標高`Pzs_FH00`元GRIB2です。

対象ファイル名は次の形式です。

```text
Z__C_RJTD_yyyyMMddhhmmss_MSM_GPV_Rjp_Glm5km_Lm1-39_Pzs_FH00_grib2.bin
```

気象庁の[技術情報第619号](https://www.data.jma.go.jp/suishin/jyouhou/pdf/619.pdf)に基づき、次を検証します。

- GRIB edition 2、資料分野0
- 地形標高Pzs：パラメータカテゴリ3、パラメータ番号33
- Lambert正角円錐図法：格子定義テンプレート3.30
- X方向817点、Y方向661点、計540,037点
- 予報時間FH00、地面または水面
- GRIB初期時刻とファイル名・source manifestの一致

[技術情報第648号](https://www.data.jma.go.jp/suishin/jyouhou/pdf/648.pdf)でモデル地形の元となる標高データセットが変更され、[実施日時のお知らせ](https://www.data.jma.go.jp/suishin/oshirase/pdf/20250509.pdf)により2025年5月20日00UTC初期値から適用されています。このリポジトリはモデル地形版`2025-05-20`だけを受け入れます。次のモデル仕様変更時は、対応版をコードレビューで明示的に追加します。

## 取得状況と利用条件

2026年7月29日、JMBSC配信事業部（`haisin@jmbsc.or.jp`）へ、更新後の`Pzs_FH00`単体サンプルの提供と、元GRIB2をGitHub Private Repositoryへ同梱する条件を照会しました。

回答と正式な利用条件が得られるまで、元GRIB2および確定manifestはリポジトリへ同梱しません。同梱が許可されない場合は代用品へ切り替えず、JMBSCとの契約または別の正規取得・保管経路を再協議します。

以下は代用品として使用しません。

- 公開サンプルZIP内の`Pqc`など、Pzs以外の気象要素
- 等緯度経度格子へ内挿された`TOPO.MSM_5K`
- 外部DEM
- LsurfのMSLPその他の地形ではない要素

地形が用意されていないときは、推定QNHを`MODEL_TERRAIN_UNAVAILABLE`として返します。暗黙のフォールバックはありません。

## Source manifest

[source-manifest.example.json](source-manifest.example.json)を複製し、JMBSCから取得した情報と書面回答に基づいて全placeholderを置換します。manifestには次を記録します。

- 元GRIB2のファイル名、SHA-256、初期時刻
- モデル地形版`2025-05-20`
- 公式取得元と取得日時
- Private Repository同梱可否
- 利用・再配布条件と根拠資料
- 技術情報第619号・第648号

`private_repository_bundling`は`permitted`または`prohibited`です。`permitted`は、JMBSCから同梱許可を得た場合だけ設定します。

## Cache生成

元GRIB2を実行環境へ配置し、次を実行します。

```bash
msm-weather prepare-terrain \
  --input-grib /secure/path/Z__C_RJTD_yyyyMMddhhmmss_MSM_GPV_Rjp_Glm5km_Lm1-39_Pzs_FH00_grib2.bin \
  --source-manifest /secure/path/source-manifest.json \
  --output data/static/model-terrain/v2/terrain.npz
```

検証はSHA-256、Pzs識別値、Lambert格子、初期時刻の順に行われ、不一致の場合はNPZを生成しません。cacheには九州範囲と補間に必要な周辺格子だけを、元のLambert格子のまま保存します。

cache schema v2は公式取得元、元GRIB SHA-256、初期時刻、モデル地形版、source manifest SHA-256、利用条件への参照をNPZ内metadataとJSON sidecarへ保存します。QNH結果には同じprovenanceが含まれます。schema v1 cacheは後方互換のため読み込めますが、v2として再保存せず、公式元GRIBとmanifestから再生成してください。

## 検証

通常の単体試験：

```bash
pytest -q
```

公式Pzsによるcache生成試験：

```bash
MSM_PZS_GRIB=/secure/path/Pzs_FH00_grib2.bin \
MSM_PZS_MANIFEST=/secure/path/source-manifest.json \
pytest -q -m real_data tests/test_real_data.py::test_official_pzs_prepares_finite_kyushu_cache
```

RISH固定Run `2026-07-27 12Z / Lsurf FH00-15`と宮崎空港付近（31.877N、131.449E、標高6m）を使うQNH試験：

```bash
MSM_PZS_GRIB=/secure/path/Pzs_FH00_grib2.bin \
MSM_PZS_MANIFEST=/secure/path/source-manifest.json \
MSM_RUN_REAL_DATA=1 \
pytest -q -m real_data tests/test_real_data.py::test_pinned_rish_run_produces_qnh_with_pzs_provenance
```

固定Runのダウンロードcacheは既定で`data/real-test`に保存されます。`MSM_REAL_DATA_CACHE`で別の保存先を指定できます。
