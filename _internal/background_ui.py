"""User-controlled startup and notification-area behavior."""
from dataclasses import replace
import sys
import tkinter as tk
from tkinter import ttk

from core import BASE, save_config
from ui_controls import ToggleSwitch
from ui_icons import IconButton
from windows_background import startup_enabled, set_startup


class BackgroundSettingsDialog:
    def __init__(self, app):
        from unified_gui import modal, show_error
        self.app = app
        self.win = modal(app.root, '自動起動・常駐の設定', 680)
        self.win.geometry('680x480')
        panel = ttk.Frame(self.win, padding=22); panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='自動起動・バックグラウンド待機', style='Section.TLabel').pack(anchor='w')
        ttk.Label(panel, text='Windows にサインインしたとき、通知領域で待機できます。\n'
                  '通知領域のアイコンから画面を開く、録音、停止、終了ができます。',
                  style='Muted.TLabel', wraplength=620).pack(anchor='w', pady=(8, 16))
        try:
            registered = startup_enabled(BASE)
        except OSError as exc:
            registered = False
            show_error(self.win, '自動起動の状態を確認できませんでした: ' + str(exc))
        self.registered = registered
        self.startup = tk.BooleanVar(self.win, registered)
        self.minimize = tk.BooleanVar(self.win, app.config.tray_minimize)
        self.close_to_tray = tk.BooleanVar(self.win, app.config.tray_close)
        for text, variable in (('Windows サインイン時に自動起動して待機', self.startup),
                               ('最小化したとき通知領域に格納', self.minimize),
                               ('閉じるボタンで通知領域に格納', self.close_to_tray)):
            switch = ToggleSwitch(panel, text=text, variable=variable)
            switch.pack(anchor='w', pady=4)
            if sys.platform != 'win32':switch.state(['disabled'])
        ttk.Label(panel, text='録音用のショートカットを「設定・診断 → 操作・設定」で設定してください。\n'
                  '設定画面をスキップしない場合と Game Bar の対象選択時は画面が開きます。\n'
                  '完全に終了するには通知領域アイコンを右クリックして「アプリを終了」を選びます。',
                  style='Muted.TLabel', wraplength=620).pack(anchor='w', pady=(14, 8))
        footer = ttk.Frame(panel); footer.pack(side='bottom', fill='x')
        IconButton(footer, text='キャンセル', command=self.win.destroy).pack(side='right', padx=(8, 0))
        IconButton(footer, text='保存', style='Primary.TButton', command=self.save).pack(side='right')

    def save(self):
        from unified_gui import show_error
        enabled = self.startup.get()
        changed = enabled != self.registered
        cfg = replace(self.app.config, tray_minimize=self.minimize.get(), tray_close=self.close_to_tray.get())
        try:
            cfg.validate()
            if (enabled or cfg.tray_minimize or cfg.tray_close) and not self.app.ensure_tray():
                raise OSError(self.app.tray.error or '通知領域アイコンを作成できませんでした。')
            if changed:set_startup(BASE, enabled)
            try:
                save_config(cfg)
            except (OSError, ValueError):
                if changed:set_startup(BASE, self.registered)
                raise
            self.app.config = cfg
            self.win.destroy()
        except (OSError, ValueError) as exc:
            show_error(self.win, str(exc))
