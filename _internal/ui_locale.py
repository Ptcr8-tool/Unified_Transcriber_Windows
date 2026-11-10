"""English/Japanese application display locale. Independent of ASR language.

The locale is explicit once selected; OS detection is used only to seed a new
installation. Missing OS locale falls back to Japanese; unsupported locales to
English. Translations are deliberately fixed strings, never network requests.
"""
from __future__ import annotations

import ctypes
import locale
import sys
import tkinter as tk
from tkinter import ttk

LANGUAGES = {'日本語': 'ja', 'English': 'en'}
_active_language = 'ja'

# Presentation strings only. Internal labels used to serialize recorded
# channels, file formats and model profiles retain their stable values.
ENGLISH = {
    '文字起こし統合スタジオ': 'Unified Transcriber Studio',
    '音声と映像を、使えるテキストに。': 'Turn audio and video into useful text.',
    '録音・録画': 'Record & Capture',
    'ツール': 'Tools',
    '設定・診断': 'Settings & Diagnostics',
    '録音・録画を開始': 'Start recording & capture',
    '取り込み・環境': 'Import & Setup',
    '結合・音声の抽出': 'Merge & Extract Audio',
    '変換・話者名': 'Convert & Speakers',
    '環境の準備・確認': 'Install & Check Environment',
    '操作・設定': 'Operation & Preferences',
    'ログ': 'Logs',
    'サポート': 'Support',
    'ファイル': 'Files',
    'カテゴリ': 'Category',
    '閉じる': 'Close',
    'キャンセル': 'Cancel',
    '保存': 'Save',
    '適用': 'Apply',
    '選択…': 'Browse…',
    '参照…': 'Browse…',
    '参照': 'Browse',
    'フォルダーを選ぶ…': 'Choose folder…',
    'ドキュメントに戻す': 'Use Documents',
    'ファイルを追加…': 'Add files…',
    '追加メニュー': 'More actions',
    '選択項目': 'Selected items',
    'リアルタイム録音・文字起こし…': 'Live recording & transcription…',
    'Game Bar連動録画…': 'Game Bar recording…',
    '録音・録画のセットアップ…': 'Set up recording…',
    '録音の文字起こしを中断': 'Stop recording transcription',
    '標準録画を取り込み・文字起こし…': 'Import & transcribe a recording…',
    'TXT結合…': 'Merge TXT…',
    '音声結合・動画から音声を抽出…': 'Merge/extract audio…',
    'CSV結合…': 'Merge CSV…',
    'TXT ⇔ CSV変換…': 'TXT ⇔ CSV conversion…',
    '話者名を設定…': 'Edit speaker names…',
    '一括セットアップ…': 'Batch setup…',
    '環境セットアップ・割り当て…': 'Configure AI environments…',
    'CUDA / GPU診断…': 'CUDA / GPU diagnostics…',
    'CPU・GPU負荷制限…': 'CPU/GPU resource limits…',
    '録音・録画のショートカット…': 'Recording shortcuts…',
    '自動起動・常駐の設定…': 'Startup & tray settings…',
    '認識用の単語辞書…': 'Recognition vocabulary…',
    '設定を書き出す…': 'Export settings…',
    '設定を読み込む…': 'Import settings…',
    'アプリの更新・確認間隔…': 'Updates & check interval…',
    'D&D 診断・修復…': 'Drag & drop diagnostics…',
    '診断ログフォルダーを開く': 'Open diagnostics folder',
    'ログを別ウィンドウで表示': 'Open logs separately',
    'ログをZIP保存…': 'Export logs as ZIP…',
    '使い方・ショートカット': 'Help & shortcuts',
    '● リアルタイム録音': 'Live recording',
    '▣ Game Bar連動': 'Game Bar',
    'すべて停止': 'Stop all',
    '画面付き操作ガイド': 'Illustrated guide',
    '認識方式': 'Recognition engine',
    '未セットアップ': 'Not installed',
    '詳細設定…': 'Advanced settings…',
    '単語辞書…': 'Vocabulary…',
    '文字起こしを開始': 'Start transcription',
    '停止': 'Stop',
    '出力先を開く': 'Open output folder',
    '完了後に話者名設定': 'Edit speaker names after finishing',
    '認識プレビューとログ': 'Transcript preview & logs',
    '認識テキスト・暫定／話者未確定': 'Transcript preview / speakers tentative',
    '詳細ログ・処理経過／エラー／進捗': 'Details / progress / errors',
    'コピー': 'Copy',
    'ログを別ウィンドウで表示': 'Open logs in new window',
    'ファイル名': 'File name',
    'サイズ': 'Size',
    '処理状態': 'Status',
    '形式': 'Format',
    '文字起こし保存先': 'Transcript folder',
    '音声を連続録音しながら、別処理で区間ごとに文字起こし': 'Record continuously and transcribe in segments',
    'リアルタイム録音・文字起こし': 'Live recording & transcription',
    '動作モード': 'Mode',
    '音声入力元': 'Audio source',
    '入力デバイス': 'Input device',
    'デバイス更新': 'Refresh devices',
    '入力テスト': 'Test input',
    '同時録音：2つの入力デバイスと音量': 'Simultaneous recording: two inputs & volume',
    'マイク': 'Microphone',
    'PC再生音': 'PC playback audio',
    '音量': 'Volume',
    '文字起こしの区切り（秒）': 'Transcription interval (seconds)',
    '録音形式・コーデック（終了後に変換）': 'Recording format / codec (post-conversion)',
    '保存形式': 'Save format',
    'ビットレート': 'Bitrate',
    'WAV／FLAC ビット深度': 'WAV/FLAC bit depth',
    '保存サンプルレート': 'Sample rate',
    '保存チャンネル': 'Audio channels',
    '出力先フォルダー': 'Output folder',
    '録音環境をセットアップ': 'Set up audio recording',
    '● 録音を開始': 'Start recording',
    '■ 録音を終了': 'Stop recording',
    '認識を中断': 'Cancel transcription',
    'ショートカット…': 'Shortcuts…',
    '次回以降は設定画面をスキップする': 'Skip settings next time',
    'ショートカットキーを入力': 'Enter shortcut',
    '割り当てたいキーを押してください': 'Press the shortcut you want to use',
    '解除': 'Clear',
    'このキーを使う': 'Use this shortcut',
    '録音・録画のショートカット': 'Recording & capture shortcuts',
    'キー入力…': 'Capture keys…',
    '録音を開始': 'Start recording',
    'Game Bar連動録画を開始': 'Start Game Bar recording',
    'セットアップ': 'Setup',
    'インストール先': 'Install location',
    'ログ・録音・文字起こしなどの共通保存先': 'Shared folder for logs, recordings and transcripts',
    '導入するもの': 'Install components',
    'プログラム一覧に登録（アンインストールの案内）': 'Register in Windows Apps',
    'スタートメニューに追加': 'Add to Start menu',
    '録音環境をセットアップ': 'Install recording environment',
    '推奨AIセット（② faster-whisper + Resemblyzer）': 'Recommended AI package (② faster-whisper + Resemblyzer)',
    '完了後にアプリを自動で起動する': 'Launch the app after setup',
    'AIの実行環境': 'AI compute device',
    '②の音声認識モデル': 'Speech recognition model for ②',
    'インストールする': 'Install',
    '修復': 'Repair',
    'アンインストール': 'Uninstall',
    '修復する': 'Repair',
    'アプリを起動': 'Launch app',
    '完了までお待ちください': 'Please wait until complete',
    '追加環境の構築を中断': 'Cancel optional setup',
    'Language / 言語': 'Language / 言語',
    '画面表示の言語': 'Interface language',
    '音声認識の言語設定は変更しません。': 'This does not change the recognition language.',
    '変更を保存': 'Save language',
    '表示言語': 'Display language',
    '日本語': 'Japanese',
    'English': 'English',
    '設定を保存しました。': 'Settings saved.',
    '音声を連続録音しながら、別処理で区間ごとに文字起こし': 'Record continuously and transcribe in segments.',
    '見出しの下から操作を選びます': 'Choose an action below its heading',
    '実行する操作を選びます': 'Choose an action',
    'ファイルの追加方法': 'Add files',
    '追加する範囲や、自動で取り込む方法を選びます': 'Choose what to add or monitor',
}


