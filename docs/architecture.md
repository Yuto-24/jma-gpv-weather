# JMA GPVの責務と拡張境界

## 最小限の分離

```text
jma_gpv_weather/
  models.py           query/result/provenance、Bounds、RemoteFile、RunSelection
  cache.py            lock、raw manifest、SHA-256、検証、listing TTL
  normalized.py       xarray/h5netcdfによる正規化cache
  grib.py              pygribによる読取りと矩形抽出
  interpolation.py    鉛直・水平・時間補間と格子/time bracket
  time_utils.py       UTC/JSTと日単位window
  errors.py           既存エラー階層
  cli.py              現在のMSM Weather query入口
  msm/
    spec.py           MSMファイル名・予報時間・気圧面・Run互換選択
    client.py         MSMの探索・固定・prepareとcache配置
    dataset.py        MSM query、surfaceの代表高度、QNHラベル
    terrain.py        MSMモデル地形
    qnh.py            既存推定QNH計算
    csv.py            MSM日単位CSVの仕様と出力
    csv_cli.py        MSM CSV入口
  sources/
    __init__.py       DataSourceの3操作の構造的Protocol
    rish.py           RISH URL、HTTP listing、再試行・Range download
```

MSM仕様は`msm/spec.py`に置き、RISHはMSMファイル名を解釈しません。`MsmClient`はsourceが返したlistingをMSM仕様で解釈します。source境界は`directory_url(day)`、`read_listing(url)`、`download(remote, destination)`だけです。downloadは親directory作成・再開・atomic置換まで担当し、cacheはそのcallableを受け取ってlock・検証・hash・manifestを担当します。RISH以外のproduction実装、registry、factory、モデル自動選択はありません。

`MsmClient(cache_dir, bounds, base_url)`の既存呼出しは維持しています。keyword-onlyの`source=`指定時はそのsourceを使用し、`base_url`は使いません。未指定時だけ`RishSource(base_url)`を生成します。別のsourceへの変更はこの境界で行い、query・補間の書換えを必要としません。

共通GRIB読取りと正規化保存は、呼出元から`pressure_levels`を受け取り、MSM定数をimportしません。pygrib、NumPy、xarray/h5netcdfの既存処理を再利用します。MSMのquery組立てはsurface高さやQNH契約を含むため、無理に共通基底classへ移しません。補間関数とbracket探索は共有し、MSMの演算順序・値を変更しません。`MsmError`という既存のエラー名・派生関係もcatch契約維持のため変更していません。

## cacheと後続モデル

MSMのraw/normalized/static path、normalized keyとschema v1を保持します。package renameによるcache破棄は不要です。CSVの`msm_wind_YYYYMMDD_*`出力名も、Python importとは独立した既存ファイル契約として維持します。

RunIdは従来どおり初期時刻であり、単独ではモデル識別子ではありません。Issue #13で別モデルを追加する際は、そのモデルのclientがMSMとは異なるcache root/namespaceを所有し、raw manifest、normalized cache、static asset、lockまで分離する必要があります。同じ時刻のMSM/GSMに既存の同一rootをそのまま使用してはいけません。これは将来モデルの実装要件であり、今回MSMのcacheを移設したりGSMのpathを予約したりはしません。

後続のGSM日本域では、Run・ファイル・格子・気圧面・coverage仕様とquery組立てをこのライブラリ側に追加します。既存のsource、GRIB読取り、cache、補間、query/result型を利用でき、AutoNavLogへGRIB処理を持ち込む必要はありません。coverage不足と運用エラーを区別する新契約もIssue #13で設計します。今回、既存MSMのlisting失敗時のRun探索動作は変更しません。

## 地形とQNH

Issue #6の公式Pzs取得とIssue #8の明示opt-in TOPOは独立した判断です。今回の基準mainにないdevelopの実装・Draft PRは取り込みません。モデル地形未指定時は引き続き`MODEL_TERRAIN_UNAVAILABLE`、外部DEM・MSLPへの暗黙fallbackなしです。地上気温は既存の正規化field `tmp_surface`とQNH入力で検証し、新しい公開queryは追加しません。
