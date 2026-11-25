# WXの全NaN recordと未準備recordを区別する

- ID: wx-missing-records
- Status: verified
- Scope: jma-gpv-weather 0.7.0、WX normalized cache / portable snapshot
- Trigger / search terms: RH、全NaN、MissingVariableError、warm cache、schema 2
- Last verified: 2026-11-26、Issue #22実装時
- Evidence: [WX契約](../../wx.md)、[回帰テスト](../../../tests/test_wx.py)、[実GPV測定](../../evidence/issue-22/measurements.json)

## Observation and impact

従来のnormalized readerは、有限値が一つもないfieldをrecordへ復元しない。
WXにそのまま流用すると、cold時の「配列はあるが値欠測」がwarm時には「record自体がない」に変わる。
portableの要求検証はrecord不足を処理失敗として扱うため、queryでの欠測分類が安定しなくなる。

## Cause and evidence

`normalized.load_records()`の`np.isfinite(values).any()`による復元条件。
WX用schema 3は実在するrecord keyを明示し、全NaN配列も復元する。
`test_warm_cache_preserves_all_nan_records_and_schema`で、両モデルのcold/warm query結果と
record集合の一致、およびportableへの再保存を確認する。

## Next-time action

新要素を追加するときは、有限値・一部NaN・全NaN・record欠落を分けて検証する。
従来の風・気温キャッシュを変更する場合は別途互換性を確認し、WXの仕様を無条件に遡及適用しない。

## Limits and counterexamples

今回の実GRIBには対象値の欠測がなかったため、全NaNケースは人工格子で検証した。
native cacheからportableへ移す際も同じ区別が必要だが、配信側の未取得assetの状態管理までは保証しない。

## 依存fieldを追加したときの旧キャッシュ

PR #24の`0651185`でALOFT_WINDの依存にTMPを追加しても、`8a7b09c`の
`v3-wx-*`キャッシュは同じ要求・source hashで再利用され、両モデルの付随気温が欠けたままだった。
file hashが正しくても、過去の依存field集合が現在の契約を満たすとは限らない。
WX namespaceを`v4-wx-*`へ更新し、旧キャッシュを再デコードする。
`test_pre_fix_wind_plus_wx_cache_is_rebuilt`で、有効な旧manifestを持つTMP欠落cacheからの
再生成、温度・高度coverage・provenanceの回復、次回のwarm reuseを両モデルで検証する。
schemaの変更とは独立して、依存field変更時は既存cacheの無効化または再検証も必要になる。