# Menu explanations and first-use guidance. The original Japanese text remains
# the fallback for any unsupported/advanced dialog until it has been localized.
ENGLISH.update({
    '表示言語…': 'Display language…',
    'アプリの表示言語を日本語／英語から選択します（音声認識の言語は変更しません）': 'Choose Japanese or English for the interface (ASR language is unchanged)',
    '画面の表示言語を切り替えます（音声認識の言語とは独立）': 'Change the interface language (independent of speech recognition)',
    'マイクやPCの音、画面を記録して文字起こしします': 'Record microphones, system sound or video and transcribe',
    '録画済みファイルの取り込みと、録音環境の準備を行います': 'Import recordings and install recording components',
    '複数の結果をまとめたり、動画から音声を取り出したりします': 'Merge output files or extract audio from videos',
    'テキストの形式や、発言者の名前を整えます': 'Convert transcript formats and edit speaker names',
    '文字起こしに必要な環境の導入や、GPUの動作確認を行います': 'Install AI environments or check GPU compatibility',
    '負荷制限、キー操作、単語辞書、設定の移行を行います': 'Configure resources, shortcuts, vocabulary and migration',
    '処理の経過やエラーを確認し、診断用に保存します': 'Review progress, errors and diagnostic logs',
    'アプリの更新や、操作で困ったときの案内です': 'Update the app and get troubleshooting help',
    '一覧で選択したファイルの結果と保存場所を確認します': 'Review output and locations of selected files',
    '一覧で選択したファイルの保存方法を変更します': 'Choose how to save selected files',
    '一覧のファイルを選択したり、一覧から取り除いたりします': 'Select or remove items from the queue',
    'ファイル一覧に表示する情報を切り替えます': 'Configure columns shown in the file queue',
    'マイク・PC音の録音設定を開きます。録音だけの利用もできます': 'Open microphone and PC audio settings, including record-only mode',
    '画面録画の設定を開き、録画するウィンドウを選びます': 'Select a window to record with Xbox Game Bar',
    '録音・録画に必要な環境を準備します': 'Install recording dependencies',
    '録音ファイルを保持し、録音と文字起こしを終了します': 'Stop capturing and transcribing while keeping the audio',
    '録画済みの動画を取り込み、音声を文字起こしします': 'Import a captured video and transcribe its audio',
    '複数の文字起こしを、1つのTXTファイルにまとめます': 'Merge transcript files into one TXT',
    '複数の音声をまとめる、または動画を音声ファイルにします': 'Join audio files or extract audio from videos',
    '複数の文字起こし結果を、1つのCSVファイルにまとめます': 'Merge transcripts into one CSV',
    '文字起こしのTXTとCSVを相互に変換します': 'Convert transcript TXT and CSV formats',
    'SPEAKER_00などの話者ラベルを、名前に置き換えます': 'Replace SPEAKER_00 labels with speaker names',
    '使いたい認識方式を選び、必要な環境をまとめて導入します': 'Install chosen recognition models and their dependencies',
    '認識方式ごとの実行環境を準備・割り当てます': 'Prepare or assign each recognition environment',
    'GPUが認識されているか、利用できるかを確認します': 'Check if a compatible GPU is detected and usable',
    '文字起こし中に使用するCPU・GPUの負荷を調整します': 'Control CPU/GPU use during transcription',
    '録音・録画の開始や停止に使うキーを変更します': 'Change shortcuts for recording and stopping',
    'Windows起動時の実行と、通知領域での待機を設定します': 'Configure Windows startup and tray behavior',
    '固有名詞や専門用語など、認識の参考にする単語を登録します': 'Register proper names and technical terms as recognition hints',
    '今の設定をファイルに保存し、バックアップや移行に使います': 'Export settings to back up or move them to another PC',
    '保存しておいた設定ファイルから、設定を読み込みます': 'Import a saved settings file',
    '新しいバージョンを確認し、自動で確認する間隔を設定します': 'Check for releases and set the check interval',
    'ファイルをドラッグして追加できないときに確認・修復します': 'Diagnose drag-and-drop if file dropping does not work',
    '診断用のログが保存されているフォルダーを開きます': 'Open the diagnostic logs folder',
    '作業画面とは別のウィンドウで、処理のログを確認します': 'View processing logs in a separate window',
    '問い合わせや調査に使うログを、ZIPファイルにまとめます': 'Export support and diagnostic logs to ZIP',
    '機能の使い方と、利用できるショートカットを確認します': 'Show help and available keyboard shortcuts',
    'フォルダー内の音声・動画を、まとめて一覧に追加します': 'Add all supported files from a folder',
    '指定フォルダーに追加されたファイルを、自動で処理します': 'Automatically transcribe newly added files',
    '選択したファイルの出力形式と、文字起こしの保存先を変えます': 'Change output format and folder for selected files',
    '出力がすでにある場合の、スキップ・上書き・別名保存を選びます': 'Choose skip, overwrite or rename if output exists',
    '選択したファイルを処理一覧から取り除きます': 'Remove selected files from the queue',
    '処理一覧にあるすべてのファイルを選択します': 'Select all queued files',
    '処理一覧を空にします': 'Clear the queue',
    '選択したファイルの処理結果と、保存場所を表示します': 'Show the selected file result and location',
    '選択した元ファイルがあるフォルダーを開きます': 'Open the folder containing the source file',
    '選択したファイルの文字起こし保存先を開きます': 'Open the selected transcript output folder',
    '選択した文字起こし結果の、話者ラベルを名前に置き換えます': 'Edit speaker labels in the selected transcript',
    '録音＋文字起こし': 'Record & transcribe',
    '録音のみ': 'Record only',
    '文字起こしのみ': 'Transcribe only',
    'マイク入力': 'Microphone',
    'PC再生音（WASAPIループバック）': 'PC playback (WASAPI loopback)',
    'マイク＋PC再生音（同時録音）': 'Microphone + PC playback (simultaneously)',
    '出力先': 'Output',
    '入力と同じ場所': 'Same folder as input',
    'デフォルト': 'Default',
    'お知らせ': 'Information',
    '録音環境のセットアップ': 'Recording setup',
    '同時録音：2つの入力デバイスと音量': 'Simultaneous recording: two inputs and levels',
    '録音は先に開始し、対象ウィンドウへフォーカスを切り替えた後でショートカットを送信します。': 'Audio starts before focus switches to the target window and the Game Bar shortcut is sent.',
    '録音中は欠落を避けるためPCM WAVを連続保存します。停止後に指定形式へ変換。変換に失敗した場合はWAVを保護します。': 'Record PCM WAV continuously, then convert after stopping. The WAV is retained if conversion fails.',
    '文字起こしのみでも一時WAVを内部に保持し、処理が終わると削除します。': 'Transcription-only mode uses a temporary WAV that is removed after processing.',
    'PC音とマイクの混合、またはL/Rに分けた録音ができます。': 'Mix microphone and PC sound, or record them on separate L/R channels.',
    'Pythonは同梱されています。録音環境とAIはインターネットから追加します。\nAIセットは数GBの空き容量が必要です。OFFでも、後からアプリ内で導入できます。': 'Python is included. Recording and AI components download on demand.\nAI models may need several GB; they can be installed later.',
    '用途別のサブフォルダーを作成します。既存の出力ファイルは移動しません。': 'Separate subfolders are created for each purpose. Existing output files are not moved.',
    '保存先と必要な構成を選び、開始してください。': 'Choose installation options and start setup.',
})

