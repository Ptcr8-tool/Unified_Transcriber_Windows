# v1.0.3-b test installer / 検証用

Base: main commit 85891a4ba75624c80e64beef4729d01ac14469ca (PR #23).
This is a manual test build, not a stable release or automatic update.

## New in b / 今回の追加
- 停止ショートカットを押した際の無音Windows通知。録音停止の受付と保存完了を区別し、保存・終了後の文字起こしは続行します。
- Notifications acknowledge a normal stop request without stealing focus or playing a sound. Repeated notices are throttled; repeated stop shortcuts do not escalate to all-stop.
- 日本語・英語対応。通知領域メニューからの停止にも適用します。Windowsの通知設定により通知が表示されない場合があります。

## Included previous changes / 前回までの変更
- TOP stop: normal recording finalization first; explicit second click cancels remaining work. Rapid double clicks are guarded for 0.6 seconds.
- Default new audio source: microphone + PC playback. Previously saved choices are retained.
- Japanese/English display language in installer and settings; OS detection for a new installation. Unsupported languages use English; unavailable OS locale uses Japanese. ASR language is separate. Some advanced messages remain Japanese.
- Check Windows global shortcut availability before accepting and saving a shortcut.
- More tolerant automatic-update timeouts, download progress and cancellation.

## Test safely / 利用上の注意
現在のアプリは通知領域の常駐も含め完全終了してください。別のインストール先と出力先での試用を推奨します。既存環境を更新する場合は、設定を書き出し、録音・文字起こしを事前にバックアップしてください。
This executable is not Authenticode code-signed. Do not disable endpoint security; use an approved environment if organizational policy blocks execution.
全Windows自動テスト・インストーラー検証・Microsoft Defenderスキャンの成功後にのみEXEをアップロードします。実マイク、実際のGame Bar収録、GPU推論、利用PCでの通知表示までは自動検証していません。
No stable release, existing tag or public release asset is replaced by this workflow.
