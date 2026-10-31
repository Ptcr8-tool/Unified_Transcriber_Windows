"""Presentation layer for the desktop studio. No model or recording dependencies."""
from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
import webbrowser
from ui_icons import IconButton, IconMenu, ACTION_ICONS, set_icon_palette, refresh_icon
from ui_controls import AccordionButton, ToggleSwitch, IconThemeToggle, install_switch_style, system_is_dark

UI_FONT = 'Yu Gothic UI' if sys.platform == 'win32' else 'Noto Sans CJK JP'
LIGHT = {
    'app':'#f3f6fb', 'surface':'#ffffff', 'ink':'#17283f', 'muted':'#61738a',
    'line':'#e0e7f0', 'blue':'#2563eb', 'blue_hover':'#1d4ed8', 'blue_light':'#edf3ff',
    'green':'#209760', 'green_light':'#eaf7ef', 'red':'#b53845', 'red_light':'#fff0f1',
    'amber':'#8b601c', 'amber_light':'#fff7e7', 'button':'#f7f9fc', 'hover':'#edf2fa',
    'disabled':'#e6ebf2', 'selected':'#dbe8ff', 'heading':'#f0f4fa', 'row_alt':'#f8fafd',
    'scroll':'#d3dce9', 'switch_off':'#b9c2cd', 'disabled_ink':'#95a1b1',
}
DARK = {
    'app':'#101722', 'surface':'#182230', 'ink':'#e6edf7', 'muted':'#a8b7cc',
    'line':'#344257', 'blue':'#76a6ff', 'blue_hover':'#528ced', 'blue_light':'#233b60',
    'green':'#43cb91', 'green_light':'#193d32', 'red':'#ff9ca8', 'red_light':'#452a35',
    'amber':'#f1c576', 'amber_light':'#423827', 'button':'#263447', 'hover':'#344760',
    'disabled':'#2a3544', 'selected':'#34547d', 'heading':'#223044', 'row_alt':'#1c2939',
    'scroll':'#465872', 'switch_off':'#556375', 'disabled_ink':'#75859b',
}
COLORS = dict(LIGHT)


def open_illustrated_guide(parent):
    """Open the bundled illustrated guide directly from the main screen."""
    guide = Path(__file__).resolve().parent / 'docs' / 'GUIDE.html'
    if not guide.is_file():
        messagebox.showerror('操作ガイド', '画面付きの操作ガイドが見つかりません。', parent=parent)
        return False
    try:
        opened = webbrowser.open(guide.as_uri())
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        messagebox.showerror('操作ガイド', '操作ガイドをブラウザーで開けませんでした。', parent=parent)
        return False
    return True



def bind_timer_cleanup(widget: tk.Misc) -> None:
    """Release window-owned callbacks while its Tcl interpreter is still usable."""
    if getattr(widget, '_studio_cleanup_bound', False):
        return
    widget._studio_cleanup_bound = True
    def cleanup(event):
        if event.widget is not widget:
            return
        traces = getattr(widget, '_studio_variable_traces', ())
        widget._studio_variable_traces = []
        for variable, mode, token in traces:
            try:
                variable.trace_remove(mode, token)
            except tk.TclError:
                # The variable or this exact trace may already have been removed.
                pass
        owned = set(getattr(widget, '_tclCommands', ()) or ())
        try:
            for job in widget.tk.call('after', 'info'):
                try:
                    script = widget.tk.call('after', 'info', job)[0]
                    words = widget.tk.splitlist(script)
                    if words and words[0] in owned:
                        # Let Widget.destroy delete the Python command once.
                        widget.tk.call('after', 'cancel', job)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
    widget.bind('<Destroy>', cleanup, add='+')


def bind_variable_trace(owner: tk.Misc, variable: tk.Variable, mode: str, callback) -> str:
    """Keep a trace active only while its owning window exists.

    Tcl stores trace callbacks independently of widgets. Destroying a dialog
    otherwise leaves callbacks that retain its Python variables and controller.
    Track exact tokens so a shared variable's other observers remain active.
    """
    bind_timer_cleanup(owner)
    token = variable.trace_add(mode, callback)
    traces = getattr(owner, '_studio_variable_traces', None)
    if traces is None:
        traces = owner._studio_variable_traces = []
    traces.append((variable, mode, token))
    return token


