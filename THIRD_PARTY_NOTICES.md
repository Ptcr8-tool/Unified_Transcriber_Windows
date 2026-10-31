# 第三者ソフトウェアとライセンス情報

この文書は、Windowsセットアップのビルド・追加環境の構築で使用する主な第三者ソフトウェアの出典と、配布物に関する確認状況を記録するものです。全依存物の完全なライセンス一覧や、再配布条件を満たしていることの保証ではありません。各配布物に付属するライセンス本文と、利用する版・構成に適用される条件を確認してください。

本プロジェクト自身のコードのライセンスは、権利者による指定を確認できていません。この文書は本プロジェクト全体にオープンソースライセンスを付与するものではありません。

## セットアップEXEに含める主なもの

以下は現在のビルド設定に基づく情報です。生成物ごとの実際の内容とバージョンは、ビルド成果物の `DEPENDENCIES.json` およびインストール後の `_internal/distribution-info.json` で確認できます。

| 構成要素 | 用途・出典・ライセンス情報 | 現在のビルド上の扱い |
| --- | --- | --- |
| CPython 3.11.18 | GUIと従来方式用のPython。Python Software Foundation等によるソフトウェアで、[Python 3.11のライセンスと第三者表記](https://docs.python.org/3.11/license.html)を参照。 | 固定した公式ソースからWindows向けランタイムをビルドし、`Runtime/LICENSE.txt` に同ソースのライセンス本文を配置する設定。差し替えたネイティブ依存物については別途確認が必要。 |
| CPython 3.14.10 | 方式②・録音環境用。Python Software Foundation等によるソフトウェア。 [Python 3.14のライセンスと第三者表記](https://docs.python.org/3.14/license.html)を参照。 | 固定した公式NuGetパッケージの `tools` 内容を `MLRuntime` に配置し、`LICENSE.txt` の存在を検証。ネイティブ拡張の一部を別途差し替え・再ビルドする。 |
| NSIS 3.13 | セットアップEXEの生成に使用。大部分はzlib/libpngライセンス、圧縮モジュール等には別条件があり、[NSIS公式ライセンス全文](https://nsis.sourceforge.io/Docs/AppendixI.html)を参照。 | `COPYING` をハッシュ検証済みNSISアーカイブから取得し、インストール先の `_internal/NSIS-LICENSE.txt` に配置する設定。 |
| Microsoft Visual C++ Runtime | Python等のアプリローカル実行に必要なMicrosoftの再頒布可能DLL。[Microsoftの再配布説明](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)と、該当するVisual Studio版のライセンス条項・[2026年版REDIST一覧](https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution)を参照。 | ビルド元のVisual StudioからDLLを選び、署名を検証して `Runtime` と `MLRuntime` に配置し、それぞれ `MICROSOFT_CRT_NOTICE.txt` を生成。使用したVisual Studioライセンスと各DLLの再配布対象への該当確認が必要。 |
| GitHub CLI | 更新物の出所証明の検証に使用。[GitHub CLIライセンス](https://github.com/cli/cli/blob/trunk/LICENSE)を参照。 | 固定版の公式アーカイブから `gh.exe` とライセンスファイルを `_internal/update-verifier` に配置する設定。ライセンスが欠落または判別できない場合はビルドを停止する。 |
| tkinterdnd2 / tkdnd | ドラッグ＆ドロップに使用。[tkinterdnd2 0.6.4配布情報](https://pypi.org/project/tkinterdnd2/0.6.4/)を参照。パッケージおよび含まれるtkdndの各ライセンスは配布wheel内の表記を確認。 | 固定したwheelを `_internal` に配置し、必要に応じて導入する設定。wheelのライセンス・第三者表記を最終配布物とともに確認する。 |

Pythonやネイティブ拡張には、OpenSSL、Tcl/Tk、SQLite、XZ、zlib、bzip2、libffi等の第三者ソフトウェアが含まれ得ます。固定した入力は `_internal/tools/cpython-windows-sources.json`、`_internal/tools/modern-python-sources.json` および `_internal/tools/windows-build-sources.json` を参照してください。CPython 3.14用のXZはソースから再ビルドし、`MLRuntime/XZ_COPYING.txt` と `MLRuntime/XZ_LICENSE_0BSD.txt`、ネイティブ構成の説明を配置する設定です。[XZ Utils](https://tukaani.org/xz/) と [SQLiteの著作権情報](https://www.sqlite.org/copyright.html)も参照してください。各構成要素の全文・著作権表記が必要十分に同梱されているかは、生成されたバイナリー単位で引き続き確認が必要です。

## セットアップ時に別途取得するもの

| 構成要素 | 用途・出典・確認事項 |
| --- | --- |
| FFmpeg / FFprobe 9.0.2 | 録音・音声動画処理用に、セットアップが [GyanのWindows builds](https://www.gyan.dev/ffmpeg/builds/) から固定版を取得。配布元は静的リンクのGPLv3ビルドと説明しています。セットアップEXEにあらかじめ含める構成ではありません。固定ハッシュと一致する `ffmpeg.exe`、`ffprobe.exe`、配布元の `LICENSE`・`README.txt` を `_internal/environments/media-tools` に配置します。固定対象のFFmpeg上流コミットは [946fcce07b](https://github.com/FFmpeg/FFmpeg/commit/946fcce07b) です。外部ライブラリーを含む対応ソースとビルド手順、および再配布条件は別途確認が必要です。[FFmpeg公式ライセンス説明](https://ffmpeg.org/legal.html)を参照。 |
| AI・音声・録音用Pythonパッケージ | 選択した方式に応じ、PyTorch、TorchAudio、faster-whisper、CTranslate2、Resemblyzer、音声収録ライブラリー等を公式配布元から追加取得します。各パッケージ・依存物にはそれぞれのライセンスが適用されます。導入済み環境の `site-packages` 内の各 `*.dist-info` にあるメタデータやライセンスファイルと、各配布元の条件を確認してください。 |
| 音声認識・話者分離モデル | モデルは選択されたものを別途取得します。モデルごとに異なるライセンスや利用条件・アクセス条件があり、ソフトウェア本体とは別に各モデルの配布ページを確認してください。 |

## 配布前に残る確認

1. 実際に生成したEXEの展開結果とインストール後のファイルを照合し、上記の既存ライセンス・著作権表記が利用者から確認できることを検証する。特に、新しいNSISとPython 3.11のライセンス類が設定どおり残ることを確認する。
2. FFmpegの固定バイナリーについて、`ffmpeg -buildconf`、配布元のLICENSEとREADME、対応する正確なソース・変更内容・外部ライブラリーの入手経路と、該当する配布条件を確認する。
3. Pythonに含まれる全ネイティブライブラリー、wheel内の同梱物、追加インストールされる依存関係・モデルをバージョンごとに棚卸しし、必要な著作権表示・ライセンス本文・対応ソース等を整備する。
4. Microsoftの再配布条件とビルドに使用するVisual Studioのライセンスを確認する。本プロジェクト自身のライセンスは、権利者が別途決定する。

上記は技術的な調査と記録であり、法的な適合判断は未完了です。
