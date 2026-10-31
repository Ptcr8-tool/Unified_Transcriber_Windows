"""Main-thread Tk update controls; all network and signature work stays off Tk."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import shutil
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

import app_updates
import core
from ui_icons import IconButton

INTERVALS = {'指定した日数ごと': 'days', '起動するたび': 'every', '確認しない': 'never'}


class UpdateController:
    """Explicitly started by main(); constructing UnifiedApp never contacts GitHub."""
    def __init__(self, app):
        self.app, self.root = app, app.root
        self.events = queue.Queue()
        self.closed = threading.Event()
        self.download_cancelled = threading.Event()
        self.checking = False
        self.downloading = False
        self.started = False
        self.attempted = False
        self.pending = None
        self.jobs = set()
        self.settings = None
        self.progress = None

    def _after(self, delay, callback):
        if self.closed.is_set():
            return
        job = None
        def invoke():
            self.jobs.discard(job)
            if not self.closed.is_set() and not getattr(self.app, 'closing', False):
                callback()
        try:
            job = self.root.after(delay, invoke)
            self.jobs.add(job)
        except tk.TclError:
            self.closed.set()

    def start(self):
        if self.started or self.closed.is_set():
            return
        self.started = True
        self._after(5000, self._startup_check)
        self._after(150, self._poll)

    def _startup_check(self):
        if self.attempted:
            return
        cfg = self.app.config
        if app_updates.is_check_due(cfg.update_check_mode, cfg.update_check_days,
                                    cfg.update_last_checked):
            self.check(manual=False)

    def _log(self, text):
        if not self.closed.is_set():
            self.app.log('[更新] '+text+'\n')

    def _status(self, text):
        if self.settings is not None:
            try:
                self.settings.status.set(text)
            except tk.TclError:
                pass
        if not self._busy():
            self.app.status.set(text)

    def _busy(self):
        processes = getattr(self.app, 'processes', None)
        return bool(getattr(self.app, 'busy', False) or
                    (processes is not None and processes.active()))

    def _can_prompt(self):
        if self._busy():
            return False
        if getattr(self.app, 'in_tray', False):
            return False  # Show the update prompt when the user returns to the app.
        own = self.settings.win if self.settings is not None else None
        try:
            return not any(isinstance(win, tk.Toplevel) and win != own and win.winfo_viewable()
                           for win in self.root.winfo_children())
        except tk.TclError:
            return False

    def check(self, manual=True):
        if self.closed.is_set() or getattr(self.app, 'closing', False):
            return False
        self.start()
        if self.checking or self.downloading:
            if manual:
                self._status('更新を確認・準備しています。完了までお待ちください。')
            return False
        self.checking = True
        self.attempted = True
        self._status('GitHubで新しいバージョンを確認しています…')
        # Record an attempt so an unavailable private/offline source does not
        # trigger a retry every launch. "Every launch" remains every launch.
        timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        cfg = replace(self.app.config, update_last_checked=timestamp)
        self.app.config = cfg
        try:
            core.save_config(cfg)
        except (OSError, ValueError, TypeError):
            self._log('更新確認日時を保存できませんでした。')
        def worker():
            try:
                result = app_updates.check_latest(core.APP_VERSION)
                event = ('checked', result, manual)
            except (app_updates.UpdateError, OSError, ValueError) as exc:
                event = ('check-error', str(exc), manual)
            except Exception:
                event = ('check-error', '更新情報を確認できませんでした。アプリは現在のバージョンで継続できます。', manual)
            if not self.closed.is_set():
                self.events.put(event)
        threading.Thread(target=worker, daemon=True, name='public-update-check').start()
        return True

    def _poll(self):
        if self.closed.is_set():
            return
        try:
            while True:
                kind, value, manual = self.events.get_nowait()
                if kind == 'checked':
                    self.checking = False
                    if value is None:
                        self._status('現在のバージョンは最新です。')
                        if manual:
                            messagebox.showinfo('更新の確認', '現在のバージョンは最新です。', parent=self.root)
                    else:
                        self.pending = (value, manual)
                        self._status('新しいバージョンがあります。処理の終了後にご案内します。')
                elif kind == 'check-error':
                    self.checking = False
                    self._log(value)
                    if manual:
                        self._status('更新情報を取得できませんでした。')
                        messagebox.showinfo('更新の確認', value, parent=self.root)
                elif kind == 'downloaded':
                    self.downloading = False
                    self._close_progress()
                    if self.download_cancelled.is_set():
                        self._discard(value)
                        self._status('更新の準備をキャンセルしました。')
                    else:
                        self._ready(value)
                elif kind == 'download-error':
                    self.downloading = False
                    self._close_progress()
                    self._log(value)
                    if not self.download_cancelled.is_set():
                        messagebox.showerror('更新を中止しました', value, parent=self.root)
                    self._status('更新は実行していません。')
        except queue.Empty:
            pass
        if self.pending and self._can_prompt():
            info, _manual = self.pending
            self.pending = None
            self._show_available(info)
        self._after(150, self._poll)

    def _show_available(self, info):
        UpdateAvailableDialog(self, info)

    def download(self, info):
        if self.closed.is_set() or self.checking or self.downloading:
            return False
        if self._busy():
            messagebox.showinfo('更新の準備', '録音や文字起こしなどの処理を終了してから更新してください。', parent=self.root)
            return False
        if not info.has_provenance:
            messagebox.showerror('自動更新を実行できません',
                '署名付きビルド証明がないため、自動更新は実行しません。', parent=self.root)
            return False
        self.downloading = True
        self.download_cancelled.clear()
        self.progress = UpdateProgressDialog(self)
        self._status('更新をダウンロードして署名を確認しています…')
        directory = core.app_config_path().parent / 'Updates'
        def worker():
            try:
                result = app_updates.download_verified(info, directory)
                if self.closed.is_set() or self.download_cancelled.is_set():
                    self._discard(result)
                    if not self.closed.is_set():
                        self.events.put(('downloaded', result, True))
                    return
                event = ('downloaded', result, True)
            except (app_updates.UpdateError, OSError, ValueError) as exc:
                event = ('download-error', str(exc), True)
            except Exception:
                event = ('download-error', '更新の検証を完了できませんでした。更新は実行していません。', True)
            if not self.closed.is_set():
                self.events.put(event)
        threading.Thread(target=worker, daemon=True, name='verified-update-download').start()
        return True

    @staticmethod
    def _discard(update):
        # Only delete the dedicated directory returned by download_verified.
        path = Path(update.path)
        if path.parent.name.startswith('verified-update-') and path.parent.is_dir() and not path.parent.is_symlink():
            shutil.rmtree(path.parent, ignore_errors=True)

    def cancel_download(self):
        self.download_cancelled.set()
        if self.progress is not None:
            self.progress.status.set('キャンセルを受け付けました。検証処理の終了を待っています…')

    def _ready(self, update):
        if self._busy():
            self._discard(update)
            messagebox.showinfo('更新の準備', '処理が実行中のため更新を開始しませんでした。処理の終了後に再度確認してください。', parent=self.root)
            return
        if not messagebox.askyesno('更新の準備ができました',
                f'v{update.info.version} の署名とファイルを確認しました。\n\n'
                'アプリを終了してセットアップを開きますか？\n'
                '既存の設定・AI環境・モデル・保存データを引き継ぎます。', parent=self.root):
            self._discard(update)
            return
        # The modal blocks normal UI interaction, but check again at execution.
        if self._busy():
            self._discard(update)
            return
        try:
            app_updates.launch_installer(update, core.BASE, wait_pid=os.getpid())
        except (app_updates.UpdateError, OSError, ValueError) as exc:
            self._discard(update)
            messagebox.showerror('更新を開始できません', str(exc), parent=self.root)
            return
        self.close()
        # A normal window close may only hide the app in the notification
        # area. The installer waits for this process, so explicitly exit.
        self.app.request_exit()

    def open_release(self, info):
        app_updates.parse_version(info.version)
        if info.tag != 'v'+info.version or info.release_url != app_updates.RELEASE_BASE+'tag/'+info.tag:
            raise app_updates.UpdateSecurityError('更新案内のURLが不正です。')
        webbrowser.open(info.release_url, new=2)

    def show_settings(self):
        if self.closed.is_set():
            return
        self.start()
        if self.settings is not None:
            try:
                self.settings.win.lift()
                return self.settings
            except tk.TclError:
                self.settings = None
        self.settings = UpdateSettingsDialog(self)
        return self.settings

    def _close_progress(self):
        if self.progress is not None:
            try:
                self.progress.win.destroy()
            except tk.TclError:
                pass
            self.progress = None

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self.download_cancelled.set()
        for job in self.jobs:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self.jobs.clear()
        self.pending = None
        self._close_progress()


class UpdateSettingsDialog:
    def __init__(self, controller):
        from unified_gui import modal, theme_widgets
        self.controller = controller
        app = controller.app
        self.win = modal(app.root, 'アプリの更新', 700)
        self.win.geometry('700x510')
        frame = ttk.Frame(self.win, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='アプリを最新の状態に', style='Section.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Label(frame, text=f'現在のバージョン：v{core.APP_VERSION}', style='Muted.TLabel').pack(anchor='w')
        ttk.Label(frame, text='起動時にGitHubで新しい正式版を確認します。\n'
                  'ダウンロードや更新の実行前には、確認画面を表示します。',
                  wraplength=650).pack(anchor='w', pady=(12, 20))
        label = next((key for key, value in INTERVALS.items() if value == app.config.update_check_mode), '指定した日数ごと')
        self.mode = tk.StringVar(self.win, label)
        self.days = tk.StringVar(self.win, str(app.config.update_check_days))
        ttk.Label(frame, text='更新チェックの間隔').pack(anchor='w', pady=(0, 6))
        row = ttk.Frame(frame)
        row.pack(fill='x')
        choice = ttk.Combobox(row, textvariable=self.mode, values=tuple(INTERVALS), state='readonly', width=24)
        choice.pack(side='left')
        self.day_input = ttk.Spinbox(row, from_=1, to=365, textvariable=self.days, width=6)
        self.day_input.pack(side='left', padx=(15, 6))
        ttk.Label(row, text='日ごと').pack(side='left')
        def refresh(_event=None):
            self.day_input.configure(state='normal' if INTERVALS.get(self.mode.get()) == 'days' else 'disabled')
        choice.bind('<<ComboboxSelected>>', refresh)
        refresh()
        self.status = tk.StringVar(self.win, '')
        ttk.Label(frame, textvariable=self.status, wraplength=650, style='Muted.TLabel').pack(anchor='w', pady=(18, 8))
        ttk.Label(frame, text='更新は配布元・署名・チェックサムを確認してから準備します。\n'
                  '公開更新情報がない場合や検証に失敗した場合は、更新を実行しません。',
                  wraplength=650, style='Muted.TLabel').pack(anchor='w', pady=(0, 12))
        foot = ttk.Frame(frame)
        foot.pack(side='bottom', fill='x')
        IconButton(foot, text='今すぐ確認', icon='refresh', command=lambda:controller.check(manual=True)).pack(side='left')
        IconButton(foot, text='閉じる', command=self.close).pack(side='right', padx=(8, 0))
        IconButton(foot, text='設定を保存', style='Primary.TButton', command=self.save).pack(side='right')
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        self.win.bind('<Escape>', lambda _event: self.close())
        theme_widgets(self.win)

    def save(self):
        try:
            mode = INTERVALS[self.mode.get()]
            raw = self.days.get().strip()
            if not raw.isdecimal():
                raise ValueError('更新チェックの間隔は1〜365日で指定してください。')
            days = int(raw)
            app_updates.is_check_due(mode, days)
            cfg = replace(self.controller.app.config, update_check_mode=mode, update_check_days=days)
            core.save_config(cfg)
            self.controller.app.config = cfg
            self.status.set('更新確認の設定を保存しました。')
        except (KeyError, ValueError, OSError, TypeError) as exc:
            messagebox.showerror('設定を保存できません', str(exc), parent=self.win)

    def close(self):
        self.controller.settings = None
        self.win.destroy()


class UpdateAvailableDialog:
    def __init__(self, controller, info):
        from unified_gui import modal, theme_widgets
        self.controller, self.info = controller, info
        self.win = modal(controller.root, '新しいバージョンがあります', 680)
        self.win.geometry('680x440')
        frame = ttk.Frame(self.win, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=f'v{info.version} に更新できます', style='Section.TLabel').pack(anchor='w', pady=(0, 14))
        ttk.Label(frame, text=f'現在：v{core.APP_VERSION}    更新後：v{info.version}\n'
                  f'ダウンロード：約 {info.setup_size / 1024 / 1024:.1f} MB').pack(anchor='w', pady=(0, 18))
        explanation = ('設定・AI環境・モデル・保存データを引き継いで更新します。\n'
                       'ダウンロードと署名の確認後、アプリを終了してセットアップを開きます。')
        if not info.has_provenance:
            explanation = ('このリリースには署名付きビルド証明がありません。\n'
                           '自動更新は実行しません。リリース情報から内容をご確認ください。')
        ttk.Label(frame, text=explanation, wraplength=625, style='Muted.TLabel').pack(anchor='w', pady=(0, 18))
        foot = ttk.Frame(frame)
        foot.pack(side='bottom', fill='x')
        IconButton(foot, text='リリース情報を見る', icon='link', command=lambda:controller.open_release(info)).pack(side='left')
        IconButton(foot, text='後で', icon='clock', command=self.win.destroy).pack(side='right', padx=(8, 0))
        if info.has_provenance:
            IconButton(foot, text='ダウンロードして更新', icon='download', style='Primary.TButton', command=self.download).pack(side='right')
        theme_widgets(self.win)

    def download(self):
        self.win.destroy()
        self.controller.download(self.info)


class UpdateProgressDialog:
    def __init__(self, controller):
        from unified_gui import modal, theme_widgets
        self.win = modal(controller.root, '更新を準備しています', 650)
        self.win.geometry('650x280')
        frame = ttk.Frame(self.win, padding=24)
        frame.pack(fill='both', expand=True)
        self.status = tk.StringVar(self.win, 'ダウンロードと署名の検証を行っています…')
        ttk.Label(frame, textvariable=self.status, wraplength=595, style='Section.TLabel').pack(anchor='w', pady=(0, 18))
        bar = ttk.Progressbar(frame, mode='indeterminate')
        bar.pack(fill='x', pady=(0, 14))
        bar.start(15)
        ttk.Label(frame, text='完了するまで、アプリの更新は実行されません。', style='Muted.TLabel').pack(anchor='w')
        IconButton(frame, text='キャンセル', command=controller.cancel_download).pack(side='right', anchor='s', pady=(20, 0))
        self.win.protocol('WM_DELETE_WINDOW', controller.cancel_download)
        self.win.bind('<Escape>', lambda _event: controller.cancel_download())
        theme_widgets(self.win)