def apply_theme(root: tk.Misc, dark=False) -> ttk.Style:
    """Use bundled Tk widgets, with the same colors on Windows and test hosts."""
    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
        tkfont.nametofont(name, root=root).configure(family=UI_FONT, size=10)
    COLORS.clear()
    COLORS.update(DARK if dark else LIGHT)
    c = COLORS
    set_icon_palette(root,c)
    root.configure(background=c['app'])
    icon = tk.PhotoImage(master=root, width=32, height=32)
    icon.put(c['blue'], to=(0, 0, 32, 32))
    for x, y in ((7, 12), (13, 6), (19, 9), (25, 14)):
        icon.put('white', to=(x, y, x+2, 32-y))
    root.iconphoto(True, icon)
    root._studio_icon = icon  # Tk keeps only the Tcl name; retain its Python owner.
    if not getattr(root, '_studio_cleanup_bound', False):
        bind_timer_cleanup(root)
        root._studio_cleanup_bound = True
    for pattern, value in (
        ('*Text.background', c['surface']),
        ('*Text.foreground', c['ink']), ('*Text.insertBackground', c['blue']),
        ('*Text.selectBackground', c['selected']), ('*Text.selectForeground', c['ink']),
        ('*Text.relief', 'flat'), ('*Text.borderWidth', 0),
        ('*Text.highlightThickness', 1), ('*Text.highlightBackground', c['line']),
        ('*Text.highlightColor', c['blue']), ('*Text.padX', 12), ('*Text.padY', 10),
        ('*Canvas.background', c['surface']), ('*Listbox.background', c['surface']),
        ('*Listbox.foreground', c['ink']), ('*Listbox.selectBackground', c['selected']),
        ('*Listbox.selectForeground', c['ink']), ('*Listbox.relief', 'flat'),
        ('*Listbox.highlightBackground', c['line']),
    ):
        root.option_add(pattern, value)
    style.configure('.', font=(UI_FONT, 10), background=c['surface'],
                    foreground=c['ink'], bordercolor=c['line'], lightcolor=c['line'],
                    darkcolor=c['line'], focuscolor=c['blue'])
    style.configure('App.TFrame', background=c['app'])
    style.configure('Card.TFrame', background=c['surface'], relief='solid', borderwidth=1)
    style.configure('TLabel', background=c['surface'])
    style.configure('Muted.TLabel', foreground=c['muted'])
    style.configure('Small.TLabel', foreground=c['muted'], font=(UI_FONT, 9))
    style.configure('Title.TLabel', background=c['app'], font=(UI_FONT, 19, 'bold'))
    style.configure('Subtitle.TLabel', background=c['app'], foreground=c['muted'],
                    font=(UI_FONT, 9))
    style.configure('Section.TLabel', font=(UI_FONT, 11, 'bold'))
    style.configure('Badge.TLabel', background=c['blue_light'], foreground=c['blue'],
                    padding=(10, 4), font=(UI_FONT, 9, 'bold'))
    style.configure('Ready.TLabel', background=c['green_light'], foreground=c['green'],
                    padding=(10, 4), font=(UI_FONT, 9, 'bold'))
    style.configure('Warning.TLabel', background=c['amber_light'], foreground=c['amber'],
                    padding=(10, 4), font=(UI_FONT, 9, 'bold'))
    style.configure('TButton', padding=(12, 7), relief='flat', borderwidth=1,
                    background=c['button'], focusthickness=2)
    style.map('TButton', background=[('disabled', c['disabled']), ('pressed', c['hover']),
                                   ('active', c['hover'])],
              foreground=[('disabled', c['disabled_ink'])])
    style.configure('Primary.TButton', background='#2563eb', foreground='white',
                    bordercolor=c['blue'], font=(UI_FONT, 10, 'bold'), padding=(19, 9))
    style.map('Primary.TButton', background=[('disabled', c['disabled']),
              ('pressed', '#1e40af'), ('active', c['blue_hover'])],
              foreground=[('disabled', '#f8fafc'), ('!disabled', 'white')],
              bordercolor=[('disabled', c['disabled']), ('!disabled', c['blue'])])
    style.configure('Stop.TButton', foreground=c['red'], background=c['red_light'])
    style.map('Stop.TButton', background=[('disabled', c['disabled']), ('active', c['red_light'])],
              foreground=[('disabled', c['disabled_ink'])])
    style.configure('Quiet.TButton', borderwidth=0, background=c['surface'],
                    foreground=c['muted'], padding=(9, 6))
    style.configure('TMenubutton', padding=(12, 7), relief='flat', borderwidth=1,
                    background=c['button'], arrowcolor=c['ink'])
    style.map('TMenubutton', background=[('active', c['hover']), ('disabled', c['disabled'])],
              foreground=[('disabled', c['disabled_ink'])])
    style.configure('Header.TMenubutton', background=c['app'])
    style.configure('TEntry', padding=(7, 6), fieldbackground=c['surface'])
    style.configure('TCombobox', padding=(7, 6), fieldbackground=c['surface'], arrowsize=13,
                    arrowcolor=c['ink'])
    style.map('TCombobox', fieldbackground=[('readonly', c['surface']),
              ('disabled', c['disabled'])], foreground=[('disabled', c['disabled_ink'])],
              selectbackground=[('readonly', c['surface'])],
              selectforeground=[('readonly', c['ink'])])
    style.configure('TSpinbox', padding=(6, 5), arrowsize=12, arrowcolor=c['ink'],
                    fieldbackground=c['surface'])
    style.configure('TCheckbutton', padding=(0, 4), background=c['surface'])
    style.configure('TRadiobutton', padding=(0, 4), background=c['surface'])
    style.configure('TLabelframe', borderwidth=1, relief='solid')
    style.configure('TLabelframe.Label', font=(UI_FONT, 10, 'bold'))
    style.configure('TNotebook', borderwidth=0, tabmargins=(0, 0, 0, 8))
    style.configure('TNotebook.Tab', padding=(15, 8), background=c['heading'])
    style.map('TNotebook.Tab', background=[('selected', c['blue_light']),
              ('active', c['hover'])], foreground=[('selected', c['blue'])])
    style.configure('Treeview', rowheight=36, borderwidth=0, relief='flat',
                    fieldbackground=c['surface'], background=c['surface'])
    style.configure('Treeview.Heading', padding=(10, 9), relief='flat',
                    background=c['heading'], font=(UI_FONT, 9, 'bold'), foreground=c['muted'])
    style.map('Treeview', background=[('selected', c['selected'])],
              foreground=[('selected', c['ink'])])
    style.map('Treeview.Heading', background=[('active', c['hover'])])
    for scrollbar in ('TScrollbar', 'Horizontal.TScrollbar', 'Vertical.TScrollbar'):
        style.configure(scrollbar, arrowsize=12, borderwidth=0, background=c['scroll'],
                        troughcolor=c['app'], gripcount=0, arrowcolor=c['ink'])
        style.map(scrollbar, background=[('active',c['muted']), ('!active',c['scroll'])],
                  arrowcolor=[('disabled',c['muted']), ('!disabled',c['ink'])])
    style.configure('TProgressbar', background=c['blue'], troughcolor=c['line'],
                    borderwidth=0, thickness=7)
    style.configure('TPanedwindow', background=c['line'], sashwidth=7)
    style.configure('Drawer.TButton', anchor='w', padding=(11, 8))
    style.configure('Category.TButton', anchor='w', padding=(10, 7), borderwidth=1)
    style.map('Category.TButton', background=[('selected',c['blue_light']),('active',c['hover'])],
              foreground=[('selected',c['blue']),('!selected',c['ink'])],
              bordercolor=[('selected',c['blue']),('!selected',c['line'])])
    style.configure('Theme.TFrame', background=c['line'])
    style.layout('Theme.TRadiobutton', style.layout('TButton'))
    style.configure('Theme.TRadiobutton', font=('Segoe UI Symbol', 14), width=3,
                    anchor='center', padding=(5, 3), background=c['app'])
    style.map('Theme.TRadiobutton', background=[('selected', c['blue_light']), ('active', c['hover'])],
              foreground=[('selected', c['blue']), ('!selected', c['muted'])])
    style.configure('Header.Switch.TCheckbutton', background=c['app'])
    style.map('Header.Switch.TCheckbutton', background=[('active',c['app'])])
    install_switch_style(root, style, c)
    root.option_add('*TCombobox*Listbox.font', (UI_FONT, 10))
    return style


