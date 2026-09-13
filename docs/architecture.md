# JMA GPVの責務と拡張境界

## 最小限の分離

```text
jma_gpv_weather/
  models.py           query/result/provenance、Bounds、RemoteFile、RunSelection
  coverage.py         offline coverage型、候補初期時刻、共通の判定組立て
  cache.py            lock、raw manifest、SHA-256、検証、listing TTL
  normalized.py       xarray/h5netcdfの正規化保存・再利用
  grib.py             pygribによる読取りと矩形抽出
  interpolation.py    鉛直・水平・時間補間と格子/time bracket
  weather.py          2モデル共通の気象値補間・結果組立て
  time_utils.py       UTC/JSTと日単位window
  errors.py           GpvError、MSM互換階層、GSMの分類
  cli.py              MSM Weather query入口
  msm/
    _errors.py        共通errorをMSM境界で変換
    spec.py           MSMファイル名・予報時間・使用気圧面・offline coverage
    client.py         MSMの探索・固定・prepareと既存cache配置
    dataset.py        MSM query入口、地上風、QNH
    terrain.py        MSMモデル地形
    qnh.py            既存推定QNH計算
    csv.py            MSM日単位CSVの仕様と出力
    csv_cli.py        MSM CSV入口
  gsm/
    spec.py           GSM日本域のRun・product・格子・時間列・offline coverage
    client.py         GSM探索・固定・prepare、独立cache root
    dataset.py        GSM query入口、要求検証とprovenance
  sources/
    __init__.py       DataSourceの3操作の構造的Protocol
    rish.py           RISH URL、HTTP listing、再試行・Range download
```

RISHはモデル別ファイル名を解釈しない。各clientがlistingをモデル仕様で解釈する。
source境界は`directory_url(day)`、`read_listing(url)`、`download(remote, destination)`だけ。
downloadは親directory作成・再開・atomic置換、cacheはlock・検証・hash・manifestを担当する。
RISH以外のproduction実装、registry、factory、モデル自動選択はない。

`MsmClient(cache_dir, bounds, base_url)`の既存呼出しを維持する。keyword-onlyの`source=`指定時は
そのsourceを使用し、未指定時だけ`RishSource(base_url)`を生成する。GSMも同じsource境界を使う。

共通GRIB読取りと正規化保存は呼出元から`pressure_levels`を受け取り、MSM/GSM定数をimportしない。
pygrib、NumPy、xarray/h5netcdfの既存処理を使い、演算順序を維持する。
`WeatherDataset`はrecord補間・provenance・結果組立てを共有する具体classであり、abstractな
モデルframeworkではない。MSMの地上風/QNHはMSM moduleに留める。

## Coverage / discovery / processing

`MsmClient.check_coverage`と`GsmClient.check_coverage`はnetwork不要の別契約。
`OUTSIDE_SPEC`、`COVERED`、`REQUIRES_HGT`を区別する。気圧面のMSL高度を固定値と仮定せず、
高度を指定した要求の実bracketはHGT取得後に確認する。
両モデルのprepared forecastは`check_altitude_coverage(query)`を公開し、共通`WeatherDataset`で
必要な全時間端・格子点・気圧面・対象fieldを検証する。全データが正常な場合だけ
`ALTITUDE_OUTSIDE_HGT_RANGE`を返し、欠測・非有限値は`SOURCE_VALUE_UNAVAILABLE`として区別する。
既存queryの値・provenance・errorは変更しない。
詳細な仕様根拠・制限・使用例は[GSM日本域の設計・調査記録](gsm-japan.md)を参照。

GSMは`GsmCoverageError`、`GsmDiscoveryError`、`GsmRunUnavailableError`、`GsmProcessingError`で
仕様外・listing失敗・実Run未発見・取得処理失敗を区別する。部分的なlisting失敗を無視して
「最新」を断言せず、discovery errorとして伝播する。どの状態にも別source/model fallbackはない。

## MSMの互換境界

`GpvError(RuntimeError)`が共通base。既存`MsmError`とそのspecific subclassを維持する。
RISHと共通GRIBは`GpvError`だけを使用する。MSMの取得/decode/CSV/地形境界で共通errorを
`MsmError`へ変換し、メッセージと元例外を保持する。既存specific errorは同一例外のまま再送出し、
既存の`ValueError`や`OSError`を追加変換しない。

MSM探索のlisting失敗集約・Run選択は従来どおり。`NoCompatibleRunError`を仕様外の判定に使わず、
新しいoffline契約で仕様上の可能性を独立して確認する。CSV CLIの終了契約も維持する。

既存MSMの値、演算順序、provenance、RunId、raw/normalized/static path、normalized key/schema v1を
維持する。MSMで使用する気圧面は従来の1000–500 hPaのままで、公式配信の全16面へ拡張しない。
新しい`SurfaceTemperatureQuery`は既存`tmp_surface`と同じ値を返す。

## Cacheとmodel識別

RunIdは初期時刻であり、モデル識別子ではない。MSMは従来のcache rootを使用し、
GSMはその配下の`gsm-japan/`を独立rootにする。raw/normalized/listing/manifest/lockを含めて分離し、
MSMの既存cacheを移設しない。GSMでは追加のnormalized hash/source照合を有効にする。
MSMの既存manifest形式は変更しない。

CSVの`msm_wind_YYYYMMDD_*`出力名はPython importから独立した既存ファイル契約として維持する。
package renameに関するPR #14の判断と、Issue #13で導入したモデル分離はこの境界で両立する。

## 地形とQNH

Issue #6の公式Pzs取得とIssue #8の明示opt-in TOPOは独立した判断であり、developの実装やDraft PRを
取り込まない。MSM地形未指定時は`MODEL_TERRAIN_UNAVAILABLE`のまま。DEM/MSLPへの暗黙fallbackはない。
GSMにQNHや地形取得を先行実装しない。
