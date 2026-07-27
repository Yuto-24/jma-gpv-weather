# JMA MSM wind extractor

RISH（京都大学生存圏研究所）のJMAデータアーカイブからMSM GRIB2を取得し、指定したJST日付・矩形範囲の風をCSVへ抽出します。既定条件は次の通りです。

- 緯度29.7–35.2°N、経度128.5–134.8°E（実格子東端134.75°E）
- 地上10 m風は1時間間隔、JST 00:00以上24:00未満
- 気圧面は1000, 975, 950, 925, 900, 850, 800, 700, 600, 500 hPaのHGT/U/Vを3時間間隔
- 15,000 ftは4572 m MSLのHGTを挟む2気圧面間でU/Vを線形補間

## セットアップと実行

Python 3.10以降を使います。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
pytest
msm-wind --date 2026-07-28 --discover-only
msm-wind --date 2026-07-28 --work-dir data --output-dir outputs
```

標準`venv`がない環境では `uv venv .venv` と `uv pip install --python .venv/bin/python -e ".[test]"` も使えます。`pygrib` wheelがない環境ではecCodesをOS/Condaで先に導入してください。

候補初期値を新しい順に調べ、対象日の全24時間をLsurf、全8時刻をL-pallで覆え、必要な分割ファイルがすべて存在する最新初期値を選びます。最新runの後半が未配信なら採用しません。ダウンロードは`.part`に保存して再開を試み、取得済みファイルは再利用します。

## 出力

- `*_surface.csv`: 地上10 m U/V・風速・風向（24時刻）
- `*_pressure_levels.csv`: 10気圧面のHGT/U/V（8時刻）。上側の補間根拠も保持
- `*_15000ft.csv`: 4572 m MSLへ補間したU/V・風速・風向（8時刻）
- `*_to_15000ft.csv`: HGT 4572 m以下の気圧面と補間面だけの上限付きデータ
- `*_metadata.json`: 初期値、範囲、元URL、実格子境界、行数

風向は気象学上の「吹いてくる方向」で `(270 - atan2(V,U)) mod 360`、0.1 m/s未満は空欄です。補間は風向でなくU/Vに行います。15,000 ftはMSLであり地面から4572 m上（AGL）ではありません。MSMは運航用の規制・観測資料を置き換えません。

## 出典・利用条件

データ作成者は気象庁、無料配布元は京都大学生存圏研究所RISHです。

- [RISH 気象庁データ](http://database.rish.kyoto-u.ac.jp/arch/jmadata/)
- [RISH GPV original](http://database.rish.kyoto-u.ac.jp/arch/jmadata/gpv-original.html)
- [気象庁 MSMカタログ](https://www.data.jma.go.jp/suishin/cgi-bin/catalogue/make_product_page.cgi?id=MesModel)
- [気象業務支援センター MSM仕様](https://www.jmbsc.or.jp/jp/online/file/f-online10200.html)

RISHの案内ではデータベースは教育研究機関向けです。企業活動等で頻繁に必要とする場合は気象業務支援センターから直接購入するよう求めています。現行案内を確認し、アクセスを集中させないでください。

## エラー処理

- `No run completely covers`: 後半未配信、予報範囲外、または欠損。時間を置いて再実行
- `Download failed`: ネットワーク/配布元障害。`.part`を残して再実行
- `Missing HGT/U/V`: GRIB欠損・仕様変更。処理は不完全CSVを成功扱いにしない
- RISHのHTTPS証明書検証に失敗する環境があるため、案内されているHTTPアーカイブを既定にしています

## 2026-07-28実データ検証

2026-07-27 23:25 JST時点で12 UTC runは後半未配信、09 UTC runの必要4ファイルは存在したため、09 UTCを自動選択しました。境界修正前のフルGRIB処理で地表24時刻、気圧面8時刻、15,000 ft補間8時刻が完走しました。座標抽出は浮動小数点誤差対策として微小halo取得後に丸め、要求矩形で厳密に再フィルタします。テストは29.7/35.2/128.5/134.75の包含と134.8125の除外を確認します。