class MenuAction:
    """An individually enabled command, including when its menu is closed."""
    def __init__(self, menu: tk.Menu, label: str, command, **options):
        self.menu = menu
        self.command = command
        menu.add_command(label=label, command=command, **options)
        self.index = menu.index('end')

    def configure(self, **options):
        self.menu.entryconfigure(self.index, **options)

    def cget(self, option):
        return self.menu.entrycget(self.index, option)

    def invoke(self):
        return self.menu.invoke(self.index)


def new_menu(parent) -> tk.Menu:
    return IconMenu(parent, tearoff=False, background=COLORS['surface'],
                   foreground=COLORS['ink'], activebackground=COLORS['blue_light'],
                   activeforeground=COLORS['blue'], disabledforeground=COLORS['disabled_ink'],
                   relief='solid', borderwidth=1, font=(UI_FONT, 10))


def scrollable_body(win: tk.Misc, padding=(16, 12)):
    """A scrollable form and a pinned action row for long desktop dialogs."""
    shell = ttk.Frame(win, padding=padding)
    shell.pack(fill='both', expand=True)
    footer = ttk.Frame(shell)
    footer.pack(side='bottom', fill='x', pady=(10, 0))
    area = ttk.Frame(shell)
    area.pack(fill='both', expand=True)
    canvas = tk.Canvas(area, highlightthickness=0)
    bar = ttk.Scrollbar(area, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=bar.set)
    bar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)
    body = ttk.Frame(canvas, padding=(0, 0, 9, 6))
    item = canvas.create_window((0, 0), window=body, anchor='nw')
    body.bind('<Configure>', lambda _e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda e: canvas.itemconfigure(item, width=e.width))
    return body, footer


def theme_widgets(parent):
    """Recolor existing dialogs and text without rebuilding them or losing edits."""
    c = COLORS
    for widget in [parent, *parent.winfo_children()]:
        try:
            refresh_icon(widget)
            if isinstance(widget, (tk.Text, tk.Listbox)):
                widget.configure(background=c['surface'], foreground=c['ink'],
                    selectbackground=c['selected'], selectforeground=c['ink'],
                    highlightbackground=c['line'], highlightcolor=c['blue'])
                if isinstance(widget, tk.Text):
                    widget.configure(insertbackground=c['blue'])
            elif isinstance(widget, tk.Canvas):
                if widget.winfo_width() > 50 or not widget.winfo_height():
                    widget.configure(background=c['surface'])
            elif isinstance(widget, tk.Menu):
                widget.configure(background=c['surface'],foreground=c['ink'],
                    activebackground=c['selected'],activeforeground=c['ink'],
                    disabledforeground=c['disabled_ink'])
                if isinstance(widget,IconMenu):widget.refresh_icons()
            elif isinstance(widget, ttk.Combobox):
                popup = str(widget)+'.popdown.f.l'
                if widget.tk.call('winfo', 'exists', popup):
                    widget.tk.call(popup, 'configure', '-background', c['surface'],
                        '-foreground', c['ink'], '-selectbackground', c['selected'],
                        '-selectforeground', c['ink'])
            elif isinstance(widget, ttk.Label):
                explicit = str(widget.cget('foreground'))
                if explicit:
                    warning = explicit in ('#995e22', '#725728', LIGHT['amber'], DARK['amber'])
                    widget.configure(foreground=c['amber'] if warning else c['muted'])
            elif isinstance(widget, ttk.Treeview):
                for tag, options in (
                    ('even', {'background':c['surface']}), ('odd', {'background':c['row_alt']}),
                    ('done', {'foreground':c['green']}), ('error', {'foreground':c['red']}),
                    ('running', {'foreground':c['blue']}), ('partial', {'foreground':c['amber']}),
                ):
                    widget.tag_configure(tag, **options)
        except tk.TclError:
            pass
        if widget is not parent:
            theme_widgets(widget)


