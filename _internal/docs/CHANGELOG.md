# v1.0.0

初期版として、音声・動画の文字起こし、リアルタイム録音、Game Bar連携、話者名編集、結合・変換をまとめています。

- 認識方式② faster-whisper + Resemblyzerを推奨し、CPU／GPUと音声認識モデルを選べます。
- 人名や団体名などを認識時のヒントとして登録できます。認識後の自動置換は行いません。
- マイク・PC音の録音、左右への分離、ライブ文字起こし、録音後のキュー追加に対応します。
- ライト／システム／ダークの切り替え、通知領域での待機、ショートカットのカスタマイズを選べます。
- 共通保存先や設定の移行、処理結果の確認、オフライン操作ガイドを利用できます。

### 未署名によるWindows警告について

- **Windows発行元署名なし：** 配布exeにはAuthenticode署名がありません。ダウンロード警告、SmartScreenの警告、Smart App Controlによる実行拒否が起こる場合があります。Windowsの保護機能の無効化は推奨しません。
- 配布先はGitHub Releasesです。`SHA256SUMS.txt` によるハッシュ検証も可能です。
- [検証範囲](VALIDATION.md)、[警告の区別と対応](PACKAGING.md)、[第三者ソフトウェアの情報](https://github.com/Ptcr8-tool/Unified_Transcriber_Windows/blob/v1.0.0/THIRD_PARTY_NOTICES.md)も参照してください。
