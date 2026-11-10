"""Key capture UI shared by app shortcuts and the Game Bar trigger."""
import tkinter as tk
from tkinter import ttk
from ui_icons import IconButton
from dataclasses import replace
from app_hotkeys import (ACTION_LABELS, normalize_hotkey, tk_shortcut,
                         validate_shortcuts, probe_global_hotkey)
from core import save_config


class KeyCaptureDialog:
    def __init__(self, parent, current, apply, manager=None, allow_function_only=True,
                 validator=None):
        from unified_gui import modal
        self.previous_grab=parent.grab_current()
        self.manager=manager
        self.allow_function_only=allow_function_only
        self.validator=validator
        if manager:
            manager.suspend(True)
        self.win=modal(parent,'ショートカットキーを入力',570)
        self.win.minsize(480,280)
        self.win.geometry('570x310')
        self.pressed=set()
        self.value=tk.StringVar(self.win,normalize_hotkey(current))
        panel=ttk.Frame(self.win,padding=22);panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='割り当てたいキーを押してください',style='Section.TLabel').pack(anchor='w')
        ttk.Label(panel,text='修飾キー＋文字／Fキーを押してください。'+
                  ('F1～F11は単独でも使えます。' if allow_function_only else ''),
                  wraplength=520,style='Muted.TLabel').pack(anchor='w',pady=(8,12))
        ttk.Label(panel,textvariable=self.value,font=('Segoe UI',17,'bold'),padding=14).pack(fill='x')
        self.availability=tk.StringVar(self.win,'')
        ttk.Label(panel,textvariable=self.availability,wraplength=510,
                  style='Muted.TLabel').pack(anchor='w',pady=(5,8))
        foot=ttk.Frame(panel);foot.pack(side='bottom',fill='x')
        IconButton(foot,text='解除',command=lambda:self.value.set('')).pack(side='left')
        IconButton(foot,text='キャンセル',command=self.close).pack(side='right',padx=(8,0))
        def accept():
            if self._check_available():
                apply(self.value.get())
                self.close()
        self.apply_button=IconButton(foot,text='このキーを使う',style='Primary.TButton',command=accept)
        self.apply_button.pack(side='right')
        self.value.trace_add('write',lambda *_:self._check_available())
        self._check_available()
        self.win.bind('<KeyPress>',self._press)
        self.win.bind('<KeyRelease>',self._release)
        self.win.protocol('WM_DELETE_WINDOW',self.close)
        self.win.bind('<Escape>',lambda _e:self.close())
        self.win.bind('<Destroy>',lambda e:self._restore() if e.widget is self.win else None,add='+')
        self.win.after_idle(self.win.focus_force)

    def _check_available(self):
        value=self.value.get()
        try:
            if self.validator is not None:
                self.validator(value)
            available, explanation = probe_global_hotkey(value) if self.validator is not None else (
                True, '録画用のキーはWindowsのGame Bar側でも設定してください。')
            allowed = available is not False
        except (ValueError, OSError, AttributeError) as exc:
            allowed, explanation = False, str(exc)
        self.availability.set(explanation)
        self.apply_button.state(['!disabled'] if allowed else ['disabled'])
        return allowed

    def _press(self,event):
        value=tk_shortcut(event,self.pressed,self.allow_function_only)
        if value:
            self.value.set(value)
        return 'break'

    def _release(self,event):
        self.pressed.clear()
        return 'break'

    def _restore(self):
        if self.manager:
            self.manager.suspend(False);self.manager=None
        if self.previous_grab is not None:
            try:
                if self.previous_grab.winfo_exists():self.previous_grab.grab_set()
            except tk.TclError:pass

    def close(self):
        self.win.destroy()


class ShortcutSettingsDialog:
    def __init__(self,app):
        from unified_gui import modal, show_error
        self.app=app
        previous_grab=app.root.grab_current()
        self.win=modal(app.root,'録音・録画のショートカット',740)
        def restore(event):
            if event.widget is self.win and previous_grab is not None:
                try:
                    if previous_grab.winfo_exists():previous_grab.grab_set()
                except tk.TclError:pass
        self.win.bind('<Destroy>',restore,add='+')
        self.win.geometry('740x430')
        panel=ttk.Frame(self.win,padding=22);panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='録音・録画のショートカット',style='Section.TLabel').pack(anchor='w')
        ttk.Label(panel,text='Windowsでは他のアプリを操作中でも利用できます。\n開始時の設定はTOPボタンと共通です。',
                  wraplength=690,style='Muted.TLabel').pack(anchor='w',pady=(8,16))
        self.variables={}
        for key,label in ACTION_LABELS.items():
            row=ttk.Frame(panel);row.pack(fill='x',pady=7)
            ttk.Label(row,text=label,width=26).pack(side='left')
            var=self.variables[key]=tk.StringVar(self.win,app.config.shortcut_keys.get(key,''))
            ttk.Entry(row,textvariable=var,state='readonly').pack(side='left',fill='x',expand=True)
            def validate_candidate(candidate, action=key):
                values={name:normalize_hotkey(v.get()) for name,v in self.variables.items()}
                values[action]=normalize_hotkey(candidate)
                validate_shortcuts(values,app.config.gamebar_shortcut)
            IconButton(row,text='キー入力…',command=lambda v=var, f=validate_candidate:KeyCaptureDialog(
                self.win,v.get(),v.set,getattr(app,'hotkeys',None),validator=f)).pack(side='left',padx=(8,0))
        foot=ttk.Frame(panel);foot.pack(side='bottom',fill='x')
        def save():
            try:
                values={key:normalize_hotkey(var.get()) for key,var in self.variables.items()}
                validate_shortcuts(values,app.config.gamebar_shortcut)
                # Free this application's own registrations, then ask Windows
                # whether *every* candidate can be registered. Do not persist
                # shortcuts that are already owned by another application.
                manager=getattr(app,'hotkeys',None)
                if manager:
                    manager.suspend(True)
                try:
                    for action,value in values.items():
                        ok,message=probe_global_hotkey(value)
                        if ok is False:
                            raise ValueError(f'{ACTION_LABELS[action]}: {message}')
                    cfg=replace(app.config,shortcut_keys=values)
                    save_config(cfg);app.config=cfg;app.configure_shortcuts()
                finally:
                    if manager:
                        manager.suspend(False)
                self.win.destroy()
            except (ValueError,OSError,AttributeError) as exc:
                show_error(self.win,str(exc))
        IconButton(foot,text='キャンセル',command=self.win.destroy).pack(side='right',padx=(8,0))
        IconButton(foot,text='保存',style='Primary.TButton',command=save).pack(side='right')