class StudioView:
    """Build existing controls around one workspace, preserving app callbacks."""
    COLUMNS = ('file', 'size', 'state', 'policy', 'out', 'format', 'destination')
    COMPACT_COLUMNS = ('file', 'size', 'state', 'format', 'destination')

    def __init__(self, app, dialogs):
        self.app, self.dialogs = app, dialogs
        self.dark = app.config.theme_mode == 'dark' or (app.config.theme_mode == 'system' and system_is_dark())
        self.style = apply_theme(app.root, self.dark)
        self.idle_controls = []
        self.actions = {}
        self.menu_groups = {}
        self.drawer_buttons = []
        self.detailed_columns = tk.BooleanVar(app.root, False)
        self.queue_summary = tk.StringVar(app.root, '0 件')
        self.profile_summary = tk.StringVar(app.root)
        self.activity = tk.StringVar(app.root, '待機中')
        self.theme_var = tk.StringVar(app.root, app.config.theme_mode)
        self.auto_speaker_var = tk.BooleanVar(app.root, app.config.auto_open_speaker)
        self._queue_tags = {}
        self._sash_fraction = .58
        self._compact = False
        self._build()
        self.refresh_queue()
        theme_widgets(app.root)
        self._theme_job = app.root.after(2000, self._poll_system_theme)

    def _action(self, menu, key, label, command, *, idle_only=True, **options):
        options.setdefault('icon',ACTION_ICONS.get(key,'sliders'))
        action = MenuAction(menu, label, command, **options)
        self.actions[key] = action
        if idle_only:
            self.idle_controls.append(action)
        return action

    def _group(self,menu,key,label,icon):
        group=new_menu(menu)
        menu.add_cascade(label=label,menu=group,icon=icon)
        self.menu_groups[key]=group
        return group

    def _dialog(self, key, *args):
        # Menu state and this guard also cover a menu already open when work starts.
        if not self.app.busy:
            self.close_drawers()
            return self.dialogs[key](self.app, *args)

    def _menu_button(self, parent, label, *, header=False):
        button = AccordionButton(parent, text=label, host=self.global_drawer if header else self.queue_drawer,
                   icon={'録音・録画':'mic','ツール':'tools','設定・診断':'sliders',
                         '追加メニュー':'file-plus','選択項目':'select-all'}.get(label),
                   max_height=self._header_drawer_height if header
                              else self._queue_drawer_height,
                   on_toggle=self._drawer_toggled,
                   style='Header.TMenubutton' if header else 'TMenubutton')
        menu = new_menu(button)
        button.configure(menu=menu)
        self.drawer_buttons.append(button)
        return button, menu

    def _header_drawer_height(self):
        height = self.app.root.winfo_height()
        # Keep at least the first queued file visible on short screens. The
        # accordion already supplies a scrollbar and keyboard focus scrolling.
        return max(100, min(180, height // 4)) if self._compact else max(100, height // 3)

    def _queue_drawer_height(self):
        hint = self.hintbar.winfo_reqheight() if not self._compact else 0
        return max(90, self.workspace.winfo_height()-120-self.queue_toolbar.winfo_reqheight()-100-hint)

    def _drawer_toggled(self, owner):
        if owner.opened:
            for button in self.drawer_buttons:
                if button is not owner and button.opened:
                    button.close()
        self._sync_compact_menu()
        self._place_sash()

    def _sync_compact_menu(self):
        if self._compact and any(button.opened and button.host is self.global_drawer for button in self.drawer_buttons):
            self.mode_card.grid_remove()
        else:self.mode_card.grid()

    def _build(self):
        a = self.app
        shell = ttk.Frame(a.root, style='App.TFrame', padding=(20, 16))
        shell.pack(fill='both', expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(3, weight=1)
        self.shell = shell
        self.global_drawer = ttk.Frame(shell, style='Card.TFrame')
        self.global_drawer.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        self.global_drawer.grid_remove()

        header = ttk.Frame(shell, style='App.TFrame')
        header.grid(row=0, column=0, sticky='ew', pady=(0, 14))
        header.columnconfigure(1, weight=1)
        mark = tk.Canvas(header, width=38, height=38, highlightthickness=0,
                         background=COLORS['blue'])
        mark.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        for x, y in ((10, 15), (16, 8), (22, 12), (28, 18)):
            mark.create_line(x, y, x, 38-y, width=3, fill='white', capstyle='round')
        self.title_label = ttk.Label(header, text='文字起こし統合スタジオ', style='Title.TLabel')
        self.title_label.grid(row=0, column=1, sticky='w')
        self.subtitle = ttk.Label(header, text='音声と映像を、使えるテキストに。',
                                  style='Subtitle.TLabel')
        self.subtitle.grid(row=1, column=1, sticky='w')
        nav = ttk.Frame(header, style='App.TFrame')
        nav.grid(row=0, column=2, rowspan=2, sticky='e')

        record_button, record = self._menu_button(nav, '録音・録画', header=True)
        record_button.pack(side='left', padx=(0, 5))
        capture=self._group(record,'capture_start','録音・録画を開始','mic')
        utilities=self._group(record,'capture_other','取り込み・環境','import')
        a.live_btn = self._action(capture, 'live', 'リアルタイム録音・文字起こし…',
                                  lambda: a.open_capture('live',force_settings=True))
        a.screen_btn = self._action(capture, 'gamebar', 'Game Bar連動録画…',
                                    lambda: a.open_capture('gamebar',force_settings=True))
        self._action(utilities, 'capture_setup', '録音・録画のセットアップ…',
                     lambda: a.open_capture_setup())
        self._action(utilities,'cancel_live_asr','録音の文字起こしを中断',a.cancel_live_transcription,idle_only=False)
        a.standard_btn = self._action(utilities, 'import', '標準録画を取り込み・文字起こし…',
                                      lambda: self._dialog('standard'))

        tools_button, tools = self._menu_button(nav, 'ツール', header=True)
        tools_button.pack(side='left', padx=(0, 5))
        merge=self._group(tools,'tools_merge','結合・音声の抽出','merge')
        editing=self._group(tools,'tools_edit','変換・話者名','edit')
        a.text_merge_btn = self._action(merge, 'text_merge', 'TXT結合…',
                                        lambda: self._dialog('concat', 'text'))
        a.audio_merge_btn = self._action(merge, 'audio_merge', '音声結合・動画から音声を抽出…',
                                         lambda: self._dialog('concat', 'audio'))
        a.csv_merge_btn = self._action(merge, 'csv_merge', 'CSV結合…',
                                       lambda: self._dialog('concat', 'csv'))
        a.convert_btn = self._action(editing, 'convert', 'TXT ⇔ CSV変換…',
                                     lambda: self._dialog('convert'))
        a.speaker_edit_btn = self._action(editing, 'speaker', '話者名を設定…',
                                          lambda: self._dialog('speaker'))

        settings_button, settings = self._menu_button(nav, '設定・診断', header=True)
        settings_button.pack(side='left')
        environment=self._group(settings,'settings_env','環境の準備・確認','download')
        operation=self._group(settings,'settings_operation','操作・設定','sliders')
        logs=self._group(settings,'settings_logs','ログ','logs')
        support=self._group(settings,'settings_support','サポート','help')
        a.bulk_btn = self._action(environment, 'bulk', '一括セットアップ…',
                                  lambda: self._dialog('bulk'))
        a.setup_btn = self._action(environment, 'setup', '環境セットアップ・割り当て…',
                                   lambda: self._dialog('setup'))
        a.cuda_btn = self._action(environment, 'cuda', 'CUDA / GPU診断…',
                                  lambda: self._dialog('cuda'))
        self._action(operation, 'limits', 'CPU・GPU負荷制限…',
                     a.edit_runtime_limits, idle_only=False)
        self._action(operation,'shortcuts','録音・録画のショートカット…',a.edit_shortcuts,idle_only=False)
        self._action(operation,'background','自動起動・常駐の設定…',a.edit_background,idle_only=False,icon='clock')
        self._action(operation,'hints','認識用の単語辞書…',lambda: self._dialog('hints'),icon='text')
        self._action(operation,'settings_export','設定を書き出す…',a.export_preferences,idle_only=False)
        self._action(operation,'settings_import','設定を読み込む…',a.import_preferences)
        self._action(support,'app_updates','アプリの更新・確認間隔…',a.show_app_updates,
                     idle_only=False,icon='download')
        self._action(support, 'dnd', 'D&D 診断・修復…', a.repair_dnd, idle_only=False)
        self._action(logs, 'logs', '診断ログフォルダーを開く',
                     a.open_diagnostic_folder, idle_only=False)
        self._action(logs, 'detached_log', 'ログを別ウィンドウで表示…',
                     a.open_detached_log, idle_only=False)
        self._action(logs, 'export_logs', 'ログをZIP保存…',
                     a.export_diagnostics, idle_only=False)
        self._action(support, 'help', '使い方・ショートカット…', self.show_help,
                     idle_only=False, accelerator='F1')

        quick = ttk.Frame(header, style='App.TFrame')
        quick.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(12, 0))
        self.live_shortcut = IconButton(quick, text='● リアルタイム録音',
            command=lambda: a.open_capture('live'))
        self.live_shortcut.pack(side='left', padx=(0, 8))
        self.gamebar_shortcut = IconButton(quick, text='▣ Game Bar連動',
            command=lambda: a.open_capture('gamebar'))
        self.gamebar_shortcut.pack(side='left')
        self.stop_all_button=IconButton(quick,text='すべて停止',icon='stop',command=a.stop_all,
            style='Stop.TButton',state='disabled')
        self.stop_all_button.pack(side='left',padx=(12,0))
        self.guide_button = IconButton(quick, text='画面付き操作ガイド', icon='help',
            command=lambda: open_illustrated_guide(a.root))
        self.guide_button.pack(side='left', padx=(12, 0))
        self.idle_controls.extend((self.live_shortcut, self.gamebar_shortcut))
        for button,target in ((self.live_shortcut,'live'),(self.gamebar_shortcut,'gamebar')):
            button.bind('<Shift-ButtonPress-1>',lambda _e:'break')
            button.bind('<Shift-ButtonRelease-1>',lambda _e,t=target: (
                a.open_capture(t,force_settings=True),'break')[1])
        self.theme_toggle = IconThemeToggle(quick, variable=self.theme_var, command=self._theme_changed)
        self.theme_toggle.pack(side='right')

        mode = ttk.Frame(shell, style='Card.TFrame', padding=(15, 11))
        self.mode_card=mode
        mode.grid(row=2, column=0, sticky='ew', pady=(0, 12))
        mode.columnconfigure(1, weight=1)
        ttk.Label(mode, text='認識方式', style='Section.TLabel').grid(
            row=0, column=0, sticky='w', padx=(0, 16))
        from core import PROFILES
        a.profile_var = tk.StringVar(a.root, next(p.name for p in PROFILES if p.id == a.config.profile))
        self.profile_box = ttk.Combobox(mode, textvariable=a.profile_var,
            values=[p.name for p in PROFILES], state='readonly', width=35)
        self.profile_box.grid(row=0, column=1, sticky='ew')
        self.profile_box.bind('<<ComboboxSelected>>', a._on_profile)
        self.env_badge = ttk.Label(mode, text='環境を確認中', style='Badge.TLabel')
        self.env_badge.grid(row=0, column=2, padx=(12, 8))
        a.settings_btn = IconButton(mode, text='詳細設定…',
                                    command=lambda: self._dialog('settings'))
        a.settings_btn.grid(row=0, column=3)
        self.hints_button = IconButton(mode, text='単語辞書…', icon='text',
                                       command=lambda: self._dialog('hints'))
        self.hints_button.grid(row=0, column=4, padx=(8, 0))
        self.idle_controls.append(self.hints_button)
        a.profile_note = ttk.Label(mode, style='Small.TLabel', wraplength=680)
        a.profile_note.grid(row=1, column=0, columnspan=5, sticky='w', pady=(7, 0))

        self.workspace = ttk.Panedwindow(shell, orient='vertical')
        self.workspace.grid(row=3, column=0, sticky='nsew')
        queue_card = ttk.Frame(self.workspace, style='Card.TFrame', padding=(1, 1))
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(2, weight=1)
        self.workspace.add(queue_card, weight=3)
        self._build_queue(queue_card)
        log_card = ttk.Frame(self.workspace, style='Card.TFrame', padding=(12, 9))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        self.workspace.add(log_card, weight=2)
        self._build_logs(log_card)
        self._build_footer(shell)
        self.workspace.bind('<Map>', self._initial_sash, add='+')
        self.workspace.bind('<Configure>', self._workspace_resized, add='+')
        self.workspace.bind('<ButtonRelease-1>', self._remember_sash, add='+')

        a.root.bind('<Control-o>', lambda _e: a.add_files())
        a.root.bind('<Control-Return>', lambda _e: a.start_batch())
        a.root.bind('<F1>', lambda _e: self.show_help())
        a.root.bind('<Escape>', lambda _e: self.close_drawers(), add='+')
        a.tree.bind('<Control-a>', self.select_all)
        a.tree.bind('<Delete>', lambda _e: a.remove_selected())
        a.root.bind('<Configure>', self._resize_text, add='+')

    def _build_queue(self, parent):
        a = self.app
        self.queue_drawer = ttk.Frame(parent, style='Card.TFrame')
        self.queue_drawer.grid(row=1,column=0,sticky='ew')
        self.queue_drawer.grid_remove()
        toolbar = ttk.Frame(parent, padding=(13, 9))
        self.queue_toolbar = toolbar
        toolbar.grid(row=0, column=0, sticky='ew')
        ttk.Label(toolbar, text='ファイル', style='Section.TLabel').pack(side='left')
        ttk.Label(toolbar, textvariable=self.queue_summary, style='Badge.TLabel').pack(
            side='left', padx=(10, 8))
        self.add_button = IconButton(toolbar, text='＋ ファイルを追加…', command=a.add_files)
        self.add_button.pack(side='right')
        self.idle_controls.append(self.add_button)
        more_button, more = self._menu_button(toolbar, '追加メニュー')
        more_button.pack(side='right', padx=(6, 7))
        self._action(more, 'add_folder', 'フォルダー内を追加…', a.add_folder)
        self._action(more, 'watch', 'フォルダー監視…', lambda: self._dialog('watch'))

        self.selection_button, selection = self._menu_button(toolbar, '選択項目')
        self.selection_button.pack(side='right', padx=(6, 0))
        self.selection_menu = selection
        result=self._group(selection,'selection_results','完了した結果','text')
        processing=self._group(selection,'selection_processing','処理・保存','sliders')
        listing=self._group(selection,'selection_list','一覧の操作','select-all')
        display=self._group(selection,'selection_display','表示','columns')
        a.edit_output_btn = self._action(processing, 'file_output', '出力形式・保存先…', a.edit_file_outputs)
        a.edit_policy_btn = self._action(processing, 'file_policy', 'スキップ・上書き・別名保存…',
                                        a.edit_queue_policies, idle_only=False)
        self._action(listing, 'remove', '選択項目を削除', a.remove_selected, accelerator='Delete')
        self._action(listing, 'select_all', 'すべて選択', self.select_all,
                     idle_only=False, accelerator='Ctrl+A')
        self._action(listing, 'clear', '一覧をクリア', a.clear_files)
        self._action(result, 'result', '完了した結果・保存先を表示…', a.show_selected_result,
                     idle_only=False)
        self._action(result, 'open_source', '元ファイルの場所を開く', a.open_selected_source,
                     idle_only=False)
        self._action(result, 'open_result', '結果の保存先を開く', a.open_selected_output,
                     idle_only=False)
        self._action(result, 'result_speaker', 'この結果の話者名を設定…', a.edit_selected_speakers)
        display.add_checkbutton(label='元の場所・既存出力の扱いの列を表示',icon='columns',
            variable=self.detailed_columns, command=self.toggle_columns)

        treeframe = ttk.Frame(parent)
        treeframe.grid(row=2, column=0, sticky='nsew')
        treeframe.columnconfigure(0, weight=1)
        treeframe.rowconfigure(0, weight=1)
        a.tree = ttk.Treeview(treeframe, columns=self.COLUMNS,
            displaycolumns=self.COMPACT_COLUMNS, show='headings', selectmode='extended', height=4)
        for key, label, width, minimum, stretch in (
            ('file', 'ファイル名', 270, 180, True), ('size', 'サイズ', 84, 74, False),
            ('state', '処理状態', 110, 100, False), ('policy', '既存出力の扱い', 150, 130, False),
            ('out', '元ファイルの場所', 240, 150, True), ('format', '形式', 80, 70, False),
            ('destination', '文字起こし保存先', 350, 180, True),
        ):
            a.tree.heading(key, text=label, anchor='w')
            a.tree.column(key, width=width, minwidth=minimum, stretch=stretch,
                          anchor='center' if key in ('size', 'format') else 'w')
        a.tree.grid(row=0, column=0, sticky='nsew')
        vbar = ttk.Scrollbar(treeframe, orient='vertical', command=a.tree.yview)
        vbar.grid(row=0, column=1, sticky='ns')
        hbar = ttk.Scrollbar(treeframe, orient='horizontal', command=a.tree.xview)
        hbar.grid(row=1, column=0, sticky='ew')
        a.tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        a.tree.bind('<Double-1>', a._on_tree_double_click)
        a.tree.bind('<<TreeviewSelect>>', lambda _e: self.refresh_queue())
        a.tree.bind('<Button-3>', self.context_menu)
        for tag, options in (
            ('even', {'background': COLORS['surface']}), ('odd', {'background': COLORS['row_alt']}),
            ('done', {'foreground': COLORS['green']}), ('error', {'foreground': COLORS['red']}),
            ('running', {'foreground': COLORS['blue']}), ('partial', {'foreground': COLORS['amber']}),
        ):
            a.tree.tag_configure(tag, **options)
        self.empty_hint = ttk.Label(a.tree,
            text='音声・動画をここにドロップ\nまたは「ファイルを追加」から選択',
            justify='center', style='Muted.TLabel', padding=(12, 10))
        self.empty_hint.place(relx=.5, rely=.54, anchor='center')
        a.empty_drop_hint = self.empty_hint
        hintbar = ttk.Frame(parent, padding=(12, 4))
        self.hintbar = hintbar
        hintbar.grid(row=3, column=0, sticky='ew')
        a.drop_hint = ttk.Label(hintbar, style='Small.TLabel', wraplength=680)
        a.drop_hint.pack(side='left', fill='x', expand=True)
        a.dnd_repair_btn = IconButton(hintbar, text='D&D 診断・修復…',
                                     command=a.repair_dnd, style='Quiet.TButton')
        a.dnd_repair_btn.pack(side='right', padx=(8, 0))
        a._enable_drag_drop(treeframe)
        self._update_empty_hint()

    def _build_logs(self, parent):
        a = self.app
        heading = ttk.Frame(parent)
        self.log_heading = heading
        heading.grid(row=0, column=0, sticky='ew', pady=(0, 7))
        ttk.Label(heading, text='認識プレビューとログ', style='Section.TLabel').pack(side='left')
        IconButton(heading, text='ログを別ウィンドウで表示…', command=a.open_detached_log,
                   style='Quiet.TButton').pack(side='right')
        a.log_panels = ttk.Panedwindow(parent, orient='horizontal')
        a.log_panels.grid(row=1, column=0, sticky='nsew')
        for is_preview, caption, width in (
            (True, '認識テキスト · 暫定 / 話者未確定', 38),
            (False, '詳細ログ · 処理経過 / エラー / 進捗', 52),
        ):
            area = ttk.Frame(a.log_panels, padding=(0, 0, 5 if is_preview else 0, 0))
            area.columnconfigure(0, weight=1)
            area.rowconfigure(1, weight=1)
            titlebar = ttk.Frame(area)
            titlebar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 5))
            if is_preview:
                a.copy_preview_btn = IconButton(titlebar, text='コピー',
                    command=a.copy_recognition_preview, style='Quiet.TButton')
                a.copy_preview_btn.pack(side='right', padx=(7, 0))
            ttk.Label(titlebar, text=caption, style='Small.TLabel').pack(side='left')
            box = tk.Text(area, height=4, width=width, wrap='word', state='disabled',
                          font=(UI_FONT, 10))
            box.grid(row=1, column=0, sticky='nsew')
            bar = ttk.Scrollbar(area, orient='vertical', command=box.yview)
            bar.grid(row=1, column=1, sticky='ns')
            box.configure(yscrollcommand=bar.set)
            a.log_panels.add(area, weight=2 if is_preview else 3)
            if is_preview:
                a.recognized_box = box
            else:
                a.logbox = box

    def _build_footer(self, shell):
        a = self.app
        footer = ttk.Frame(shell, style='Card.TFrame', padding=(14, 10))
        self.footer = footer
        footer.grid(row=4, column=0, sticky='ew', pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        controls = ttk.Frame(footer)
        controls.grid(row=0, column=0, sticky='ew')
        a.start_btn = IconButton(controls, text='▶ 文字起こしを開始',
                                 command=a.start_batch, style='Primary.TButton')
        a.start_btn.pack(side='left', padx=(0, 8))
        a.stop_btn = IconButton(controls, text='■ 停止', command=a.stop,
                                state='disabled', style='Stop.TButton')
        a.stop_btn.pack(side='left')
        IconButton(controls, text='出力先を開く', command=a.open_output,
                   style='Quiet.TButton').pack(side='left', padx=(12, 0))
        self.activity_badge = ttk.Label(controls, textvariable=self.activity, style='Badge.TLabel')
        self.activity_badge.pack(side='right')
        ToggleSwitch(controls, text='完了後に話者名設定', variable=self.auto_speaker_var,
                     command=self._auto_speaker_changed).pack(side='right', padx=(10, 12))
        a.status = tk.StringVar(a.root, 'ファイルを追加して文字起こしを開始してください。')
        self.status_label = ttk.Label(footer, textvariable=a.status, style='Small.TLabel', wraplength=1000)
        self.status_label.grid(row=1, column=0, sticky='w', pady=(7, 0))
        a.progress_box = ttk.Frame(footer)
        a.progress_box.grid(row=2, column=0, sticky='ew', pady=(4, 0))
        footer.rowconfigure(2, minsize=6)
        a.progress_label = tk.StringVar(a.root, '')
        a.progress_text = ttk.Label(a.progress_box, textvariable=a.progress_label,
                                    style='Small.TLabel', wraplength=600)
        a.spinner = ttk.Label(a.progress_box, text='', foreground=COLORS['blue'],
                              font=('Segoe UI Symbol', 13))
        a.progress = ttk.Progressbar(a.progress_box, mode='determinate', length=180, maximum=100)
        a._animation_job = a.root.after(130, a._animate_busy)

    def _initial_sash(self, event):
        if event.widget is self.workspace and not getattr(self, '_sash_set', False):
            self._sash_set = True
            self.app.root.after_idle(self._place_sash)

    def close_drawers(self):
        for button in self.drawer_buttons:
            if button.opened:
                button.close()

    def _auto_speaker_changed(self):
        from core import save_config
        self.app.config.auto_open_speaker = self.auto_speaker_var.get()
        save_config(self.app.config)
        if not self.auto_speaker_var.get():
            self.app.pending_speaker_edits.clear()

    def _theme_changed(self):
        from core import save_config
        mode = self.theme_var.get()
        self.app.config.theme_mode = mode
        save_config(self.app.config)
        dark = system_is_dark() if mode == 'system' else mode == 'dark'
        self._apply_appearance(dark)

    def _apply_appearance(self, dark):
        self.dark = bool(dark)
        self.style = apply_theme(self.app.root, self.dark)
        theme_widgets(self.app.root)

    def _poll_system_theme(self):
        if self.app.closing:
            return
        if self.app.config.theme_mode == 'system':
            dark = system_is_dark()
            if dark != self.dark:
                self._apply_appearance(dark)
        self._theme_job = self.app.root.after(2000, self._poll_system_theme)

    def _place_sash(self):
        if self.workspace.winfo_exists():
            height = self.workspace.winfo_height()
            if height > 100:
                minimum_top = min(148,max(80,height-64)) if self._compact else min(120,height//2)
                minimum_bottom = min(64,height//2) if self._compact else min(120,height//2)
                owner = getattr(self.queue_drawer, '_drawer_owner', None)
                if owner is not None and owner.opened:
                    hint = self.hintbar.winfo_reqheight() if not self._compact else 0
                    minimum_top = min(height-minimum_bottom, self.queue_toolbar.winfo_reqheight()+
                        getattr(self.queue_drawer, '_expanded_height', 90)+hint+100)
                position = max(minimum_top, min(height-minimum_bottom, round(height*self._sash_fraction)))
                self.workspace.sashpos(0, position)

    def _workspace_resized(self, event):
        if event.widget is self.workspace:
            self._place_sash()

    def _remember_sash(self, _event):
        height = self.workspace.winfo_height()
        if height > 0:
            self._sash_fraction = max(.1, min(.9, self.workspace.sashpos(0)/height))

    def _resize_text(self, event):
        if event.widget is self.app.root:
            compact = event.height < 760
            if compact != self._compact:
                self._compact = compact
                self._sync_compact_menu()
                if compact:
                    self.subtitle.grid_remove()
                    self.app.profile_note.grid_remove()
                    self.hintbar.grid_remove()
                    self.log_heading.grid_remove()
                    self.queue_toolbar.configure(padding=(10, 4))
                else:
                    self.subtitle.grid()
                    self.app.profile_note.grid()
                    self.hintbar.grid()
                    self.log_heading.grid()
                    self.queue_toolbar.configure(padding=(13, 9))
                self._update_empty_hint()
            self.title_label.configure(font=(UI_FONT, 15 if event.width < 1000 else 19, 'bold'))
            width = max(300, event.width-100)
            self.app.profile_note.configure(wraplength=width)
            self.app.drop_hint.configure(wraplength=max(260, width-145))
            self.status_label.configure(wraplength=width)
            self.app.progress_text.configure(wraplength=max(260, width-220))

    def refresh_profile(self, profile, python: Path):
        ready = python.is_file()
        self.env_badge.configure(text='● セットアップ済み' if ready else '未セットアップ',
                                 style='Ready.TLabel' if ready else 'Warning.TLabel')
        description = profile.description.rstrip('。 ') + '。'
        self.app.profile_note.configure(text=description)

    def sync_busy(self, busy: bool):
        self.close_drawers()
        self.stop_all_button.configure(state='normal' if busy and not getattr(self.app,'all_stop_requested',False) else 'disabled')
        for control in self.idle_controls:
            control.configure(state='disabled' if busy else 'normal')
        self.profile_box.configure(state='disabled' if busy else 'readonly')
        self.activity.set('処理中' if busy else '待機中')
        self.footer.rowconfigure(2, minsize=23 if busy else 6)
        self.refresh_queue()

    def _update_empty_hint(self):
        if self._compact:
            text = 'ファイルを追加してください'
        elif self.app.drop_hint.cget('text').startswith('D&D有効'):
            text = '音声・動画をここにドロップ\nまたは「ファイルを追加」から選択'
        else:
            text = 'ファイルを追加して始めましょう\n複数の音声・動画をまとめて処理できます'
        self.empty_hint.configure(text=text, padding=(8, 2) if self._compact else (12, 10))

    def refresh_queue(self):
        a = self.app
        if not hasattr(a, 'tree'):
            return
        keys = a.tree.get_children()
        selected = len(a.tree.selection())
        self.queue_summary.set(f'{len(keys)} 件' + (f'  /  {selected} 件選択' if selected else ''))
        if keys:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=.5, rely=.5, y=16, anchor='center')
        # Cache tags so selection changes do not rewrite every row or fire more events.
        current_keys = set(keys)
        self._queue_tags = {k: v for k, v in self._queue_tags.items() if k in current_keys}
        for index, key in enumerate(keys):
            row = a.tree.item(key, 'values')
            state = str(row[2]) if len(row) > 2 else ''
            tone = ('partial' if '一部完了' in state else 'error' if any(x in state for x in ('失敗', 'エラー'))
                    else 'done' if state == '完了' else 'running' if '中' in state and state != '待機中' else '')
            tags = ('even' if index % 2 == 0 else 'odd',) + ((tone,) if tone else ())
            if self._queue_tags.get(key) != tags:
                a.tree.item(key, tags=tags)
                self._queue_tags[key] = tags

    def toggle_columns(self):
        self.app.tree.configure(displaycolumns=self.COLUMNS if self.detailed_columns.get()
                                else self.COMPACT_COLUMNS)

    def select_all(self, _event=None):
        self.app.tree.selection_set(self.app.tree.get_children())
        self.refresh_queue()
        return 'break'

    def context_menu(self, event):
        tree = self.app.tree
        key = tree.identify_row(event.y)
        if key and key not in tree.selection():
            tree.selection_set(key)
        self.selection_button.toggle()

    def show_help(self):
        from unified_gui import modal
        win = modal(self.app.root, '使い方・ショートカット', 740)
        outer = ttk.Frame(win, padding=22)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='スタジオの使い方', font=(UI_FONT, 17, 'bold')).pack(anchor='w', pady=(0, 12))
        text = tk.Text(outer, wrap='word', width=65, height=16, font=(UI_FONT, 10))
        text.pack(fill='both', expand=True)
        bar = ttk.Scrollbar(text, command=text.yview)
        bar.pack(side='right', fill='y')
        text.configure(yscrollcommand=bar.set)
        text.insert('1.0',
            '1  認識方式を選ぶ\n'
            '初めて使う方式は「設定・診断 → 一括セットアップ」から導入します。'
            'モデル・話者分離・既定の保存先は「詳細設定」で変更できます。\n\n'
            '2  ファイルを追加する\n'
            '音声・動画を一覧にドロップするか「ファイルを追加」を押します。'
            'フォルダーの追加・監視は「追加メニュー」にあります。\n\n'
            '3  文字起こしを開始する\n'
            '実行状況は一覧とログで確認できます。「停止」は録音・録画を保存して認識を完了します。TOPの「すべて停止」は進行中・待機中の文字起こし、結合、変換、監視、セットアップを中断します。\n\n'
            'ファイルごとの設定\n'
            '処理前の行をダブルクリック、または「選択項目」から出力形式・保存先を変更できます。'
            '保存先の「入力と同じ場所」は、ファイルごとにその元フォルダーを使います。'
            '同じメニューを右クリックでも開けます。既存出力の扱いは、実行中でも次のキューに変更できます。'
            '元ファイルの場所と既存出力の扱いの列も、このメニューから表示できます。\n\n'
            '完了した結果\n'
            '完了した行のファイル名をダブルクリックすると、保存先・元ファイルの場所を開く操作と話者名設定が表示されます。'
            '保存先の列はダブルクリックでそのフォルダーを開きます。'
            '「完了後に話者名設定」をONにすると、一括処理の終了後に結果を順番に開きます。\n\n'
            '録音・録画 / ツール\n'
            'メニューは上段の中カテゴリで切り替え、その下から操作を選べます。'
            'TOPのリアルタイム録音・Game Bar連動は初回に設定画面を開きます。'
            '「次回以降は設定画面をスキップする」をONにして保存すると、TOPから前回設定で開始します。'
            'Game BarはスキップがONでも、録画対象の画像付き一覧を毎回表示します。'
            'メニューまたはTOPのShift＋クリックでは毎回設定を開けます。'
            '録画対象は画像付き一覧から選択できます。'
            '「録音・録画」からセットアップの再実行や標準録画の取り込みもできます。'
            'TXT・音声・CSVの結合、TXT ⇔ CSV変換、話者名編集は「ツール」へ。\n\n'
            '設定の移行・デフォルトの保存先\n'
            '「設定・診断 → 操作・設定」から、設定をまとめて書き出し・読み込みできます。'
            'ユーザーやアプリのフォルダーが変わっても、その中の保存先を移行先に合わせます。'
            '各保存先の「デフォルト」は、共通の保存先（初期値はドキュメント内のUnifiedTranscriber）を使います。'
            'Game Barの録画対象は保存せず、開始時に毎回選択します。設定だけなら未選択のまま保存できます。\n\n'
            '録音と終了後の認識\n'
            '「録音のみ」でも、終了後だけ文字起こしするオプションを選べます。'
            '停止は1回で録音を確定し、必要な最終解析のあと自動で終了します。'
            '途中で認識をやめる場合は「認識を中断」を使用してください。'
            'L/R分離はLにPC音（モノラル）、Rにマイクを保存します。\n\n'
            'ライブ文字起こし\n'
            '通常のライブ録音とGame Bar連携で利用できます。Game Barでは設定画面の「録画中にライブ文字起こし」をONにします。'
            '左のプレビューへ録音中の結果を続けて表示し、停止後の全文解析では一度クリアして先頭から表示します。'
            'プレビューの「コピー」で、表示した認識テキスト全体をコピーできます。\n\n'
            '表示テーマ\n'
            'TOP右側の3つのアイコンを、左からライト／システムに合わせる／ダークとして選択できます。'
            'マウスを重ねると名前を表示します。\n\n'
            'ショートカット\n'
            '録音開始・Game Bar開始・停止のキーは「設定・診断 → 録音・録画のショートカット」で設定できます。'
            '「キー入力」で実際の組み合わせを押して登録します。初期状態は未設定です。\n'
            'Ctrl+O  ファイルを追加\nCtrl+Enter  文字起こしを開始\n'
            'Ctrl+A  一覧のすべての行を選択（一覧にフォーカスがあるとき）\n'
            'Delete  選択項目を一覧から削除（元ファイルは保持）\nF1  この使い方を表示\n\n'
            '一覧とプレビューの境界、認識テキストと詳細ログの境界はドラッグで調整できます。'
            '長いファイルパスは一覧下の横スクロールで確認できます。')
        text.configure(state='disabled')
        footer = ttk.Frame(outer)
        footer.pack(fill='x', pady=(14, 0))
        IconButton(footer, text='画面付きの操作ガイド', icon='help',
                   command=lambda: open_illustrated_guide(self.app.root)).pack(side='left')
        IconButton(footer, text='閉じる', command=win.destroy).pack(side='right')
