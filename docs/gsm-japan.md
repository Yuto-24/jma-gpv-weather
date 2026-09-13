# GSM日本域: 仕様・公開契約・実データ検証

Issue #13 / AutoNavLog #161に向けた0.4.0の追加。基準はPR #14 merge後のmain
`8ec3dbcd1d85cab2b8a45f9a3bc04b625a07e86c`。

## Goal / Non-Goals

GSM日本域の上空風・上空気温・地上気温を、要求全体を満たす1つの固定Runから取得する。
モデル仕様のcoverageとRISHでの探索・取得成否を分離する。

GSMアジア域・全球域、AutoNavLogのモデル選択policy/UI/Project schema、MSM/GSM混在、
別source、通信障害時のmodel/source fallbackは対象外。registry、plugin、factory、
abstract base classは追加しない。GSMに地上風や推定QNHの公開queryは追加しない。

## 一次資料と採用仕様

2026-09-13 JSTに以下の公式資料を確認した。

- [気象庁・仕様No.12501](https://www.data.jma.go.jp/suishin/shiyou/pdf/no12501)
  （2023-03-14改訂、本文・別紙2・別紙4）。現行一覧の`definition.csv`でもNo.12501を確認。
- [気象庁・技術情報601号](https://www.data.jma.go.jp/suishin/jyouhou/pdf/601.pdf)
  （2024-01-24訂正版）。特に表3の訂正済みファイル名を採用。
- [JMBSC・GSM仕様](https://www.jmbsc.or.jp/jp/online/file/f-online10100.html)
  （ページ記載日2024-09-18）。旧0.2°×0.25°日本域の配信終了も確認。
- [JMBSC・MSM仕様](https://www.jmbsc.or.jp/jp/online/file/f-online10200.html)
  （2022-06-16更新）。MSMの新しいoffline判定だけにRun別horizonを使用する。

| 項目 | GSM日本域の採用値 |
| --- | --- |
| Run初期時刻 | 00 / 06 / 12 / 18 UTC |
| horizon | 00 / 12 UTC: 264 h、06 / 18 UTC: 132 h |
| 対象領域 | 北緯20–50°、東経120–150°、端点を含む |
| 地上・気圧面の格子 | 緯度0.1° × 経度0.125°、緯度301 × 経度241点 |
| FH0–132 | 地上1時間間隔、気圧面3時間間隔 |
| FH132以降（00/12 UTCのみ） | 地上135,138,…,264 h、気圧面138,144,…,264 h |
| 気圧面 hPa | 1000,975,950,925,900,850,800,700,600,500,400,300,250,200,150,100 |
| 上空必要変数 | 各面のU/V、気温、ジオポテンシャル高度（HGT） |
| 地上必要変数 | 2 m AGL気温（GRIB parameter category/number 0/0、heightAboveGround=2） |
| product | `GSM_GPV_Rjp_Gll0p1deg_Lsurf` / `L-pall` |

ファイル名は`Z__C_RJTD_YYYYMMDDhhmmss_GSM_GPV_Rjp_Gll0p1deg_KIND_FDdddd-dddd_grib2.bin`。
`FD`は2桁の日数＋2桁の時間であり、4桁をそのままforecast hourとして扱わない。

| product | FD分割（前6区間は全Run、後3区間は00/12 UTCのみ） |
| --- | --- |
| Lsurf | 0000–0100, 0101–0200, 0201–0300, 0301–0400, 0401–0500, 0501–0512; 0515–0700, 0703–0900, 0903–1100 |
| L-pall | 0000–0100, 0103–0200, 0203–0300, 0303–0400, 0403–0500, 0503–0512; 0518–0700, 0706–0900, 0906–1100 |

時間補間の両端はRunごとの公式時刻列から求める。例えばFH133.5では地上132/135、
気圧面132/138を使う。FH24.5は両productでファイルを跨ぐ。
旧解像度・Rgl・アジア域・未知の分割・不正なRun時刻はGSM日本域ファイルとして採用しない。

## RISHの実配置と実GRIB調査

[RISHの原データ案内](http://database.rish.kyoto-u.ac.jp/arch/jmadata/gpv-original.html)から
リンクされた`/arch/jmadata/data/gpv/original/YYYY/MM/DD/`を使用する。
MSMと同じUTC日別directoryにGSM日本域と全球域が共存し、日本域だけを選別する。
別archive、proxy、有料配信は使用していない。

[2026-09-12のlive listing](http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original/2026/09/12/)
の調査時snapshotでは日本域30ファイル（00Z:18、06Z:12）を確認した。
これは公開途中のsnapshotであり、1日のRun回数やhorizonの仕様根拠ではない。
RISH案内には原データの欠落がある旨が記載されている。archiveの恒久保持や完全性は保証しない。

固定`20260912000000`の`Lsurf_FD0000-0100`（19,511,629 bytes）と
`L-pall_FD0000-0100`（45,315,774 bytes）を実downloadし、pygribでmessageを確認した。
両者ともNi=241/Nj=301、北端50°/南端20°、西端120°/東端150°、増分0.125°/0.1°。
地上はFH0–24の1時間刻み、気圧面はFH0–24の3時間刻みだった。
地上2t（heightAboveGround=2）と、上記16面すべてのgh/u/v/tを確認した。
共通`grib.read_grib_records`の既存identity判定で扱え、GRIB処理の再実装は不要だった。

## Coverageと取得の公開契約

`MsmClient.check_coverage` / `GsmClient.check_coverage`は同じ引数形式。
`ForecastRequirements`に加え、`points=(CoveragePoint(...), ...)`、任意の`run=`、`as_of=`を受け取る。
I/O、download、listing、cache参照は行わない。`points`省略時は地点・高度の確認を要求していない扱い。
`as_of`は候補Runの初期時刻の上限であり、公開時刻・到着予定を仮定しない。
Run未指定時は要求の全時刻を満たす候補を仕様だけから列挙する。

| state / exception | 意味 |
| --- | --- |
| `CoverageState.COVERED` | 指定した検査項目が仕様内。ファイルの存在は未確認 |
| `CoverageState.OUTSIDE_SPEC` / `SpecCoverage.outside_spec=True` | 領域、使用可能気圧面、変数、Run/horizonによる確定した仕様外 |
| `CoverageState.REQUIRES_HGT` | MSL高度の指定あり。実HGTの確認が必要で、仕様外ではない |
| `GsmCoverageError` | clientの領域・変数・Run/時刻が確定した仕様外。`.coverage`に詳細 |
| `GsmDiscoveryError` | listing/通信失敗（部分的な失敗も隠さず伝播） |
| `GsmRunUnavailableError` | 仕様上可能でも、実archiveで互換Run/fileを発見できなかった |
| `GsmProcessingError` | download・GRIB・decode・cache・補間失敗。原因は`__cause__`で追跡 |

`GsmError`は`GpvError`を継承し、`MsmError`を継承しない。共通source/GRIBはモデル中立を維持。
MSMの従来の`NoCompatibleRunError`、`SelectedRunCoverageError`等はそのままであり、
**これらを仕様外のsignalとして使わない**。特に従来のMSM discoveryはlisting失敗を集約するため、
後続#161は独立したspec判定結果と取得結果を別々に処理する必要がある。

### MSL高度について保証できること・できないこと

気圧面の高度は一定ではない。公式の100 hPa/500 hPaを、推測した固定MSL高度へ変換しない。
`CoveragePoint.pressure_bracket_hpa=(lower, upper)`は指定した両気圧面が使用可能かをoffline判定する。
`altitude_msl_m`がある場合は、その値が有限であれば`REQUIRES_HGT`を返す。
標準大気で計算した便宜的な高度を「気象庁仕様の高度上限」として扱わない。

prepare後の上空queryは、各時間端・各水平格子点の実HGTで高度を挟む。
格子点のHGTと完全一致する場合も補間可能。最下端より下・最上端より上へ外挿しない。
挟めない場合の`VERTICAL_BRACKET_UNAVAILABLE`はデータを用いたquery結果であり、
`OUTSIDE_SPEC`とは別概念。高度範囲外と欠測の区別には、以下のpost-prepare公開APIを使用する。
**MSL高度だけで完全なyes/noをdownload前に保証することは、公式の気圧面仕様からはできない**。
この条件付き判定は後続#161にも保持する必要がある。

MSMのoffline契約は既存APIの実装範囲を正直に示すため、使用可能面を既存の1000–500 hPaに限定する。
公式MSMの配信自体は100 hPaまであるが、既存decode/queryの面集合を今回変更しない。
MSMのRun/horizonは00/12 UTCが78 h、他の3時間毎のRunが39 h、domainは22.4–47.6°N/120–150°E。

## Post-prepare高度coverage

MSM/GSM双方で`prepared.check_altitude_coverage(query)`を使用する。
既存の`WeatherResult`を返し、`kind="altitude_coverage"`、次の3状態を区別する。

| 結果 | 意味 |
| --- | --- |
| `availability=AVAILABLE`、`reason_code=None` | 必要な全時間端・全水平格子点で対象高度が実HGT範囲内 |
| `UNAVAILABLE`、`ALTITUDE_OUTSIDE_HGT_RANGE` | 全必要データが正常で、少なくとも1つの時間端/格子点で実HGT範囲外 |
| `UNAVAILABLE`、`SOURCE_VALUE_UNAVAILABLE` | 欠測・非有限値・不整合などにより判定不能。高度範囲外ではない |

```python
query = AloftQuery(latitude, longitude, valid_time, altitude_msl_m)
coverage = prepared.check_altitude_coverage(query)
# 後続#161で高度不足を示すsignalは、このreasonだけ。
altitude_outside = coverage.reason_code == "ALTITUDE_OUTSIDE_HGT_RANGE"
```

`AloftQuery`ではHGTとU/V/T、`AloftTemperatureQuery`ではHGTとTを確認する。
従来の`AloftQuery`のTは任意だったが、新しいcoverage確認では全気象値を要求する保守的な契約。
使用する全気圧面（MSMは既存10面、GSMは16面）の対象fieldが必要。
モデル既存spec関数で公式の時間端を求め、実queryの時間bracketと一致することを検証する。
端点全体の欠測を、遠い時刻への補間で隠さない。GSMのFH132以降は6時間間隔を使用する。

水平格子は既存の`_grid_bracket`と同じ点を使う。完全一致する時刻/格子点ではその端点/点だけが必要。
必要点で全fieldが有限、格子が整合し、降順気圧面のHGTが厳密に増加する場合に範囲を確定する。
最下端・最上端を含むHGT完全一致はcoverage内。外挿しない。
1点でも範囲外なら、他の全必要データを検証した後で高度範囲外を返す。
欠測、NaN/Inf、欠けたpressure level、不正なHGT profileや格子不整合が1つでもあれば、
別の点の範囲外より`SOURCE_VALUE_UNAVAILABLE`を優先する。
query対象外の格子点/時間の欠測は判定に混ぜない。

想定外の処理例外を高度範囲外へ変換しない。GSMは既存のprocessing境界と同様に
`GsmProcessingError`、MSMは元の処理例外を伝播する。入力不正はMSMで`InvalidQueryError`、
GSMで`ValueError`。prepare時のdownload/decode/cache失敗も従来のエラーのまま。
このAPIはprepare済みrecordsの読取りのみで、cacheへアクセスしない。

共通の検証は既存`WeatherDataset`内に置き、両prepared classはモデルの時間端を渡すだけ。
既存`query`の数値・補間順序・provenance・`VERTICAL_BRACKET_UNAVAILABLE`は変更しない。
後続#161はprivate methodや内部recordへアクセスせず、新しいAPIの結果で高度不足だけを識別できる。
実際のモデルfallback policyは本PRに含めない。offlineの`REQUIRES_HGT`も維持する。

## Run固定とWeather API

`discover_runs`は公式の全補間端点について必要なファイルが揃うRunだけを返す。
`resolve_run`はselected Runを変更せず、新しい互換Runを`latest_compatible_run`と
`update_available`で通知する。指定済みRunが仕様内だが未発見なら`SELECTED_RUN_NOT_DISCOVERED`、
仕様外なら`SELECTED_RUN_OUTSIDE_SPEC`を区別する。
`prepare_run`は指定Runが未発見なら失敗し、別Runのファイルを補完しない。
注入された`available_runs`もファイル名と所属Run、要件を再検証し、空tupleは明示的な空結果として扱う。

| query | requirements.variables | 公開結果 |
| --- | --- | --- |
| `AloftQuery` | `ALOFT_WIND`（気温も必要なら`ALOFT_TEMPERATURE`を追加） | 既存のU/V、風速、風向、任意の気温 |
| `AloftTemperatureQuery` | `ALOFT_TEMPERATURE` | `temperature_k` / `temperature_c`。風fieldを要求しない |
| `SurfaceTemperatureQuery` | `SURFACE_TEMPERATURE` | `temperature_k` / `temperature_c`、`representative_height_agl_m=2` |

3種類とも`forecast.query()` / `query_many()`と直接query methodから使用できる。
MSMとGSMで同じquery/result型を共有する。MSM地上気温は従来の`tmp_surface`と同じ演算順序・値。
CLIは引き続きMSMのみ。GSMは明示的なPython APIで利用する。

`weather.WeatherDataset`は2モデルで必要なrecord補間・結果組立てだけを共有する具体class。
MSM側は地上風とQNH、GSM側はquery検証とGSM provenanceを担当する。
`grib.py`、`normalized.py`、`interpolation.py`、`cache.py`、RISH transportは共通。
GSM provenanceはRun、全使用URL/SHA-256、補間法、時間端、格子点、実際の気圧面bracketとHGTを記録する。
MSMの既存provenanceへ追加fieldを混ぜず、完全一致で回帰確認する。

## Cache namespace

MSMの`data/raw/RUN`、`normalized/v1/RUN/KEY`、`locks`、`listings`は移動しない。
GSMは`GsmClient(cache_dir='data')`内部で**`data/gsm-japan`**を共通処理のrootへ渡す。
このためraw GRIB、`.part`、raw manifest、normalized NetCDF/manifest、listing cache、
各lock・一時file・破損退避がモデルごとに分離する。RunId自体へモデルprefixは付けない。
非UTC表現のGSM RunIdもUTCへ揃え、同じinitial timeは同じcacheへ入る。

共通の`normalized.prepare_records`へ既存MSMの保存処理を抽出した。
GSMでは追加のmanifest検証を有効にし、normalized SHA-256と入力rawのSHA-256を照合する。
破損やsource変更時は再decodeし、再構築不能はprocessing errorになる。
必要fieldの検証はcache再利用前と新規保存前に行い、部分的なdecode結果を正常cacheとして公開しない。
GSMにはRun単位のprepare lockも置き、異なる要求によるraw manifestの同時更新を直列化する。
MSMの既存manifest/schema/keyは変更しない。

## 実行した検証

通常テストはnetwork不要。実データは`tests/test_real_gsm.py` / `test_real_msm.py`の明示opt-in。
有効化後の通信失敗・欠損はskipせずtest failureになる。

GSM受入の固定条件:

- Run: `2026-09-12 00:00 UTC`
- 地点: 31.877°N / 131.449°E（宮崎空港付近）、4572 m MSL
- valid time: FH0 / 1.5 / 131.5 / 133.5 / 264
- `as_of=2026-09-12 06:00 UTC`、要求全体を満たす00Z Runだけを選択
- 実download8ファイル。00–24、121/123–132、135/138–168、219/222–264の4分割×2product
- 上空風速0–150 m/s、上空気温180–330 K、地上気温230–330 Kを検証
- 実GRIBの時間端132/138と132/135、水平・鉛直・時間補間trace、全source hashを確認
- 31.9°N / 131.5°E、FH0の850 hPa元GRIBをpygribで直接decodeし、同一HGTのAPI結果と照合
- 同じ格子点の地上2tも直接decode値と完全一致
- downloadとdecodeを禁止してwarm cacheから再queryし、値とprovenanceが完全一致

| FH | 上空U m/s | 上空V m/s | 上空気温 K | 地上気温 K |
| --- | --- | --- | --- | --- |
| 0 | 3.1514195061 | 1.0768675077 | 278.7600650929 | 299.0630393945 |
| 1.5 | 2.8980389779 | 1.5007813128 | 278.7546501229 | 299.6813386621 |
| 131.5 | 0.3316987309 | 2.0295569457 | 274.9211612121 | 298.5918934668 |
| 133.5 | 0.1439568822 | 1.7777582455 | 274.9435943713 | 298.4676014893 |
| 264 | 6.3164519203 | 12.3496395656 | 276.1506314408 | 300.1107505078 |

直接decode: 850 hPa HGT=1578.3216552734375 m、U=1.7228240966796875 m/s、
V=2.460023880004883 m/s、T=291.0312194824219 K。地上2t=299.24786376953125 K。

各ファイルは上記RISH日別URLと`Z__C_RJTD_20260912000000_GSM_GPV_Rjp_Gll0p1deg_`prefix、
`_grib2.bin`suffixで一意に指定できる。

| product / FD | SHA-256 |
| --- | --- |
| L-pall / 0000-0100 | d908038ce8aa059f5168ce07dd5b999ed65c81f3c91afa5737fa7350e738c19e |
| L-pall / 0503-0512 | 6ace81e65e87d2d5942b53fdd774351a064ed204ab859256bc0d1ff50e738ff0 |
| L-pall / 0518-0700 | 8f6103aec9bb377a18f8dd940e8601e9664a7a97957e3d67ed83c09579bfad2f |
| L-pall / 0906-1100 | ed3a66736ce5fd5c405daeefa1e179124c11ab14d05122f329f77eb000a85314 |
| Lsurf / 0000-0100 | 3f67f78e3276c09e0859d83e05e7f7b2fbe3d22b30e99fc4f7d55427fbe87fc9 |
| Lsurf / 0501-0512 | b3ca8af346e7fa73246ec171211ae2ec27477065a90b11f2181c3d91c5111c65 |
| Lsurf / 0515-0700 | f2ad0d8bdcb4b84ea7ca99d1c05e05f63d22aacc3878b178a355c1dd74ca367a |
| Lsurf / 0903-1100 | 4c71e743bb87443485496b03d592b8a4fa7b5a3ce1da22c94f19d830977b63e4 |

再現手順:

```bash
python -m pytest -q
JMA_GPV_REAL_GSM=1 JMA_GPV_REAL_MSM=1 JMA_GPV_REAL_CACHE=/tmp/gpv-acceptance \
  python -m pytest -q -s tests/test_real_gsm.py tests/test_real_msm.py
```

実行時のtest件数、package検証、Independent Reviewの結果はPR本文にも記録する。
上記固定Runが将来RISHで取得不能になっても別Run/sourceへ暗黙に置換しない。

初回GSM実装時の回帰・配布確認（Python 3.12、高度coverage追加前）:

- 通常テスト: 95 passed, 2 skipped（skipは実データopt-inの2件）。
- 新規cacheでGSM→MSMの実データtestを同一processで連続実行: 2 passed。
- mainと今回の実装でMSM固定Runを別々にcold decode:
  Run status、上空/地上風、気温、QNH未指定結果、provenance、
  surface 24件/pressure 80件の値・緯度・経度array SHA-256がすべて完全一致。
- mainが生成したnormalized cacheを今回の実装で再利用し、decode禁止でも同じ結果。
- sdistからwheelを隔離build。別Python 3.12環境へのwheel install後も通常テスト95 passed, 2 skipped。

調査中に受入test内の直接pygrib読取りを最初の2t messageで終了すると、その後のMSM decodeで
native crashが再現した（Python 3.14 / 3.12、pygrib 2.1.8）。test側もproduction decoderと同じく
multi-field streamを最後まで読むよう修正し、新規cacheでの連続cold decode成功を確認した。
モデルの値やproduction decoderへ迂回処理は追加していない。成功結果は修正後の実行であり、
クラッシュした実行を受入成功として扱っていない。


## Independent Review

GPT-5.6 Solが全差分を独立Reviewし、次の2件を修正後に再確認した。
不完全なnormalized cacheの保存/再利用を防ぎ、再decodeによる回復を検証した。
また、固定Runがある場合の空の実discoveryを`SELECTED_RUN_NOT_DISCOVERED`として扱い、
listing/通信失敗は引き続き例外として伝播するようにした。
再レビューで残る指摘なし。Reviewerの通常testは95 passed/2 skipped、実データwarm cacheは2 passed。

実装担当は修正後の最終wheelを隔離build/installし、通常95 passed/2 skipped、
新規cacheでGSM→MSMの実データ受入2 passedを確認した。
Reviewer自身の独立buildはsetuptools/wheel・ensurepip不足により未完了であり、
実装担当の成功結果とは区別する。


### Post-prepare高度coverage追加後の確認

Python 3.12のeditable環境と、sdistから隔離buildしたwheelの別install環境で、
それぞれ通常テスト178 passed / 2 skipped。高度coverageの追加83件では、
完全一致・上下範囲外・時間端/各水平格子点の片側範囲外、欠測/NaN/Inf、
pressure level不足、格子/HGT profile不整合、処理例外、offline契約維持を確認した。

固定GSM/MSM実データ受入は両環境で2 passed。初回に取得済みの実データcacheを再利用し、
4572 m MSLのcoverage内判定と、download/decode禁止のwarm cache判定一致を確認した。
追加前後の2モデルの出力JSON（数値・provenance・source hash）は完全一致した。
今回の再検証を新規download/cold decodeの証拠とはしていない。

GPT-5.6 Solが追加差分をIndependent Reviewし、指摘なし。
Reviewer独立実行は通常178 passed / 2 skipped、focused 141 passed、実データ2 passed。
変更前後の実データJSON完全一致と、331高度でcoverageと既存queryの整合も確認した。
