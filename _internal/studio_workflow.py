"""First-run setup and completed-transcript navigation, separate from recognition."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tkinter import ttk
from ui_icons import IconButton

from core import save_config
from studio_ui import UI_FONT


def open_folder(path: Path):
    path = Path(path).expanduser()
    folder = path if path.is_dir() else path.parent
    if not folder.is_dir():
        raise FileNotFoundError(f'フォルダーが見つかりません。移動・削除されている可能性があります。\n{folder}')
    if sys.platform == 'win32':
        os.startfile(str(folder))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(folder)])
    else:
        subprocess.Popen(['xdg-open', str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WelcomeDialog:
    def __init__(self, app):
        from unified_gui import modal
        self.app = app
        self.win = modal(app.root, 'はじめにセットアップしてください', 690)
        self.win.geometry('690x430')
        body = ttk.Frame(self.win, padding=24)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='まずは、使う機能をセットアップ',
                  font=(UI_FONT, 17, 'bold')).pack(anchor='w', pady=(0, 16))
        ttk.Label(body, text='文字起こし用の実行環境・AIモデルと、録音環境を準備します。\n'
            '一括セットアップで使う方式とCPU／GPUを選んでください。\n'
            'すでに導入済みの環境・モデルは引き続き利用できます。',
            wraplength=625).pack(anchor='w', pady=(0, 18))
        ttk.Label(body, text='あとから「設定・診断 → 一括セットアップ」で開けます。\n'
            '録音・Game Bar連動は、TOPのボタンから初回の録音環境確認へ進めます。',
            style='Muted.TLabel', wraplength=625).pack(anchor='w')
        actions = ttk.Frame(body)
        actions.pack(side='bottom', fill='x')
        IconButton(actions, text='後で', command=self.dismiss).pack(side='right', padx=(9, 0))
        IconButton(actions, text='一括セットアップへ', style='Primary.TButton',
                   command=self.start).pack(side='right')
        self.win.protocol('WM_DELETE_WINDOW', self.dismiss)
        self.win.bind('<Escape>', lambda _e: self.dismiss())

    def dismiss(self):
        self.app.config.onboarding_seen = True
        save_config(self.app.config)
        self.win.destroy()

    def start(self):
        from unified_gui import BulkSetupDialog
        self.dismiss()
        BulkSetupDialog(self.app)


class CaptureSetupDialog:
    def __init__(self, app, target=None):
        from unified_gui import modal
        self.app, self.target = app, target
        self.win = modal(app.root, '録音・録画の初回セットアップ', 690)
        self.win.geometry('690x430')
        body = ttk.Frame(self.win, padding=24)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='録音・録画を使う準備', font=(UI_FONT, 17, 'bold')).pack(
            anchor='w', pady=(0, 16))
        ttk.Label(body, text='マイク・PC再生音の録音に必要な環境を確認します。\n'
            '不足するものがあれば導入します。AIモデルの再導入は行いません。\n\n'
            '正常に完了すると、次回からこの画面をスキップして開きます。\n'
            '「録音・録画 → 録音・録画のセットアップ」からいつでも再実行できます。',
            wraplength=625).pack(anchor='w')
        actions = ttk.Frame(body)
        actions.pack(side='bottom', fill='x')
        IconButton(actions, text='閉じる', command=self.win.destroy).pack(side='right', padx=(9, 0))
        IconButton(actions, text='確認・セットアップして続ける', style='Primary.TButton',
                   command=self.start).pack(side='right')

    def start(self):
        if self.app.busy:
            return
        self.app.pending_capture_target = self.target
        self.app.capture_verified = False
        self.app.config.capture_setup_complete = False
        save_config(self.app.config)
        self.win.destroy()
        self.app.start_capture_setup()


class ResultDialog:
    def __init__(self, app, key):
        from unified_gui import modal
        self.app, self.key = app, key
        source = app.job_sources[key]
        self.win = modal(app.root, '文字起こし結果 · '+source.name, 800)
        body = ttk.Frame(self.win, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=source.name, font=(UI_FONT, 14, 'bold'),
                  wraplength=740).pack(anchor='w', pady=(0, 12))
        ttk.Label(body, text='元ファイル', style='Section.TLabel').pack(anchor='w')
        ttk.Label(body, text=str(source), wraplength=740, style='Muted.TLabel').pack(
            anchor='w', pady=(4, 6))
        IconButton(body, text='元ファイルの場所を開く',
                   command=lambda: app.open_job_location(key, original=True)).pack(anchor='w')
        ttk.Label(body, text='保存された文字起こし', style='Section.TLabel').pack(
            anchor='w', pady=(22, 8))
        outputs = app.job_outputs.get(key, ())
        for path in outputs:
            ttk.Label(body, text=str(path), wraplength=740).pack(anchor='w', pady=(3, 5))
        if not outputs:
            ttk.Label(body, text='この起動中に作成した出力の記録がありません。',
                      style='Muted.TLabel').pack(anchor='w')
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(12, 0))
        IconButton(actions, text='結果の保存先を開く',
                   state='normal' if outputs else 'disabled',
                   command=lambda: app.open_job_location(key)).pack(side='left')
        def edit():
            self.win.destroy()
            app.edit_job_speakers(key)
        IconButton(actions, text='この結果の話者名を設定', style='Primary.TButton',
                   state='normal' if outputs and not app.busy else 'disabled',
                   command=edit).pack(side='left', padx=10)
        IconButton(body, text='閉じる', command=self.win.destroy).pack(side='bottom', anchor='e')
