"""Export/import preferences without rebuilding the workspace or clearing its queue."""
import json
import sys
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog,messagebox,ttk

import core
from settings_transfer import export_settings,import_settings,write_export
from ui_controls import ToggleSwitch,system_is_dark
from ui_icons import IconButton


def confirm_imported_python_overrides(cfg, current, parent):
    """Require explicit consent before imported executable paths can be run."""
    existing = current.python_overrides
    changed = {profile: path for profile, path in cfg.python_overrides.items()
               if path and existing.get(profile) != path}
    if not changed:
        return cfg, False
    choices = '\n'.join(f'{profile}: {path!r}' for profile, path in sorted(changed.items()))
    approved = messagebox.askyesno('Python実行環境の確認',
        '設定ファイルに外部のPython実行ファイルが指定されています。'
        '文字起こし時にこれらのファイルが実行されます。\n\n'
        + choices + '\n\n信頼できるファイルの場合だけ適用してください。'
        '\n「いいえ」を選ぶと、現在のPython設定を維持して、他の設定を読み込みます。',
        parent=parent, default=messagebox.NO)
    if approved:
        return cfg, False
    safe = replace(cfg, python_overrides=dict(existing))
    safe.validate()
    return safe, True


def save_import_with_credential(cfg, token, persist):
    """Import an explicitly included token without writing it to settings.json.

    On a failed preference save restore the previous vault entry; never leave
    a successful-looking import with settings and credential out of sync.
    """
    from hf_auth import credential_guard
    with credential_guard():
        return _save_import_with_credential_locked(cfg, token, persist)


def _save_import_with_credential_locked(cfg, token, persist):
    from hf_auth import read_credential, write_credential, delete_credential
    previous = read_credential()
    try:
        write_credential(token)
        if read_credential() != token:
            raise OSError('保存した資格情報を確認できません。')
        cleaned = replace(cfg, hf_token_json='')
        persist(cleaned)
        return cleaned
    except (OSError, ValueError, TypeError):
        try:
            if previous:
                write_credential(previous)
            else:
                delete_credential()
        except (OSError, ValueError):
            raise OSError('設定の読み込みに失敗し、以前の資格情報も復元できませんでした。') from None
        raise


class SettingsExportDialog:
    def __init__(self,app):
        from unified_gui import modal
        self.app=app
        self.win=modal(app.root,'設定を書き出す',680)
        self.win.geometry('680x440')
        body=ttk.Frame(self.win,padding=20);body.pack(fill='both',expand=True)
        ttk.Label(body,text='設定をほかの環境へ',style='Section.TLabel').pack(anchor='w',pady=(0,10))
        ttk.Label(body,text='認識方式・モデル、録音、保存先、表示、ショートカットを1つの設定ファイルにまとめます。'
            'アプリやユーザーのフォルダー内の保存先は、読み込み先の場所に合わせて反映します。',
            wraplength=620,style='Muted.TLabel').pack(anchor='w',pady=(0,18))
        self.path=tk.StringVar(self.win,str(core.default_directory('exports')/'transcriber-settings.json'))
        row=ttk.Frame(body);row.pack(fill='x')
        ttk.Entry(row,textvariable=self.path).pack(side='left',fill='x',expand=True)
        IconButton(row,text='参照…',command=self.browse).pack(side='left',padx=(8,0))
        self.include_token=tk.BooleanVar(self.win,False)
        ToggleSwitch(body,text='認証トークンも含める（ファイルへ平文で保存）',
            variable=self.include_token).pack(anchor='w',pady=(17,5))
        ttk.Label(body,text='録音データやAIモデル・実行環境は含みません。録音デバイスは読み込み先のPCの既定を使います。',
            wraplength=620,style='Muted.TLabel').pack(anchor='w',pady=(4,10))
        foot=ttk.Frame(body);foot.pack(side='bottom',fill='x')
        IconButton(foot,text='キャンセル',command=self.win.destroy).pack(side='right',padx=(8,0))
        IconButton(foot,text='設定を書き出す',icon='archive',style='Primary.TButton',command=self.save).pack(side='right')

    def browse(self):
        selected=filedialog.asksaveasfilename(parent=self.win,title='設定の書き出し先',
            initialdir=str(core.default_directory('exports')),initialfile='transcriber-settings.json',defaultextension='.json',
            filetypes=[('設定ファイル','*.json')])
        if selected:self.path.set(selected)

    def save(self):
        from unified_gui import show_error
        try:
            if not self.path.get().strip():raise ValueError('書き出し先を指定してください。')
            target=Path(self.path.get()).expanduser()
            data=export_settings(self.app.config,include_token=self.include_token.get(),token=self.app.hf_token)
            write_export(target,data)
            messagebox.showinfo('書き出し完了',f'設定を書き出しました。\n{target}',parent=self.win)
            self.win.destroy()
        except (OSError,ValueError,TypeError) as exc:show_error(self.win,str(exc))