def normalize_language(value: str) -> str:
    if value not in ('ja', 'en'):
        raise ValueError('UI language must be ja or en.')
    return value


def language_for_locale(value: str | None) -> str:
    """Unknown supported OS language is English; unavailable is Japanese."""
    if not value:
        return 'ja'
    code = str(value).replace('_', '-').split('-')[0].lower()
    return 'ja' if code == 'ja' else 'en'


def system_language() -> str:
    if sys.platform == 'win32':
        try:
            # 0x11 = Japanese; 0x09 = English. Other primary languages use en.
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if langid:
                return 'ja' if langid & 0x3FF == 0x11 else 'en'
        except (OSError, AttributeError, ValueError):
            return 'ja'
        return 'ja'
    try:
        return language_for_locale(locale.getlocale()[0])
    except (ValueError, TypeError):
        return 'ja'


def set_language(language: str) -> None:
    global _active_language
    _active_language = normalize_language(language)


def get_language() -> str:
    return _active_language


def translate(value: str, language: str | None = None) -> str:
    if (language or _active_language) != 'en':
        return value
    if value.startswith('文字起こし統合スタジオ セットアップ v'):
        return value.replace('文字起こし統合スタジオ セットアップ v', 'Unified Transcriber Setup v', 1)
    if value.startswith('文字起こし統合スタジオ v'):
        return value.replace('文字起こし統合スタジオ v', 'Unified Transcriber Studio v', 1)
    return ENGLISH.get(value, value)


