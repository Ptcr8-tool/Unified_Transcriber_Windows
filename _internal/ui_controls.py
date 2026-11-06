"""Small standard-Tk controls: switches, navigation panels and OS appearance."""
from __future__ import annotations

import math
import sys
import tkinter as tk
from tkinter import ttk
from ui_icons import IconButton, IconMenubutton, attach_icon


def system_is_dark() -> bool:
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
        except (OSError, ImportError):
            pass
    return False


def install_switch_style(root, style, colors):
    scale = max(1, float(root.tk.call('tk', 'scaling')) / 1.333333)
    width, height = round(40*scale), round(24*scale)
    images = getattr(root, '_studio_switch_images', None)
    if images is None:
        images = [tk.PhotoImage(master=root, width=width, height=height) for _ in range(4)]
        root._studio_switch_images = images
    for image, selected, disabled in zip(images, (False, True, False, True), (False, False, True, True)):
        image.configure(width=width, height=height)
        image.blank()
        track = colors['disabled'] if disabled else (colors['green'] if selected else colors['switch_off'])
        radius = height/2
        knob_radius = radius-3*scale
        knob_x = width-radius if selected else radius
        for y in range(height):
            dy = y+.5-radius
            inset = max(0, round(radius-math.sqrt(max(0, radius*radius-dy*dy))))
            image.put(track, to=(inset, y, width-inset, y+1))
            if abs(dy) < knob_radius:
                half = math.sqrt(knob_radius*knob_radius-dy*dy)
                image.put('#e7ebf0' if disabled else '#ffffff',
                          to=(round(knob_x-half), y, round(knob_x+half), y+1))
    if 'StudioSwitch.indicator' not in style.element_names():
        style.element_create('StudioSwitch.indicator', 'image', images[0],
            ('disabled', 'selected', images[3]), ('disabled', images[2]), ('selected', images[1]))
    style.layout('Switch.TCheckbutton', [('Checkbutton.padding', {'sticky': 'nsew', 'children': [
        ('StudioSwitch.indicator', {'side': 'left', 'sticky': ''}),
        ('Checkbutton.focus', {'side': 'left', 'sticky': 'w', 'children': [
            ('Checkbutton.label', {'sticky': 'w'})]})]})])
    style.configure('Switch.TCheckbutton', background=colors['surface'], padding=(0, 5),
                    foreground=colors['ink'])
    style.map('Switch.TCheckbutton', background=[('active', colors['surface'])],
              foreground=[('disabled', colors['muted'])])


class ToggleSwitch(ttk.Checkbutton):
    """Keep ttk's variables, keyboard support, disabled state and invoke semantics."""
    def __init__(self, master=None, **kwargs):
        kwargs.setdefault('style', 'Switch.TCheckbutton')
        super().__init__(master, **kwargs)