def load_settings_file(app,source=None):
    from unified_gui import PROFILE_NAMES,show_error,save_preferences_with_output
    from resource_limits import write_gpu_pacing
    if app.busy:return
    app.ui.close_drawers()
    if source is None:
        source=filedialog.askopenfilename(parent=app.root,title='設定を読み込む',
            initialdir=str(core.default_directory('exports')),filetypes=[('設定ファイル','*.json'),('すべてのファイル','*.*')])
    if not source:return
    try:
        with Path(source).open('rb') as stream:
            raw=stream.read(core.MAX_CONFIG_BYTES+1)
        if len(raw)>core.MAX_CONFIG_BYTES:
            raise ValueError('設定ファイルが大きすぎます。')
        data=json.loads(raw.decode('utf-8-sig'))
        cfg,missing=import_settings(data,app.config)
        cfg,kept_python=confirm_imported_python_overrides(cfg,app.config,app.root)
        values=data.get('settings', {}) if isinstance(data,dict) and 'format' in data else data
        imported_token=(values.get('hf_token_json', '').strip()
                        if isinstance(values,dict) and isinstance(values.get('hf_token_json'),str) else '')
        if imported_token and sys.platform == 'win32':
            cfg=save_import_with_credential(cfg, imported_token,
                lambda value:save_preferences_with_output(app,value,persist=core.save_config))
        else:
            save_preferences_with_output(app,cfg,persist=core.save_config)
    except (OSError,ValueError,TypeError,KeyError,RecursionError) as exc:
        show_error(app.root,'設定を読み込めませんでした。\n'+str(exc));return
    app.hf_token=imported_token or cfg.hf_token_json or app.hf_token
    app.diagnostics.token=app.hf_token
    app.device_cache.clear()
    if not cfg.auto_open_speaker:app.pending_speaker_edits.clear()
    app.profile_var.set(PROFILE_NAMES[cfg.profile])
    app.ui.auto_speaker_var.set(cfg.auto_open_speaker)
    app.ui.theme_var.set(cfg.theme_mode)
    app.ui._apply_appearance(system_is_dark() if cfg.theme_mode=='system' else cfg.theme_mode=='dark')
    app.configure_shortcuts()
    write_gpu_pacing(app.resource_control_file,cfg.gpu_soft_percent)
    app.update_profile_display()
    app.status.set('設定を読み込みました。保存先をこの環境に合わせて反映しました。')
    message='設定を読み込みました。\n保存先はこの環境に合わせて反映しました。'
    if missing and not kept_python:message+='\n移行先にないPythonの割り当ては解除しました。'
    if kept_python:message+='\nPython実行環境は読み込まず、現在の設定を維持しました。'
    message+='\nAI・録音環境が未導入の場合は、環境セットアップを実行してください。'
    messagebox.showinfo('読み込み完了',message,parent=app.root)
    return cfg
