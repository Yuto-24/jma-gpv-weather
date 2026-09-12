# jma-gpv-weather

JMA GPV向けの取得・cache・補間基盤です。対応モデルはMSMとGSM日本域、取得元はRISH（京都大学生存圏研究所）のみです。GRIB2を取得し、任意地点・時刻・高度の風と気温を問い合わせるPythonライブラリです。AutoNavLogの気象データ基盤として利用でき、従来の日単位CSV出力も維持しています。

> このパッケージと出力値は、運航用の規制・観測・飛行場気象資料を代替しません。

## 対応機能

- 既定範囲：29.7–35.2°N、128.5–134.8°E
- `Bounds`による任意矩形
- 要求全体を覆う最新の完全なForecast Runの選択
- 保存済みRunの固定と、より新しいRunの`UPDATE_AVAILABLE`通知
- HGTを用いた任意MSL高度のU/V/TMP補間
- AGL 0 m要求に対するMSM 10 m AGL地上風
- 地点標高とMSMモデル地形を使う`MSM-derived estimated QNH`
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

## 0.3.0への移行

- distribution: `jma-msm-wind` → `jma-gpv-weather`
- import: `msm_wind` → `jma_gpv_weather`
- Weather CLI: `msm-weather` → `jma-gpv-weather`
- MSM CSV CLI: `msm-wind` → `jma-gpv-msm-csv`

旧distributionをアンインストールし、新distributionをインストールしてimportとCLI呼出しを更新してください。旧module・CLI alias・移行shimは提供しません。`MsmClient`、query/result型とMSMのRun固定、補間、cache key/path、provenance、エラー契約は維持しています。内部moduleの配置は[責務と拡張境界](docs/architecture.md)を参照してください。

0.4.0では`GsmClient`、両モデル共通の`SurfaceTemperatureQuery`（2 m AGL気温）と`AloftTemperatureQuery`（気温のみ）、network不要の`check_coverage()`を追加しました。`AloftQuery`の既存の風・気温結果、MSMの数値・cache path・error契約は維持します。

## Python API

```python
from datetime import datetime, timezone
from jma_gpv_weather import (
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

Weather query（現在はMSMのみ）：

```bash
jma-gpv-weather resolve --time 2026-07-28T03:30:00Z
jma-gpv-weather prepare \
  --time 2026-07-28T03:30:00Z \
  --variable aloft_wind --variable aloft_temperature
jma-gpv-weather query-aloft \
  --time 2026-07-28T03:30:00Z \
  --lat 31.877 --lon 131.449 --altitude-m-msl 4572
jma-gpv-weather query-surface \
  --time 2026-07-28T03:30:00Z \
  --lat 31.877 --lon 131.449
jma-gpv-weather query-qnh \
  --time 2026-07-28T03:30:00Z \
  --lat 31.877 --lon 131.449 --elevation-m-msl 6 \
  --terrain-cache data/static/model-terrain/v1/terrain.npz