class IconThemeToggle(ttk.Frame):
    """Three mutually exclusive appearance choices, with labels on hover/focus."""
    MODES = (('light', 'sun', 'ライト'), ('system', 'monitor', 'システムに合わせる'), ('dark', 'moon', 'ダーク'))

    def __init__(self, master, *, variable, command):
        super().__init__(master, style='Theme.TFrame', padding=3)
        self.variable, self.command = variable, command
        self.buttons = {}
        self._tip = self._tip_job = None
        for index, (mode, icon, label) in enumerate(self.MODES):
            button = ttk.Radiobutton(self, text=label, variable=variable, value=mode,
                                     command=command, style='Theme.TRadiobutton', takefocus=True)
            attach_icon(button,icon,icon_only=True)
            button.pack(side='left', padx=1)
            button.bind('<Enter>', lambda _e, b=button, s=label: self._schedule_tip(b, s))
            button.bind('<FocusIn>', lambda _e, b=button, s=label: self._schedule_tip(b, s))
            button.bind('<Leave>', self._hide_tip)
            button.bind('<FocusOut>', self._hide_tip)
            button.bind('<Left>', lambda _e, i=index: self._choose(i-1))
            button.bind('<Right>', lambda _e, i=index: self._choose(i+1))
            self.buttons[mode] = button
        self.bind('<Destroy>', lambda e: self._hide_tip() if e.widget is self else None)

    def _choose(self, index):
        mode = self.MODES[index % len(self.MODES)][0]
        self.variable.set(mode)
        self.buttons[mode].focus_set()
        self.command()
        return 'break'

    def _schedule_tip(self, button, label):
        self._hide_tip()
        def show():
            self._tip_job = None
            tip = self._tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.geometry(f'+{button.winfo_rootx()}+{button.winfo_rooty()+button.winfo_height()+5}')
            ttk.Label(tip, text=label, padding=(9, 5)).pack()
        self._tip_job = self.after(450, show)

    def _hide_tip(self, _event=None):
        if self._tip_job is not None:
            self.after_cancel(self._tip_job)
            self._tip_job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class NavigationMenuButton(IconMenubutton):
    """A nonmodal, anchored menu with a persistent category rail and action list.

    The backing Tk menus still own commands, variables and enabled states. The
    panel is presentation only, so opening it never moves the working area.
    """
    def __init__(self, master, *, flat_groups=False, **kwargs):
        super().__init__(master, takefocus=True, **kwargs)
        self.flat_groups = flat_groups
        self.opened = False
        self.active_group = None
        self.group_buttons, self.action_buttons = {}, {}
        self.panel = None
        self._bindings = []
        self._focus_job = None
        for sequence in ('<Button-1>', '<space>', '<Return>', '<Down>'):
            self.bind(sequence, self.toggle)
        self.bind('<Destroy>', self._destroyed)

    def toggle(self, _event=None):
        if self.instate(['disabled']):
            return 'break'
        if self.opened:
            self.close(restore_focus=True)
            return 'break'
        root = self.winfo_toplevel()
        previous = getattr(root, '_navigation_owner', None)
        if previous is not None:
            previous.close()
        root._navigation_owner = self
        menu = self.nametowidget(str(self.cget('menu')))
        self.opened = True
        self.state(['selected'])
        panel = self.panel = tk.Toplevel(root)
        panel.withdraw()
        panel.overrideredirect(True)
        panel.transient(root)
        panel.title(f'{self.cget("text")} メニュー')
        panel.bind('<Escape>', lambda _e: self._dismiss())
        self._focus_entered = False
        panel.bind('<FocusIn>', self._focus_in)
        panel.bind('<FocusOut>', self._focus_out)
        shell = ttk.Frame(panel, style='Card.TFrame', padding=1)
        shell.pack(fill='both', expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)
        heading = ttk.Frame(shell, padding=(16, 8))
        heading.grid(row=0, column=0, sticky='ew')
        ttk.Label(heading, text=self.cget('text'), style='Section.TLabel').pack(side='left')
        self.close_button = IconButton(heading, text='閉じる', icon='close',
            style='Quiet.TButton', command=self._dismiss)
        self.close_button.bind('<Return>', lambda _e: self._dismiss())
        self.close_button.pack(side='right')
        ttk.Label(heading, text='Esc', style='Small.TLabel').pack(side='right', padx=8)
        ttk.Separator(shell).grid(row=1, column=0, sticky='ew')
        content = ttk.Frame(shell)
        content.grid(row=2, column=0, sticky='nsew')
        content.columnconfigure(2, weight=1)
        content.rowconfigure(0, weight=1)
        self._group_menus = {}
        self.group_buttons, self.action_buttons = {}, {}
        end = menu.index('end')
        groups = [i for i in range(end+1 if end is not None else 0) if menu.type(i) == 'cascade']
        if groups and not self.flat_groups:
            rail = ttk.Frame(content, padding=(12, 12), style='Navigation.TFrame')
            rail.grid(row=0, column=0, sticky='ns')
            ttk.Label(rail, text='カテゴリ', style='Navigation.Caption.TLabel').pack(anchor='w', padx=8, pady=(0, 8))
            for item in groups:
                label = menu.entrycget(item, 'label')
                self._group_menus[label] = self.nametowidget(str(menu.entrycget(item, 'menu')))
                button = IconButton(rail, text=label, icon=menu.icon_name(item),
                    style='Category.TButton', command=lambda key=label: self.show_group(key))
                button.pack(fill='x', pady=2)
                self._bind_category_keys(button, label)
                self.group_buttons[label] = button
            ttk.Separator(content, orient='vertical').grid(row=0, column=1, sticky='ns')
        right = ttk.Frame(content, padding=(16, 12))
        right.grid(row=0, column=2, sticky='nsew')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        intro = ttk.Frame(right)
        intro.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        self.location = ttk.Label(intro, style='Small.TLabel')
        self.location.pack(anchor='w')
        self.group_title = ttk.Label(intro, style='Section.TLabel')
        self.group_title.pack(anchor='w', pady=(4, 4))
        self.group_description = ttk.Label(intro, style='Muted.TLabel', wraplength=400)
        self.group_description.pack(anchor='w')
        self._clip = tk.Canvas(right, highlightthickness=0, borderwidth=0, width=400, height=220)
        self._clip.grid(row=1, column=0, sticky='nsew')
        self._bar = ttk.Scrollbar(right, orient='vertical', command=self._clip.yview)
        self._bar.grid(row=1, column=1, sticky='ns', padx=(8, 0))
        self._clip.configure(yscrollcommand=self._scroll_changed)
        self._body = self._action_area = ttk.Frame(self._clip)
        self._window_item = self._clip.create_window((0, 0), window=self._body, anchor='nw')
        self._clip.bind('<Configure>', self._canvas_resized)
        self._body.bind('<Configure>', lambda _e: self._clip.configure(scrollregion=self._clip.bbox('all')))
        panel.bind('<MouseWheel>', self._wheel)
        panel.bind('<Button-4>', lambda _e: self._scroll(-1))
        panel.bind('<Button-5>', lambda _e: self._scroll(1))
        if self.flat_groups:
            self._action_paths = {}
            self.location.configure(text=self.cget('text'))
            self.group_title.pack_forget()
            self.group_description.configure(text='見出しの下から操作を選びます')
            self.group_description.pack_forget()
            self.action_buttons, self.action_descriptions = {}, {}
            for item in groups:
                label = menu.entrycget(item, 'label')
                child_menu = self.nametowidget(str(menu.entrycget(item, 'menu')))
                heading = ttk.Frame(self._action_area)
                heading.pack(fill='x', pady=(6, 3))
                ttk.Frame(heading, width=3, style='Navigation.Mark.TFrame').pack(side='left', fill='y', padx=(0, 8))
                ttk.Label(heading, text=label, style='Section.TLabel').pack(side='left')
                ttk.Separator(heading).pack(side='left', fill='x', expand=True, padx=8)
                self._populate_actions(child_menu, group=label)
        elif groups:
            selected = self.active_group if self.active_group in self._group_menus else next(iter(self._group_menus))
            self.show_group(selected)
        else:
            self.location.configure(text=f'{self.cget("text")} / 操作')
            self.group_title.configure(text='ファイルの追加方法')
            self.group_description.configure(text='追加する範囲や、自動で取り込む方法を選びます')
            self._populate_actions(menu)
        panel.update_idletasks()
        self._position_panel()
        panel.deiconify()
        panel.lift()
        panel.update_idletasks()
        self._owner_geometry = root.winfo_geometry()
        # Root bindings run only for events in the working window. No grab is
        # needed: another menu, the queue and other apps remain accessible.
        for sequence, handler in (('<ButtonPress-1>', self._outside_click),
                                  ('<Configure>', self._root_changed),
                                  ('<Unmap>', self._root_changed)):
            self._bindings.append((root, sequence, root.bind(sequence, handler, add='+')))
        target = self.group_buttons.get(self.active_group) or next(iter(self._enabled_actions()), self.close_button)
        target.focus_force()
        return 'break'

    def _position_panel(self):
        root = self.winfo_toplevel()
        scale = max(1, float(self.tk.call('tk', 'scaling')) / 1.333333)
        gap = round(8 * scale)
        left, top = root.winfo_rootx()+gap, root.winfo_rooty()+gap
        # Screenwidth can describe only the primary display on Windows. Anchor
        # to the working window so menus also fit on a secondary monitor.
        right = root.winfo_rootx()+root.winfo_width()-gap
        bottom = root.winfo_rooty()+root.winfo_height()-gap
        width = min(round((600 if self.flat_groups else 760)*scale), max(1, right-left))
        height = round(400*scale)
        if self.flat_groups:
            self._clip.itemconfigure(self._window_item, width=width - round(64*scale))
            self.panel.update_idletasks()
            height = self.panel.winfo_reqheight() - self._clip.winfo_reqheight() + self._body.winfo_reqheight()
        height = min(height, max(1, bottom-top))
        x = max(left, min(self.winfo_rootx(), right-width))
        below = self.winfo_rooty()+self.winfo_height()+gap
        above = self.winfo_rooty()-gap-height
        y = below if below+height <= bottom else above
        y = max(top, min(y, bottom-height))
        self.panel.geometry(f'{width}x{height}+{x}+{y}')

    def _populate_actions(self, menu, *, group=None):
        end = menu.index('end')
        if group is None:
            self.action_buttons, self.action_descriptions = {}, {}
        for item in range(end+1 if end is not None else 0):
            if menu.type(item) == 'separator':
                continue
            row = ttk.Frame(self._action_area, padding=(12, 1, 0, 3) if group else (0, 4, 0, 8))
            row.pack(fill='x')
            label = menu.entrycget(item, 'label')
            if menu.type(item) == 'checkbutton':
                command = menu.entrycget(item, 'command')
                widget = ToggleSwitch(row, text=label, variable=str(menu.entrycget(item, 'variable')),
                    command=lambda cmd=command: menu.tk.call(cmd) if cmd else None)
                attach_icon(widget, menu.icon_name(item))
            else:
                def invoke(position=item, source=menu):
                    self.close()
                    source.invoke(position)
                widget = IconButton(row, text=label, icon=menu.icon_name(item),
                    command=invoke, style='Drawer.TButton')
            widget.configure(state=menu.entrycget(item, 'state'))
            widget.pack(fill='x')
            widget.bind('<Return>', lambda _e, control=widget: (control.invoke(), 'break')[1])
            widget.bind('<space>', lambda _e, control=widget: (control.invoke(), 'break')[1])
            help_text = getattr(menu, '_navigation_help', {}).get(item, '')
            accelerator = menu.entrycget(item, 'accelerator')
            if accelerator:
                help_text = f'{help_text}  ({accelerator})' if help_text else accelerator
            description = ttk.Label(row, text=help_text, style='Small.TLabel', padding=(12, 0), wraplength=400)
            description.pack(anchor='w', fill='x', pady=(4, 0))
            description.bind('<Button-1>', lambda _e, control=widget: control.invoke())
            widget.bind('<FocusIn>', self._reveal)
            if group:
                path = f'{self.cget("text")} › {group} › {label}'
                self._action_paths[widget] = path
                def show_path(_event, text=path):
                    self.location.configure(text=text)
                row.bind('<Enter>', show_path)
                row.bind('<Leave>', self._leave_action)
                widget.bind('<FocusIn>', show_path, add='+')
                widget.bind('<Motion>', show_path)
                description.bind('<Motion>', show_path)
            widget.bind('<Left>', lambda _e: self._focus_category())
            widget.bind('<Home>', lambda _e: self._focus_actions())
            widget.bind('<End>', lambda _e: self._focus_actions(-1))
            for sequence, offset in (('<Up>', -1), ('<Down>', 1)):
                widget.bind(sequence, lambda _e, w=widget, step=offset: self._move_action(w, step))
            self.action_buttons[label] = widget
            self.action_descriptions[label] = description

    def show_group(self, label):
        if label not in self._group_menus:
            return
        self.active_group = label
        for key, button in self.group_buttons.items():
            button.state(['selected'] if key == label else ['!selected'])
        menu = self._group_menus[label]
        self.location.configure(text=f'{self.cget("text")} / {label}')
        self.group_title.configure(text=label)
        self.group_description.configure(text=getattr(menu, '_navigation_description', '実行する操作を選びます'))
        for child in self._action_area.winfo_children():
            child.destroy()
        self._populate_actions(menu)
        self._clip.yview_moveto(0)

    def _bind_category_keys(self, button, label):
        for sequence, step in (('<Up>', -1), ('<Down>', 1)):
            button.bind(sequence, lambda _e, key=label, offset=step: self._move_category(key, offset))
        button.bind('<Home>', lambda _e: self._select_category(0))
        button.bind('<End>', lambda _e: self._select_category(-1))
        button.bind('<Right>', lambda _e: self._focus_actions())
        button.bind('<Return>', lambda _e: (self.show_group(label), 'break')[1])

    def _select_category(self, index):
        label = list(self.group_buttons)[index]
        self.show_group(label)
        self.group_buttons[label].focus_set()
        return 'break'

    def _move_category(self, label, step):
        labels = list(self.group_buttons)
        return self._select_category((labels.index(label)+step) % len(labels))

    def _leave_action(self, event):
        if not self.opened:
            return
        target = self.winfo_containing(event.x_root, event.y_root)
        # Tk also sends Leave to a row when entering one of its children.
        if target is event.widget or getattr(target, 'master', None) is event.widget:
            return
        self.location.configure(text=self._action_paths.get(self.focus_get(), self.cget('text')))

    def _enabled_actions(self):
        return [b for b in self.action_buttons.values() if not b.instate(['disabled'])]

    def _focus_actions(self, index=0):
        buttons = self._enabled_actions()
        if buttons:
            buttons[index].focus_set()
        return 'break'

    def _focus_category(self):
        target = self.group_buttons.get(self.active_group)
        if target is not None:
            target.focus_set()
        return 'break'

    def _move_action(self, widget, step):
        buttons = self._enabled_actions()
        if widget in buttons:
            buttons[(buttons.index(widget)+step) % len(buttons)].focus_set()
        return 'break'

    def _canvas_resized(self, event):
        self._clip.itemconfigure(self._window_item, width=event.width)
        for description in self.action_descriptions.values():
            description.configure(wraplength=max(100, event.width-24))
        self.group_description.configure(wraplength=max(100, event.width))

    def _scroll_changed(self, first, last):
        self._bar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self._bar.state(['disabled'])
        else:
            self._bar.state(['!disabled'])

    def _reveal(self, event):
        row = event.widget.master
        top = row.winfo_y()
        bottom = top+row.winfo_height()
        visible = self._clip.canvasy(0)
        height = max(1, self._body.winfo_height())
        if top < visible:
            self._clip.yview_moveto(top/height)
        elif bottom > visible+self._clip.winfo_height():
            self._clip.yview_moveto((bottom-self._clip.winfo_height())/height)

    def _wheel(self, event):
        if event.delta:
            return self._scroll(-1 if event.delta > 0 else 1)

    def _scroll(self, units):
        # Canvas can otherwise scroll short lists past their upper boundary.
        first, last = self._clip.yview()
        if (units < 0 and first > 0) or (units > 0 and last < 1):
            self._clip.yview_scroll(units, 'units')
        return 'break'

    def _outside_click(self, event):
        if event.widget is not self:
            self.close()

    def _root_changed(self, event):
        root = self.winfo_toplevel()
        # Windows can also send Configure when a transient changes stacking order.
        if event.widget is root and (event.type == tk.EventType.Unmap or
                                     root.winfo_geometry() != self._owner_geometry):
            self.close()

    def _focus_in(self, _event):
        self._focus_entered = True
        if self._focus_job is not None:
            self.after_cancel(self._focus_job)
            self._focus_job = None

    def _focus_out(self, event):
        if not self.opened or not self._focus_entered or event.widget is not self.panel:
            return
        if self._focus_job is not None:
            self.after_cancel(self._focus_job)
        # Let the window manager finish activating the newly mapped popup.
        self._focus_job = self.after(100, self._check_focus)

    def _check_focus(self):
        self._focus_job = None
        if self.opened:
            focus = self.focus_get()
            if focus is None or focus.winfo_toplevel() is not self.panel:
                self.close()

    def _dismiss(self):
        self.close(restore_focus=True)
        return 'break'

    def _destroyed(self, event):
        if event.widget is self:
            self.close()

    def close(self, *, restore_focus=False):
        if not self.opened:
            return
        self.opened = False
        root = self.winfo_toplevel()
        if getattr(root, '_navigation_owner', None) is self:
            root._navigation_owner = None
        if self.winfo_exists():
            self.state(['!selected'])
        if self._focus_job is not None:
            self.after_cancel(self._focus_job)
            self._focus_job = None
        for root, sequence, token in self._bindings:
            root.unbind(sequence, token)
        self._bindings.clear()
        panel, self.panel = self.panel, None
        if panel.winfo_exists():
            panel.destroy()
        if restore_focus and self.winfo_exists():
            self.focus_force()



def input_folder(source) -> str:
    from pathlib import Path
    value = str(source).strip()
    if not value:
        raise ValueError('先に入力ファイルを選択してください。')
    return str(Path(value).expanduser().resolve().parent)


def destination_buttons(parent,*,default_command,same_command=None,same_text='入力と同じ場所'):
    row=ttk.Frame(parent)
    if same_command:
        IconButton(row,text=same_text,icon='folder-same',command=same_command).pack(side='left',padx=(0,7))
    IconButton(row,text='デフォルト',icon='folder',command=default_command).pack(side='left')
    return row
