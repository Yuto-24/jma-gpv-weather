# MSMのruntime境界（0.5.0 / Issue #16）

## 選定と検証対象

基準はmain `9d806a1`（0.4.0）と[Issue #16](https://github.com/Yuto-24/jma-gpv-weather/issues/16)。
下流の[AutoNavLog #144](https://github.com/Yuto-24/AutoNavLog/issues/144)が使用する
Pyodide **0.27.7 / CPython 3.12 / NumPy 2.0.2**を対象に、NodeとChromiumで検証する。

| desktop pathの要素 | Pyodideの制約 / 今回の扱い |
| --- | --- |
| `pygrib` | 0.27.7の配布packageにない。native CPython拡張とecCodesをdesktop wheelからそのまま移植できない。実runtimeでもimport不能。 |
| `fcntl.flock` | Pyodideでは`fcntl`自体が除去されている。既存listing/raw/normalized cacheをそのまま呼べない。 |
| `urllib` / `RishSource` | BrowserのHTTPはFetch/XHRとCORS等の制限を受ける。desktopのsocket、timeout、Range/retry挙動が同一とは仮定しない。 |
| `Path` | Pyodideの仮想FSで使えるが、永続storageやOSの排他lockと同義ではない。 |
| NumPy | 対象runtimeにWASM buildがある。既存補間とportable snapshotの読書きに使う。 |
| `ZoneInfo("Asia/Tokyo")` | timezone databaseが標準では入っていない。実runtimeでimport失敗を確認し、Pyodide向けに`tzdata`を宣言。既存JSTの意味を維持する。 |
| xarray / h5netcdf / h5py | prepared-data経路では不要。h5pyはPyodide配布にあるが、NetCDF stackの追加導入を要求しない。desktopの通常依存は維持。 |

根拠：Pyodideの[0.27.7 package一覧](https://pyodide.org/en/0.27.7/usage/packages-in-pyodide.html)、
[標準library・HTTPの制限](https://pyodide.org/en/0.27.7/usage/wasm-constraints.html)、
[対応wheelの説明](https://pyodide.org/en/0.27.7/usage/loading-packages.html)。
対応はこのversionで検証するもので、将来の全Pyodide versionへの保証ではない。

既存コードでは`prepare_run`がraw取得、GRIB decode、NetCDF cache、必要field検証を呼び、
最終的にNumPy recordから`PreparedForecast`を構築していた。query実装は既にnative decoderに
依存していないため、この準備済みデータの境界を公開する方式を選んだ。
独自GRIB/WASM実装や別の気象formatへの変換は、このGoalを満たすためには不要。
NumPy NPZは既存recordの可搬な保存に使い、新しい気象モデル・GRIB解釈は導入しない。

これは完成した**libraryのprepared-data接続経路**であり、生GRIBを直接取得・decodeする
Browser Weather Adapterではない。生GRIBからのproduction供給経路、RISHのHTTPS/CORS、
Windows Chromiumと物理iPhone/iPadでの実MSM acceptanceは#144に残る。
この境界だけで、任意の新しいRunのデータがBrowserに自動生成されることはない。

## Desktop producer

従来の`MsmClient`を使用し、GRIBの意味・単位・格子・HGT・必要fieldをlibraryに解釈させる。
fixture、配布済みartifact、runtime間の受け渡しに同じ公開APIを使える。HTTP serviceを
必須とせず、生成済みsnapshotは静的fileとして運べる。生成や配布のserviceは本変更で追加しない。

```python
from datetime import datetime, timezone
from pathlib import Path
import hashlib
from jma_gpv_weather import (
    Bounds, ForecastRequirements, MsmClient, MsmPreparedData, WeatherVariable,
)

requirements = ForecastRequirements(
    (datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc),),
    frozenset({WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
               WeatherVariable.SURFACE_TEMPERATURE}),
)
client = MsmClient("data", Bounds(31.8, 31.95, 131.35, 131.55))
runs = client.discover_runs(requirements)
status = client.resolve_run(requirements, available_runs=runs)
forecast = client.prepare_run(status.selected_run, requirements, available_runs=runs)
payload = MsmPreparedData.from_forecast(forecast).to_bytes()
Path("msm-prepared.npz").write_bytes(payload)
digest = hashlib.sha256(payload).hexdigest()  # payloadと別に保存・配布
```

`from_forecast`は独立したsnapshotを作る。元forecastの配列を後から変更してもsnapshotへ
影響しない。地形provider（callable）はserializeしない。必要ならconsumerが明示的に
`prepare_run(..., terrain_provider=...)`へ渡す。未指定QNHは従来の
`MODEL_TERRAIN_UNAVAILABLE`であり、地形/MSLPへfallbackしない。

## Browser / Pyodide consumer

wheelは通常の依存解決でinstallできる。`deps=False`で未充足依存を隠す必要はない。
`sys_platform == 'emscripten'`では必須依存をNumPyとtzdataにする。その他のplatformでは
既存のNumPy / pygrib / xarray / h5netcdf / h5pyを引き続きinstallする。

```javascript
const pyodide = await loadPyodide({indexURL: "/pyodide/0.27.7/"});
await pyodide.loadPackage("micropip");
await pyodide.runPythonAsync(`
import micropip
await micropip.install("https://your-static-host.example/jma_gpv_weather-0.5.0-py3-none-any.whl")
`);
```

runtime transportが取得した`bytes`をPythonへ渡す（以下の`payload`）。networkやstorageは
呼出側が所有する。Fetch、Cache Storage、IndexedDBをmodel logicへ持ち込まない。

```python
from datetime import datetime, timezone
from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, ForecastRequirements,
    MsmClient, MsmPreparedData, RunId, SurfaceTemperatureQuery, WeatherVariable,
)

data = MsmPreparedData.from_bytes(payload, expected_sha256=expected_digest)
valid = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
requirements = ForecastRequirements((valid,), frozenset({
    WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
    WeatherVariable.SURFACE_TEMPERATURE,
}))
client = MsmClient()
selected = RunId(data.selection.run_utc)  # 保存済みRunがある場合はそのRunIdを使う
forecast = client.prepare_run(selected, requirements, prepared_data=data)
wind_and_temperature = forecast.query(AloftQuery(31.877, 131.449, valid, 1500))
temperature_only = forecast.query(AloftTemperatureQuery(31.877, 131.449, valid, 1500))
surface_temperature = forecast.query(SurfaceTemperatureQuery(31.877, 131.449, valid))
altitude_coverage = forecast.check_altitude_coverage(AloftQuery(31.877, 131.449, valid, 1500))
```

この呼出しではdirectory作成、`fcntl`、raw/NetCDF cache、GRIB decoder、通信は行わない。
`available_runs`を省略した場合もsnapshotの元fileから同じMSM互換性判定を行い、
selected Runを置換しない。**最新Runが何かはsnapshotだけから判断できない。**
更新通知には次のlisting取得を使う。

## Listing取得とRun固定 / update available

既存`DataSource`の`directory_url(day)`を再利用する。MSM file名、必要なproductや時刻の
解釈をconsumer側へ複製しない。

1. `urls = client.listing_urls(requirements)`で必要なdirectory URLを得る。
2. runtime transportで各URLの本文を取得する。必要なら`DataSource`を注入してURLを構成する。
3. 全URLから文字列本文へのmappingを`discover_runs`へ渡す。

```python
runs = client.discover_runs(requirements, listings=acquired_listing_text_by_url)
status = client.resolve_run(requirements, selected_run=saved_run, available_runs=runs)
# status.selected_runはsaved_runのまま。新Runを使うかの判断は呼出側。
forecast = client.prepare_run(
    status.selected_run, requirements, available_runs=runs, prepared_data=data,
)
```

URLに対する取得失敗はtransportのエラーを伝播させる。欠けたentryを空listingへ変換しない。
既知の空directoryは`""`で表せるが、未取得・不正entryは`MsmError`になる。
完全なlistingから互換Runが見つからない場合のみ、従来の`NoCompatibleRunError`になる。
desktop既存経路のlisting cache、失敗集約、`available_runs`の既存解釈は変更しない。

`DataSource`の`read_listing` / `download`を同期的に実装できるruntimeは従来の境界も使えるが、
それだけでdesktopの`fcntl`や`pygrib`が動くわけではない。Pyodideでは取得済みlistingと
`prepared_data`を組み合わせて使用する。

## Data / validation contract

`MsmPreparedData(selection, surface, pressure, source_hashes)`はNumPy配列の直接注入も可能。
model変換を再実装するためのAPIではなく、library producerが生成したrecordを受け渡す境界。
通常は`from_forecast` / `to_bytes` / `from_bytes`を使用する。

- record key：`(timezone-aware valid_time, level, variable)`。
- record value：同じshapeの`(values, latitude, longitude)`という2-D NumPy配列。格子は
  矩形・単調でproduct内で一致する。LsurfとL-pallの格子は別でよい。
- pressure：従来の1000–500 hPa、`hgt`（m）、`u` / `v`（m/s）、`tmp`（K）。
- surface：`u` / `v`は10 m、`tmp_surface` / `rh`はdecoderが記録した0または2 m、
  `sp` / `mslp`はlevel 0。圧力はPa、湿度は%。
- 配列中の欠測はNaNで表す。pickle/object配列やmasked arrayは受け付けない。
- 元fileはMSM file名・URL・Run・product・予報時刻と一致し、全URLに元GRIBのSHA-256が必要。
  snapshotのSHA-256と元GRIBのSHA-256は異なる。元hashは由来を記録するもので、snapshot内に
  生GRIBがないため再計算しない。`expected_sha256`は信頼する外部manifestから受け取る。
- 他モデル、構造破損、source/hash/gridの不一致は`CacheIntegrityError`。
  正常な構造でも要求fieldが不足すれば従来と同じ`MissingVariableError`。
  selected Run/fileの時刻範囲が要求を覆わなければ`SelectedRunCoverageError`。
  他Runのpayloadをselected Runへ取り付けようとした場合は`CacheIntegrityError`。
- 必要なfield、時間端、全ての既存気圧面を同じ`MsmClient`の検証で確認する。
  NaNは埋めない。post-prepare altitude coverageではデータ欠測を
  `SOURCE_VALUE_UNAVAILABLE`、正常なHGT範囲外を`ALTITUDE_OUTSIDE_HGT_RANGE`と区別する。

snapshotの格子がprepared areaを定義する。consumerの`MsmClient.bounds`で再decodeや
再切出しは行わない（`bounds`はdesktop取得時の矩形）。範囲外queryは既存のunavailableになる。
同じsnapshotの範囲内で別地点・時刻をqueryできるが、追加の時間端やfieldが必要なら再prepareする。
snapshotだけで元fileに存在する全時刻が準備済みとはみなさない。

portable形式はMSM schema v1のNPZ。JSON metadataにmodel / schema / selection / source hashes / record keyを持ち、
値配列とproductごとの格子を保存する。NPZのCRCと構造検証を行い、`allow_pickle=False`で読む。
既存NetCDF normalized schema v1 / cache key / manifestは変更しない。
runtime storageの原子性、quota、eviction、取得サイズ制限は呼出側の責務。ライブラリは
破損を例外にして止め、別Run、別source、Legacyへ自動fallbackしない。

## 検証と再現

```bash
python -m pip install -e ".[test]" build
pytest -q
python -m build
python tests/runtime_case.py .runtime-case
npm ci --prefix tests/pyodide
npm exec --prefix tests/pyodide -- playwright install --with-deps chromium
npm test --prefix tests/pyodide
```

異なる出力先では`JMA_RUNTIME_CASE`と`JMA_RUNTIME_WHEEL`（wheelの絶対path）を指定する。
CIではPython 3.10 / 3.11 / 3.12の既存testsとwheel再installを維持し、Pyodide jobを追加する。
fixtureはsynthetic decodeから**desktopの通常prepare / NetCDF cacheを経由**して生成する。
風・気温の既知の解析値もunit testで照合する。
NodeとLinux ChromiumのPyodideではwheelを通常installし、以下を照合する。

- 上空風・上空気温・地上気温・地上風、水平・時間・HGT鉛直補間。
- Run固定 / update available、offline coverage、post-prepare altitude coverage。
- 全結果のprovenance（元URL・hash・Run・補間方式・trace）。
- portable cold/warm roundtrip、破損・SHA-256不一致・別Run・不足fieldの例外。
- `pygrib` / `fcntl` / NetCDF stackなし、desktop cacheとweather networkなしでの実行。

数値のruntime間比較は`rel_tol=1e-12, abs_tol=1e-10`、その他のcontractは完全一致。
runtime/テスト依存の初回setupにはnetworkを使うが、気象データは常にlocal fixture。
Chromiumから外部networkへのrequestは遮断して検証する。実データの直接取得・decoder性能、
productionアプリのcold/warm download量やmemory、Safari物理端末の安定性を証明するテストではない。

2026-09-16の検証結果：Python 3.12で199件成功（network opt-in 2件は通常実行から除外）。
独立したwheel installでも同じ結果。Node / Linux ChromiumのPyodide 0.27.7
（CPython 3.12.7）で両方成功し、20,603 bytesのsynthetic snapshotから5 queryと
全provenance・coverage・Run statusを照合した。
さらに既存の実データacceptanceを以下で実行し、1件成功した。

```bash
JMA_GPV_REAL_MSM=1 JMA_GPV_REAL_CACHE=/tmp/jma-real-msm pytest -q tests/test_real_msm.py
```

実データはRISHの2026-07-27 12Z RunのLsurf / L-pall FH00–15をdesktopで取得し、
31.877°N / 131.449°E、4572 m MSL、FH0 / FH1.5で確認。
GRIB → NetCDF → portable snapshotの上空・地上気温とprovenance、HGT coverageが一致した。
これはdesktopでの実GRIB decodeとsnapshot互換性の証拠であり、BrowserからのRISH取得の証拠ではない。

確認した元GRIBのSHA-256（同Run、FH00–15）：

| product | SHA-256 |
| --- | --- |
| L-pall | `6883387e115223b0942709cf4569a69b14b6d6cdee29e95e30c860d65252c685` |
| Lsurf | `4140479d4604e3c72eb76051293dc2d772652e574d731ab80d45a02b77b9fdf6` |

## #144への引継ぎ

AutoNavLogは公開`MsmClient`、`MsmPreparedData`、各queryとcoverageを利用し、
旧private surface-temperature branchを削除できる。listing本文とpayload bytesの取得・保存・
hash管理をPlatform/Weather Adapterが所有する。MSM file名、Run selection、補間、HGT判定、
provenanceをAutoNavLogへ複製する必要はない。

最新実MSMのpayloadをどう供給するかは#144の実通信・runtime検証が必要。
生GRIB decoderの追加が必要になった場合はmodel変換をこのlibraryへ置き、AutoNavLog専用forkにしない。
このPRは外部decode service、application server、proxy、別formatへの自動切替を要求・導入しない。
GSM選択・fallback policyは扱わない。