def translate_widgets(root, language: str | None = None) -> None:
    """Translate existing text widgets without mutating values/IDs or callbacks.

    Retain source strings on widgets so changing back to Japanese restores
    originals; dynamic fields and user data are deliberately never translated.
    """
    lang = normalize_language(language or _active_language)
    stack = [root]
    while stack:
        widget = stack.pop()
        try:
            stack.extend(widget.winfo_children())
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                current = widget.title()
                original = getattr(widget, '_ui_source_title', None)
                if original is None or current not in (original, translate(original, 'en')):
                    original = current
                    widget._ui_source_title = original
                widget.title(translate(original, lang))
            if isinstance(widget, tk.Menu):
                continue  # Internal menu label keys are deliberately stable.
            try:
                current = str(widget.cget('text'))
            except (tk.TclError, AttributeError, TypeError):
                continue
            if not current:
                continue
            original = getattr(widget, '_ui_source_text', None)
            if original is None or current not in (original, translate(original, 'en')):
                original = current
                widget._ui_source_text = original
            label = translate(original, lang)
            if label != current:
                if isinstance(widget, ttk.Widget):
                    # Bypass icon mixins: translated labels must not reset icons.
                    ttk.Widget.configure(widget, text=label)
                else:
                    widget.configure(text=label)
        except tk.TclError:
            continue


