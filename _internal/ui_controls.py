"""Small standard-Tk controls: switch indicators, inline drawers and OS appearance."""
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


class AccordionButton(IconMenubutton):
    """Present an existing Tk menu as an inline, keyboard-accessible action panel."""
    def __init__(self, master, *, host, max_height=None, on_toggle=None, **kwargs):
        super().__init__(master, **kwargs)
        self.host = host
        self.max_height = max_height
        self.on_toggle = on_toggle
        self.opened = False
        self.active_group = None
        self.group_buttons = {}
        self.action_buttons = {}
        self._animation = None
        self.bind('<Button-1>', self.toggle)
        self.bind('<space>', self.toggle)
        self.bind('<Return>', self.toggle)
        self.bind('<Down>', self.toggle)
        self.bind('<Destroy>', lambda e: self.tk.call('after', 'cancel', self._animation)
                  if e.widget is self and self._animation else None)

    def toggle(self, _event=None):
        if self.instate(['disabled']):
            return 'break'
        if self.opened:
            self.close()
            return 'break'
        previous = getattr(self.host, '_drawer_owner', None)
        if previous is not None:
            previous.close()
        self.host._drawer_owner = self
        for child in self.host.winfo_children():
            child.destroy()
        menu = self.nametowidget(str(self.cget('menu')))
        self.opened = True
        clip = tk.Canvas(self.host, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self.host, orient='vertical', command=clip.yview)
        clip.configure(yscrollcommand=bar.set)
        clip.pack(side='left', fill='both', expand=True)
        body = ttk.Frame(clip, padding=(10, 7))
        self._clip,self._bar,self._body=clip,bar,body
        window_item = clip.create_window((0, 0), window=body, anchor='nw',
                                         width=max(200, self.host.winfo_width()))
        clip.bind('<Configure>', lambda e: clip.itemconfigure(window_item, width=e.width))
        body.bind('<Configure>', lambda _e: clip.configure(scrollregion=clip.bbox('all')))
        def reveal(event):
            top=event.widget.winfo_rooty()-body.winfo_rooty()
            bottom=top+event.widget.winfo_height()
            visible = clip.canvasy(0)
            height = max(1, body.winfo_height())
            if top < visible:
                clip.yview_moveto(top/height)
            elif bottom > visible+clip.winfo_height():
                clip.yview_moveto((bottom-clip.winfo_height())/height)
        self._reveal=reveal
        self.group_buttons={};self.action_buttons={}
        end=menu.index('end')
        groups=[item for item in range(end+1 if end is not None else 0) if menu.type(item)=='cascade']
        self._group_menus={}
        if groups:
            categories=ttk.Frame(body);categories.pack(fill='x',pady=(0,6))
            for column,item in enumerate(groups):
                label=menu.entrycget(item,'label')
                self._group_menus[label]=self.nametowidget(str(menu.entrycget(item,'menu')))
                categories.columnconfigure(column,weight=1,uniform='category')
                button=IconButton(categories,text=label,icon=menu.icon_name(item),style='Category.TButton',
                    command=lambda key=label:self.show_group(key))
                button.grid(row=0,column=column,sticky='ew',padx=3)
                button.bind('<FocusIn>',reveal)
                self.group_buttons[label]=button
            self._action_area=ttk.Frame(body);self._action_area.pack(fill='x')
            selected=self.active_group if self.active_group in self._group_menus else next(iter(self._group_menus))
            self.show_group(selected,resize=False)
        else:
            self._action_area=body
            self._populate_actions(menu)
        self.host.grid()
        self._resize_drawer(animate=True)
        return 'break'

    def _populate_actions(self,menu):
        area=self._action_area
        end=menu.index('end')
        items=[item for item in range(end+1 if end is not None else 0) if menu.type(item)!='separator']
        wide=self.winfo_toplevel().winfo_width()>=1100
        columns=min(len(items) or 1,3 if wide and len(items)==3 else 2)
        for col in range(3):area.columnconfigure(col,weight=1 if col<columns else 0,uniform='action' if col<columns else '')
        self.action_buttons={}
        for index,item in enumerate(items):
            label=menu.entrycget(item,'label')
            if menu.type(item)=='checkbutton':
                variable=str(menu.entrycget(item,'variable'))
                command=menu.entrycget(item,'command')
                widget=ToggleSwitch(area,text=label,variable=variable,
                    command=lambda cmd=command:menu.tk.call(cmd) if cmd else None)
                attach_icon(widget,menu.icon_name(item))
            else:
                def invoke(position=item,source=menu):
                    self.close()
                    source.invoke(position)
                widget=IconButton(area,text=label,icon=menu.icon_name(item),command=invoke,style='Drawer.TButton')
            widget.configure(state=menu.entrycget(item,'state'))
            widget.grid(row=index//columns,column=index%columns,sticky='ew',padx=3,pady=3)
            widget.bind('<FocusIn>',self._reveal)
            self.action_buttons[label]=widget

    def show_group(self,label,*,resize=True):
        if label not in self._group_menus:return
        self.active_group=label
        for key,button in self.group_buttons.items():
            button.state(['selected'] if key==label else ['!selected'])
        for child in self._action_area.winfo_children():child.destroy()
        self._populate_actions(self._group_menus[label])
        if resize:self._resize_drawer()

    def _resize_drawer(self,*,animate=False):
        self.host.update_idletasks()
        target = max(1, self._body.winfo_reqheight())
        limit = self.max_height() if callable(self.max_height) else self.max_height
        if limit is not None and target > limit:
            target = max(80, int(limit))
            self._bar.pack(side='right', fill='y')
        else:self._bar.pack_forget()
        self._clip.configure(height=target)
        self._clip.yview_moveto(0)
        self.host._expanded_height = target
        self.host.pack_propagate(False)
        self.host.configure(height=1 if animate else target)
        if self.on_toggle:
            self.on_toggle(self)
        if self._animation is not None:
            self.after_cancel(self._animation);self._animation=None
        if not animate:
            self.host.pack_propagate(True)
            return
        def step_height(step=1):
            if not self.opened or not self.host.winfo_exists():
                return
            self.host.configure(height=max(1, round(target*step/5)))
            if step < 5:
                self._animation = self.after(24, lambda: step_height(step+1))
            else:
                self.host.pack_propagate(True)
                self._animation = None
        step_height()

    def close(self):
        self.opened = False
        if self._animation is not None:
            self.after_cancel(self._animation)
            self._animation = None
        self.host.grid_remove()
        self.host.pack_propagate(True)
        if self.on_toggle:
            self.on_toggle(self)


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