```

MSM日単位CSV：

```bash
jma-gpv-msm-csv --date 2026-07-28 --discover-only
jma-gpv-msm-csv --date 2026-07-28 --work-dir data --output-dir outputs
```

既存の`*_surface.csv`、`*_pressure_levels.csv`、`*_15000ft.csv`、`*_to_15000ft.csv`とmetadataの名称・列を維持します。

## キャッシュ

```text
data/
├─ raw/RUN_ID/                 生GRIB2、manifest、SHA-256
├─ normalized/v1/RUN_ID/KEY/  正規化NetCDF
├─ static/model-terrain/v1/   Pzs由来の静的地形
├─ locks/
└─ gsm-japan/                GSM専用root（raw/normalized/listings/locksを内包）
```

CSV CLIは既存の`data/RUN_ID/*.bin`を再利用します。Weather APIは`data/raw/RUN_ID/`を使用します。破損ファイルは削除せず`.corrupt.TIMESTAMP`へ退避します。RISHへのアクセスを集中させないため、ダウンロードは逐次実行します。

## MSM推定QNH

QNH推定には、Lsurfの地上気圧・気温・相対湿度に加え、気象庁MSMモデル地形`Pzs`が必要です。

```bash
jma-gpv-weather prepare-terrain \
  --input-grib /path/to/MSM_GPV_Rjp_Glm5km_Lm1-39_Pzs_FH00_grib2.bin \
  --output data/static/model-terrain/v1/terrain.npz
```

RISHの通常の`gpv/original`一覧にはPzsがないため、公式ソースから別途入手してください。Pzsがない場合、風・気温・地上風は使用できますが、QNHだけが`MODEL_TERRAIN_UNAVAILABLE`になります。外部DEMや海面更正気圧へ暗黙にフォールバックしません。

QNH結果には必ず`MSM-derived estimated QNH`、`ESTIMATED_QNH_NOT_OFFICIAL`、使用地形、地点標高、診断用MSLP、計算方式versionを付与します。

## 出典

- データ作成：気象庁
- 無料配布：京都大学生存圏研究所RISH
- [RISH 気象庁データ](http://database.rish.kyoto-u.ac.jp/arch/jmadata/)
- [JMBSC MSM仕様](https://www.jmbsc.or.jp/jp/online/file/f-online10200.html)

RISHの利用条件を確認し、企業活動等で頻繁に利用する場合は気象業務支援センターからの取得を検討してください。

## 検証

GSMの仕様、coverageの条件付き高度判定、error分類、実データ検証の詳細は[GSM日本域の設計・調査記録](docs/gsm-japan.md)を参照してください。

通常テストはnetworkなしで実行できます。実データ受入は明示的に有効化します。

```bash
pytest -q
JMA_GPV_REAL_MSM=1 JMA_GPV_REAL_CACHE=data/acceptance pytest -q -s tests/test_real_msm.py
JMA_GPV_REAL_GSM=1 JMA_GPV_REAL_CACHE=data/acceptance pytest -q -s tests/test_real_gsm.py
python -m pip install build
python -m build
```

実データ受入はRISHの固定Run `2026-07-27 12Z`で、上空風・上空気温・地上風・地上気温、時空間補間、SHA-256/provenance、warm cache再利用を確認します。有効化後の通信・decode失敗はskipせず失敗になります。公式Pzsを用いたQNH受入は含みません（Issue #6）。Issue #8のTOPO opt-inもmainには未導入であり、今回の再編には取り込みません。

## GSM日本域とoffline coverage

```python
from jma_gpv_weather import (
    GsmClient, CoveragePoint, CoverageState, SurfaceTemperatureQuery,
    AloftTemperatureQuery,
)

client = GsmClient(cache_dir="data")
coverage = client.check_coverage(
    requirements,
    points=(CoveragePoint(31.877, 131.449, altitude_msl_m=4572),),
)
# OUTSIDE_SPECだけが確定した仕様外。REQUIRES_HGTは仕様外ではありません。
# 圧力面のMSL高度は気象状態で変わるため、download前には保証しません。
print(coverage.state, coverage.reason_codes, coverage.candidate_runs)

status = client.resolve_run(requirements)
forecast = client.prepare_run(status.selected_run, requirements)
aloft = forecast.query(AloftQuery(31.877, 131.449, valid_time, 4572))
```

地上気温を取得する場合は`ForecastRequirements.variables`に
`WeatherVariable.SURFACE_TEMPERATURE`を含め、
`forecast.query(SurfaceTemperatureQuery(latitude, longitude, valid_time))`を使用します。
気温だけの上空queryには`ALOFT_TEMPERATURE`と`AloftTemperatureQuery`を使用します。
どちらもMSM/GSM共通の公開APIです。地上風・推定QNHは引き続きMSMのみです。

`check_coverage`は地点・気圧面・変数・予報時刻・Run仕様を確認し、RISHへアクセスしません。
再現可能な判定には`as_of=`（その時点までに初期時刻を迎えたRun）を指定します。
`run=RunId(...)`で固定Runの仕様判定もできます。Run公開完了を保証する引数ではありません。

GSMはlisting失敗を`GsmDiscoveryError`、取得可能な互換Runが見つからない状態を
`GsmRunUnavailableError`、download/decode/cache失敗を`GsmProcessingError`として返します。
これらは`GsmCoverageError`ではありません。MSMの既存`discover_runs()`は互換性を維持するため、
listing失敗を集約する従来動作のままです。MSMの`NoCompatibleRunError`を仕様外へ読み替えず、
独立した`MsmClient.check_coverage()`を使用してください。モデル自動切替は実装していません。