def language_settings(app):
    """Language switch in Settings -> Operation; persists independently of ASR."""
    from dataclasses import replace
    from core import save_config
    from unified_gui import modal, show_error
    from ui_icons import IconButton

    app.ui.close_drawers()
    win = modal(app.root, 'Language / 言語', 580)
    win.geometry('580x340')
    content = ttk.Frame(win, padding=24)
    content.pack(fill='both', expand=True)
    ttk.Label(content, text='Language / 言語', style='Section.TLabel').pack(anchor='w')
    ttk.Label(content, text='画面表示の言語 / Interface language').pack(anchor='w', pady=(14, 5))
    selection = tk.StringVar(win, 'English' if app.config.ui_language == 'en' else '日本語')
    ttk.Combobox(content, state='readonly', textvariable=selection,
                 values=('日本語', 'English'), width=30).pack(anchor='w')
    ttk.Label(content, text='音声認識の言語設定は変更しません。 / Recognition language stays unchanged.',
              wraplength=510).pack(anchor='w', pady=(12, 5))
    footer = ttk.Frame(content)
    footer.pack(side='bottom', fill='x')
    IconButton(footer, text='Cancel / キャンセル', command=win.destroy).pack(side='right', padx=(10, 0))
    def save():
        try:
            requested = LANGUAGES[selection.get()]
            cfg = replace(app.config, ui_language=requested)
            save_config(cfg)
            app.config = cfg
            set_language(requested)
            win.destroy()
            translate_widgets(app.root, requested)
        except (KeyError, OSError, ValueError) as error:
            show_error(win, str(error))
    IconButton(footer, text='Apply / 適用', command=save, style='Primary.TButton').pack(side='right')
    return win
