# 湿度・層別雲量API（0.7.0 / Issue #22）

[NavMate #374の検証結果](https://github.com/Yuto-24/NavMate/blob/main/docs/wx_profile_feasibility.md)
に基づき、相対湿度のnative気圧面プロファイルと層別雲量を公開する。
RH閾値による雲判定、鉛直方向の穴埋め、雲底・雲頂の高度は生成しない。
既存のNAV LOG用風・気温とWXは、別々の`ForecastRequirements`とsnapshotで取得できる。

## 採用した要素と補間

根拠は気象庁の[MSM仕様No.12601](https://www.data.jma.go.jp/suishin/shiyou/pdf/no12601)と
[GSM仕様No.12501・日本域の別紙2](https://www.data.jma.go.jp/suishin/shiyou/pdf/no12501)。
2026-11-26に一次資料と、先行検証と同じ2026-09-12 00 UTCの実GRIBを照合した。

| 要素 | MSM / GSM日本域の契約 |
| --- | --- |
| 相対湿度 | GRIB category 1 / number 1、単位%。1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300 hPa |
| 高度 | 同じ気圧面・モデル・RunのHGT（3 / 5）、単位gpm。幾何高度や機上気圧高度とは同一視しない |
| 地上気圧 | 地上のSP（3 / 0）、Pa。補間したSPより高圧の面をモデル地表下として除外 |
| 雲量 | 全・下・中・上層（6 / 1,3,4,5）、単位%。気圧面ごとの雲量ではなく層の集約値 |
| 水平 | 各product固有の矩形格子で双線形補間。MSMの地上格子と気圧面格子は混ぜない |
| 時刻 | 公式の隣接端点で線形補間。MSMは地上1h/気圧面3h、GSMはFH132まで同じ、以降3h/6h |
| 鉛直 | 補間なし。RHとHGTの同じ気圧面値を返す。300 hPaより上のRHは対応外 |

全雲量の`shortName`と`units`がpygrib 2.1.8では`unknown`になる実ファイルがある。
新要素はJMAの数値パラメータと面の種類で識別し、単位は上記仕様による。
雲量を固定MSL高度帯に変換しない。モデル・Run・予報時間・領域は既存の選択規則を使う。
MSMの上空風・気温は従来の1000–500 hPa、GSMは1000–100 hPaを維持する。

地表下判定は問い合わせ地点・時刻へ補間したSPによる。各気圧面格子点の地形混合の精査、
DEMによる地表判定、観測雲高による精度検証は含まない。

## 公開API

```python
from datetime import datetime, timezone
from jma_gpv_weather import (
    CloudCoverQuery, CloudLayer, ForecastRequirements, HumidityProfileQuery,
    MsmClient, MsmPreparedData, RunId, WeatherVariable,
)

valid = datetime(2026, 9, 12, 1, 30, tzinfo=timezone.utc)
requirements = ForecastRequirements((valid,), frozenset({
    WeatherVariable.PRESSURE_RELATIVE_HUMIDITY,
    WeatherVariable.CLOUD_COVER,
}))
client = MsmClient(cache_dir="data")
# NAV LOGなどで固定したRunをそのまま使う。新Runへ暗黙に切り替えない。
selected = RunId(datetime(2026, 9, 12, tzinfo=timezone.utc))
forecast = client.prepare_run(selected, requirements)

profile = forecast.query(HumidityProfileQuery(31.877, 131.449, valid))
for level in profile.levels:
    print(level.availability, level.values, level.reason_code)
clouds = forecast.query_many(
    CloudCoverQuery(31.877, 131.449, valid, layer) for layer in CloudLayer
)

payload = MsmPreparedData.from_forecast(forecast).to_bytes()
data = MsmPreparedData.from_bytes(payload)  # 配信時はexpected_sha256も指定
offline = client.prepare_run(selected, requirements, prepared_data=data)
assert offline.query(HumidityProfileQuery(31.877, 131.449, valid)) == profile
```

GSMでは`GsmClient` / `GsmPreparedData`を使う。`prepared_data`指定時は、既存どおり
通信・GRIB decode・NetCDF・file lock・cache作成を行わない。PyodideはNumPyとtzdataだけで動く。
`resolve_run`・取得済みlistingからの`discover_runs`・更新通知も同じAPIを使う。

湿度だけの要求ではRH/HGT/SP、雲量だけなら4種類の雲量とLsurfのみを準備する。
WXが不要な要求は新要素をdecodeせず、旧キャッシュ経路・計算値・provenanceを維持する。
WXと従来変数の混合要求も可能だが、独立assetにするとWX取得の失敗をNAV LOGと分離しやすい。
上空風を要求した場合は、従来の`AloftQuery`の付随気温と高度coverageに必要なTMPも保持する。

`HumidityProfileQuery.pressure_levels_hpa`は既定で12面。任意の部分集合を指定でき、
例えば`(850, 300, 250)`では250 hPaだけが`PRESSURE_LEVEL_UNSUPPORTED`になる。
正の整数を重複なしで最大16面指定できる。native面の間の値は推定しない。

`HumidityProfileResult.levels`は各面の`WeatherResult`。`values`には`pressure_hpa`、
`relative_humidity_percent`、`geopotential_height_gpm`を持ち、欠測成分は`None`になる。
HGTが有効でRHだけ欠測ならHGTは保持する。SP/HGTの欠測で面の利用可否を判断できない場合も
面の`availability`は`unavailable`であり、数値があるだけで描画可能とはみなさない。
地表下ではRHを`None`にする。親profileは1面以上有効なら`available`、一部無効なら
`PROFILE_PARTIALLY_UNAVAILABLE`、全て無効なら`unavailable`となる。
各面の状態を必ず確認する。

`CloudCoverQuery.layer`は`CloudLayer.TOTAL/LOW/MIDDLE/HIGH`。
応答は`cloud_cover_percent`と`layer`を持つ`WeatherResult`。雲底・雲頂の数値は返さない。

## 状態・失敗・provenance

| 状態 | 意味 |
| --- | --- |
| `OUTSIDE_DOMAIN` / `TIME_OUTSIDE_COVERAGE` | モデル仕様の領域/Runの予報時間外 |
| `PRESSURE_LEVEL_UNSUPPORTED` | RHが仕様上存在しない面。欠測と区別 |
| `LOCATION_NOT_PREPARED` | モデル領域内だがsnapshotの格子範囲外 |
| `DATA_NOT_PREPARED` | 公式補間端点のrecordがsnapshotにない。遠い時刻で代用しない |
| `MISSING_SOURCE_VALUE` | 必要な格子値にNaN/非有限値がある。0%にしない |
| `INVALID_SOURCE_VALUE` | RH/雲量が0–100%外、または地上気圧が非正値。clampしない |
| `BELOW_MODEL_SURFACE` | 問い合わせ地点のSPより高圧の面。利用可能な大気として返さない |

構造上必要なrecordがないデータを要求して`prepare_run`した場合は、従来の
`MissingVariableError`（MSM）/ `GsmProcessingError`となる。
prepared済みforecastに後から未準備の時刻/変数をqueryした場合は`DATA_NOT_PREPARED`。
これらは、値配列が存在して一部または全部NaNの場合と異なる。

モデル未対応変数はoffline `check_coverage`の`UNSUPPORTED_VARIABLE`。
RH要求の`pressure_levels_hpa`は12面、RHと風・気温を混合したcoverageの面数は共通部分になる。
既存MSMのdiscoveryエラー体系、GSMのcoverage/discovery/run-unavailable/processingの区別は変更しない。
通信・decode・不正payload・hash不一致をcoverage外や晴天へ変換しない。

provenanceには初期時刻、対象の端点を含む元GRIBのURL/SHA-256、モデル、単位、
`wx_contract_version=1`、補間に使った時間端・格子座標・気圧面を記録する。
雲量queryにはLsurf由来のみ、湿度profileにはRH/HGTとSPの由来を付ける。
元GRIBのhashと配布snapshotのhashは別物である。

## Portableと正規化キャッシュ

- WX recordを含むsnapshotだけNPZ schema 2。モデル名と既存record表現は維持する。
- 新readerはschema 1/2を読み、schema 1にRH/雲量などを偽装して入れたpayloadは拒否する。
- 0.6.0以前のreaderはschema 2を拒否する。新データを旧readerへ黙って渡さない。
- WXを含まないsnapshotはschema 1で出力し続ける。旧snapshotは風・気温用途で利用できるが、
  RHを準備できるデータとはみなさない。
- WX normalized cacheは`normalized/v4-wx-<要求変数>/RUN/KEY/weather.nc`。
  TMP依存の修正前に作られた`v3-wx-*`は再利用せず、同じ要求・source hashでも再デコードする。
  キャッシュnamespaceの更新であり、NetCDF schema 3とportable schema 2は変更しない。
  ソースhashとファイルhashを照合し、実在するrecordのinventoryを保存する。
  旧normalized cacheの「全NaNのfieldを復元しない」挙動をWXへ流用しない。
- raw cacheとRISHは従来どおり。snapshotのtransport/storage/retentionは利用側の責務。

schema 2はproducer/consumerとも、圧縮32 MiB、2048 ZIP members、各member展開32 MiB、
展開合計128 MiB、metadata 1 MiBを検査する。旧codecの上限を拡大しない。
member/NPY headerの検査、`allow_pickle=False`、source/model/Run/格子検証も維持する。

WX-onlyのrecord数は`24 × 気圧面時刻数 + 5 × 地上時刻数`、ZIP member数は共有座標4枚と
metadataを加えて`24P + 5S + 5`。GSM 264h全体は2498 membersとなり、一つに詰められない。
呼出側で12時間などのwindowを`ForecastRequirements`に分割する。同じRunを指定し、境界時刻を
含めればlibraryが公式の補間端点を準備する。chunk索引と本番配信はNavMate #375の範囲。

## 実GPVのサイズ・メモリ測定

[数値と入力hash](evidence/issue-22/measurements.json)は、2026-09-12 00 UTCのRISH 4ファイル
（計184,591,261 bytes）を使った2026-11-26の検証。
既定Boundsとlibraryの補間用余白、0–12hの13時間端点・5気圧面端点、float64/NPZ。
MSM地上115×105点、気圧面58×53点。GSMは両方58×53点。

| モデル / snapshot | 圧縮MiB | 展開配列MiB | members | 読込ms | RSS読込前 / peak MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| MSM 従来要素 | 2.607 | 12.107 | 283 | 87.8 | 44.34 / 66.34 |
| MSM WXのみ | 1.788 | 9.034 | 190 | 65.7 | 44.08 / 62.33 |
| MSM 混合要求 | 2.912 | 13.749 | 353 | 99.2 | 44.12 / 68.25 |
| GSM 従来要素 | 1.867 | 9.428 | 403 | 95.8 | 43.78 / 62.78 |
| GSM WXのみ | 0.967 | 4.433 | 190 | 54.9 | 44.57 / 57.32 |
| GSM 混合要求 | 2.251 | 10.835 | 463 | 108.2 | 44.25 / 65.00 |

従来要素の要求は上空風・上空気温・地上気温。旧decoderは付随要素も保存する一方、
混合要求は明示した要素だけを保存するため、差分を単純なWX追加量とみなさない。
独立WX assetの追加コストは「WXのみ」の行で判断する。

Linux/CPython 3.11.15、NumPy 2.4.6、pygrib 2.1.8。各snapshotは独立プロセスで1回測定。
NumPy/library import後のファイル読込・NPZ検査・全配列展開・model検証までを計時する。
RSSは`/proc/self/status`のVmRSS/VmHWM。配列量は共有座標の重複を除く。
転送、Pyodide起動、query、描画は計時外。warm/cold GRIB準備の性能比較ではない。
数値は当該環境・単一Runの観測で、ブラウザー総RAM・iPhone/iPad Safariの成立性を保証しない。

```sh
python scripts/measure_wx.py --output /tmp/jma-wx
```

このスクリプトは原ファイルのサイズ/hash、独立pygribによる850 hPa RH/HGTと4雲量、
cold/warm/portableの値とprovenance、混合要求での既存風・気温値も照合する。
予報期間全体や他のRunを測定したものではない。

## 自動検証

`pytest -q`は既存回帰に加え、RH/雲量の補間、地表下、NaN/不正値、未準備・仕様外、
独立要求、GSM FH132境界、portable schema/上限、旧データ、cold/warm cacheを検証する。
`tests/runtime_case.py`と`npm test --prefix tests/pyodide`はMSM/GSMのWX fixtureも
Node/ChromiumのPyodide 0.27.7で読み、desktopと数値・全provenanceを照合する。
物理iPhone/iPadと本番Static Feedの受入は後続作業である。

2026-11-26の初回ローカル検証では、editableとビルド済みwheelで各268件成功、既存のopt-in実通信試験2件skip。
上記スクリプトの実GPV検証は別途両モデルで成功した。
NodeとLinux ChromiumのPyodide 0.27.7/CPython 3.12.7で、既存11 queryと
新WXの16 query（各モデル8件）、全provenance、NaNとportable再保存の一致を確認した。
CodeRabbit初回レビューで指摘された「風＋WX、気温を明示しない要求」の付随TMP欠落は、
両モデルで修正前に再現する回帰テストを追加して修正した。
修正後はeditable・wheelとも270件成功、同じopt-in 2件skip。
続いて、TMPを欠く旧`v3-wx-*`キャッシュ（要求・source hash・file hashが有効）を再現し、
両モデルで修正前の再利用による付随気温の欠落を確認した。namespace更新後は温度・高度coverage・
provenanceが回復し、再生成したキャッシュは次回prepareで再デコードせず利用できる。
この修正後はeditable・wheelとも272件成功、同じopt-in 2件skip。
