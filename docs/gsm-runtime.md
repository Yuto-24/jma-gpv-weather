# GSM日本域のLocal / Pyodide公開境界（0.6.0 / Issue #18）

[Issue #18](https://github.com/Yuto-24/jma-gpv-weather/issues/18)は
[AutoNavLog #161](https://github.com/Yuto-24/AutoNavLog/issues/161)から必要になった公開境界を追加する。
GSMの仕様、Run選択、時間・水平・HGT補間とprovenanceはこのライブラリが所有する。
呼出側は取得済みlistingと準備済みsnapshotのtransport・storage、およびMSM/GSM選択policyを所有する。
ブラウザー内の生GRIB decoder、RISHへのCORS proxy、モデル自動fallbackは追加しない。

## Producerとconsumer

Desktopでは既存のGSM取得・GRIB decode・NetCDF cacheを使用する。

```python
from jma_gpv_weather import GsmClient, GsmPreparedData

client = GsmClient(cache_dir="data")
status = client.resolve_run(requirements)
forecast = client.prepare_run(status.selected_run, requirements)
payload = GsmPreparedData.from_forecast(forecast).to_bytes()
```

Consumerでは同じパッケージの公開APIを使用する。

```python
from jma_gpv_weather import GsmClient, GsmPreparedData, RunId, AloftQuery

client = GsmClient(cache_dir="unused")
data = GsmPreparedData.from_bytes(payload, expected_sha256=manifest_sha256)
selected = saved_run or RunId(data.selection.run_utc)
forecast = client.prepare_run(selected, requirements, prepared_data=data)
result = forecast.query(AloftQuery(latitude, longitude, valid_time, altitude_msl_m))
altitude = forecast.check_altitude_coverage(
    AloftQuery(latitude, longitude, valid_time, altitude_msl_m)
)
```

`prepared_data`指定時はnetwork、GRIB、NetCDF、file lock、cache directory作成を行わない。
PyodideではNumPyとtzdataが必要。通常のwheel installでemscripten依存markerが適用される。
MSMと同じ[ランタイムセットアップ](msm-runtime.md)を使える。
Desktop依存・cache layout・通常の`prepare_run`経路は変更しない。

## 取得済みlisting、固定Run、更新通知

```python
urls = client.listing_urls(requirements, as_of=as_of)
# 各URLをruntime transportで取得する。未取得を空文字へ置換しない。
runs = client.discover_runs(requirements, as_of=as_of, listings=text_by_url)
status = client.resolve_run(requirements, selected_run=selected, available_runs=runs)
forecast = client.prepare_run(
    status.selected_run, requirements, available_runs=runs, prepared_data=data,
)
```

`listing_urls`はGSM仕様から必要な日付を算出し、`source.directory_url(day)`を使用する。
`as_of`を使う場合は両呼出しで同じ値を渡す。指定時点の公開完了を保証する引数ではない。
`listings`は要求URLすべてを文字列へ対応させるmapping。欠落・不正値は`GsmDiscoveryError`。
既知の空directoryは空文字で表せる。完全なlistingに互換Runがなければ
`GsmRunUnavailableError`となる。どちらもcoverage除外ではない。

`available_runs`を省略したportable prepareはsnapshotの元fileからGSM互換性を判定する。
別Runへ自動変更せず、新しいRunの有無もsnapshotだけでは判断しない。
更新通知にはlistingから求めた`available_runs`を`resolve_run`へ渡す。

## データ・失敗契約

`GsmPreparedData(selection, surface, pressure, source_hashes)`はGSMのdecoded recordを保持する。
通常は`from_forecast` / `to_bytes` / `from_bytes`で受け渡す。

- record keyはawareなvalid time、整数level、variable。valueは同一shapeの2-D NumPy
  `(values, latitude, longitude)`。矩形・単調な日本域格子をproduct内で統一する。
- pressureはGSMの既存1000–100 hPaの16面、HGT（m）、U/V（m/s）、TMP（K）。
  surfaceはsnapshot元に存在する0/2 m気温と補助fieldを保持する。GSM queryは格納順に依存せず2 m気温のみを使う。
  Native normalized cacheは従来の単一surface level表現で2 m値を保存するため、warm snapshotに0 m補助記録が残る保証はない。
  GSMは`normalized/v2-surface-2`を使い、0/2 mを区別しなかった旧v1 cacheを再利用しない。MSM cacheは変更しない。
  0 mレコードだけでは不足fieldの処理エラーとなる。GSM地上風/QNH APIは追加しない。
- 時間端は選択RunとproductのGSM仕様を用いる。FH132以降のL-pall 6時間、Lsurf 3時間への
  切替も既存GSM APIと同じ。MSMの予報間隔を流用しない。
- 元fileのGSM model、Run、product、時刻範囲とrecordを照合し、全source URLにSHA-256を要求する。
  元GRIB hashはprovenanceであり、snapshot自体のhashとは別。信頼する外部manifestの
  `expected_sha256`で取得payloadを検証する。
- pickle/object/masked arrayは拒否し、欠測NaNは保持する。要求field・時間端・気圧面の不足は
  `GsmProcessingError`。破損、foreign model、source/hash/grid不一致はその派生型
  `GsmCacheIntegrityError`。別Runのpayloadで固定Runを置き換えない。
- `check_coverage`の`REQUIRES_HGT`は確定除外ではない。prepare後の
  `ALTITUDE_OUTSIDE_HGT_RANGE`だけが正常なHGTによる高度除外。
  `SOURCE_VALUE_UNAVAILABLE`は欠測・不正値による判定不能。
  listing、取得可能Run不足、cache、decode、snapshot失敗を`GsmCoverageError`へ変換しない。
- snapshotの格子がprepared areaを定め、consumerの`bounds`による再decode/再切出しはしない。
  要求を狭めると採用source fileとrecord、provenanceを絞る。元snapshotは変更しない。
  追加field・時間端が必要なら新たなsnapshotが必要で、元file全体が準備済みとは仮定しない。

形式はNPZ schema 1、modelは`GSM_JAPAN`。MSM snapshotとは相互に受け付けない。
MSMと共通のbounded ZIP/NPY codecを使用し、`np.load`前に圧縮32 MiB、2048 member、
各member非圧縮32 MiB、合計128 MiB、metadata 1 MiBを検査する。
NPY headerのshape/dtypeも検査し、偽装された巨大allocationを拒否する。
詳細なarchive制約は[MSMの検証契約](msm-runtime.md#data--validation-contract)と同じ。
地域・時刻を絞ったsnapshotの上限であり、process全体のpeak memoryの保証ではない。
大きな入力はproducerで分割する。受信前サイズ制限、storageの原子性・quota・evictionは呼出側が所有する。

## 検証

```bash
python -m pip install -e ".[test]" build
pytest -q
python -m build
python tests/runtime_case.py .runtime-case
npm ci --prefix tests/pyodide
npm exec --prefix tests/pyodide -- playwright install --with-deps chromium
npm test --prefix tests/pyodide
```

GeneratorはMSMとGSMの両fixtureを作る。GSM fixtureはsynthetic GRIB decodeから通常の
Desktop prepare/cacheを経由し、FH13.5とFH133.5で上空風・上空気温・地上気温を検証する。
Node / Chromium Pyodide 0.27.7で同じ公開APIを呼び、数値（rel_tol 1e-12、abs_tol 1e-10）、
Run固定・更新通知、coverage、HGT判定、source hashを含む全provenanceを照合する。
Native module、weather network、desktop cacheなしで実行し、破損・foreign model・不足fieldも確認する。
Runtime依存のsetupには通信が必要だが、Chromiumの外部requestは遮断する。
実RISH取得の性能・production payloadのサイズ・物理端末のmemory安定性はこのfixture検証の対象外。

2026-09-25の初回検証：Python 3.12のeditable / wheel installで各235件成功、
実RISH opt-in 2件skip。sdist・wheel buildと両CLI help成功。
Review修正後のwheel再検証は236件成功、同じopt-in 2件skip。共通decoderの
0m surface-temperature記録もcodecは受理し、GSM prepareが必要な2m fieldの不足として
拒否することをDesktopと両Pyodide runtimeで確認した。破損とは分類しない。
Node / Linux ChromiumのPyodide 0.27.7（CPython 3.12.7）で、
54,518 bytesのGSM snapshotから6 queryと全provenance・Run status・coverageが一致した。
同時にMSMの既存5 queryも成功した。

追加review修正後はPython 3.12で239件成功、実RISH opt-in 2件skip。
0 m/2 mの異なる気温が共存する55,949 bytesのsnapshotで、格納順を変えても
DesktopとNode / Chromium Pyodideの2 m queryが一致した。Native cold / warm cacheの
公開query・provenanceも一致し、warm時の再decodeがないことを回帰テストで確認した。

同日の追加実データ検証：`JMA_GPV_REAL_GSM=1 pytest -q -s tests/test_real_gsm.py`
が成功（338.60秒）。RISHの20260912000000 Run、8 GRIBファイルを取得し、
FH0/1.5/131.5/133.5/264のquery・時間端・HGT coverage・source hash、
直接pygribで読んだ850 hPaと2 m気温の一致、cold/warm cacheの一致を確認した。
これは宮崎周辺の小範囲に対するNative検証で、全域portable payloadや物理端末の保証ではない。
