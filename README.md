# JMA MSM weather query foundation

RISH（京都大学生存圏研究所）のJMA MSM GRIB2を取得し、任意地点・時刻・高度の風と気温を問い合わせるPythonライブラリです。AutoNavLogの気象データ基盤として利用でき、従来の日単位CSV出力も維持しています。

> このパッケージと出力値は、運航用の規制・観測・飛行場気象資料を代替しません。

## 対応機能

- 既定範囲：29.7–35.2°N、128.5–134.8°E
- `Bounds`による任意矩形
- 要求全体を覆う最新の完全なForecast Runの選択
- 保存済みRunの固定と、より新しいRunの`UPDATE_AVAILABLE`通知
- HGTを用いた任意MSL高度のU/V/TMP補間
- AGL 0 m要求に対するMSM 10 m AGL地上風
- 地点標高とMSMモデル地形を使う`MSM-derived estimated QNH`
- Pzs由来cacheと、明示opt-inの実験的`TOPO.MSM_5K`を分離した地形provider
- 元URL、SHA-256、格子点、気圧面、補間方式のprovenance
- `.part`再開、atomic write、ファイルlock、raw/NetCDFキャッシュ

風はU/V成分を鉛直、水平、時間方向に補間した後で、気象学上の「吹いてくる方向」と風速へ変換します。10 m風を気圧面風と自動接続せず、指定高度を気圧面HGTで挟めない場合は`unavailable`です。

## セットアップ

Python 3.10–3.12をサポートし、3.12を主対象とします。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
pytest -q
```

`pygrib`が利用できない環境ではecCodesをOSまたはCondaで導入してください。

## Python API

```python
from datetime import datetime, timezone
from msm_wind import (
    AloftQuery,
    ForecastRequirements,
    MsmClient,
    RunId,
    WeatherVariable,
)

valid_time = datetime(2026, 7, 28, 3, 30, tzinfo=timezone.utc)
requirements = ForecastRequirements(
    valid_times=(valid_time,),
    variables=frozenset({
        WeatherVariable.ALOFT_WIND,
        WeatherVariable.ALOFT_TEMPERATURE,
    }),
)

client = MsmClient(cache_dir="data")
status = client.resolve_run(requirements, selected_run=None)
forecast = client.prepare_run(status.selected_run, requirements)
result = forecast.query(
    AloftQuery(31.877, 131.449, valid_time, altitude_msl_m=4572)
)
```

2回目以降は保存した`RunId`を`selected_run`へ渡してください。より新しい互換Runがあってもselected Runは変更されず、`update_available=True`だけが返ります。最新Runを使う場合のみ、呼出側が`latest_compatible_run`を明示的に`prepare_run`へ渡します。

## CLI

新API：

```bash
msm-weather resolve --time 2026-07-28T03:30:00Z
msm-weather prepare \
  --time 2026-07-28T03:30:00Z \
  --variable aloft_wind --variable aloft_temperature
msm-weather query-aloft \
  --time 2026-07-28T03:30:00Z \
  --lat 31.877 --lon 131.449 --altitude-m-msl 4572
msm-weather query-surface \
  --time 2026-07-28T03:30:00Z \
  --lat 31.877 --lon 131.449
msm-weather query-qnh \
  --time 2026-07-28T03:30:00Z \
  --lat 31.877 --lon 131.449 --elevation-m-msl 6 \
  --terrain-cache data/static/model-terrain/v1/terrain.npz
```

従来互換：

```bash
msm-wind --date 2026-07-28 --discover-only
msm-wind --date 2026-07-28 --work-dir data --output-dir outputs
```

既存の`*_surface.csv`、`*_pressure_levels.csv`、`*_15000ft.csv`、`*_to_15000ft.csv`とmetadataの名称・列を維持します。

## キャッシュ

```text
data/
├─ raw/RUN_ID/                              生GRIB2、manifest、SHA-256
├─ normalized/v1/RUN_ID/KEY/               正規化NetCDF
├─ static/model-terrain/v1/                Pzs由来の静的地形
├─ static/interpolated-model-terrain/v1/   実験的TOPO.MSM_5K
└─ locks/
```

既存の`data/RUN_ID/*.bin`も再利用できます。破損ファイルは削除せず`.corrupt.TIMESTAMP`へ退避します。RISHへのアクセスを集中させないため、ダウンロードは逐次実行します。

## MSM推定QNH

QNH推定には、Lsurfの地上気圧・気温・相対湿度に加え、MSMモデル地形が必要です。従来のPzs由来cacheは次のように生成します。

```bash
msm-weather prepare-terrain \
  --input-grib /path/to/MSM_GPV_Rjp_Glm5km_Lm1-39_Pzs_FH00_grib2.bin \
  --output data/static/model-terrain/v1/terrain.npz
```

RISHの通常の`gpv/original`一覧にはPzsがないため、公式ソースから別途入手してください。Pzsがない場合、風・気温・地上風は使用できますが、QNHだけが`MODEL_TERRAIN_UNAVAILABLE`になります。外部DEMや海面更正気圧へ暗黙にフォールバックしません。

JMBSCがCC BY 4.0で公開する`TOPO.MSM_5K`は、Pzsとは別の実験的providerとして明示指定時だけ使用できます。公式配布ZIPからouter/inner archiveと2つのartifactのSHA-256を検証してcacheを生成します。

```bash
msm-weather prepare-interpolated-terrain \
  --distribution-archive /path/to/chikeidata_joho648.zip \
  --source-manifest manifests/topo-msm-5k-2025-05-20.json \
  --output data/static/interpolated-model-terrain/v1/terrain.npz

msm-weather query-qnh \
  --time 2026-07-27T12:00:00Z \
  --lat 31.877 --lon 131.449 --elevation-m-msl 6 \
  --interpolated-terrain-cache \
  data/static/interpolated-model-terrain/v1/terrain.npz
```

`--terrain-cache`と`--interpolated-terrain-cache`は排他的です。形式を自動判定せず、Pzs取得失敗時のfallbackにもなりません。後者のQNHには`INTERPOLATED_MODEL_TERRAIN`を必ず付け、`LANDSEA`の補間値が0.05より大きく0.95未満なら`COASTAL_MIXED_LAND_FRACTION`も付けます。

QNH結果には必ず`MSM-derived estimated QNH`、`ESTIMATED_QNH_NOT_OFFICIAL`、使用地形、地点標高、診断用MSLP、計算方式versionを付与します。

- [TOPO.MSM_5Kの実験的opt-in方針](docs/interpolated-terrain.md)

## 出典

- データ作成：気象庁
- 無料配布：京都大学生存圏研究所RISH
- [RISH 気象庁データ](http://database.rish.kyoto-u.ac.jp/arch/jmadata/)
- [JMBSC MSM仕様](https://www.jmbsc.or.jp/jp/online/file/f-online10200.html)
- [気象庁 技術情報第648号](https://www.data.jma.go.jp/suishin/jyouhou/pdf/648.pdf)
- [JMBSC 地形データ配布](https://www.jmbsc.or.jp/jp/online/c-onlineGsd.html)

RISHの利用条件を確認し、企業活動等で頻繁に利用する場合は気象業務支援センターからの取得を検討してください。
