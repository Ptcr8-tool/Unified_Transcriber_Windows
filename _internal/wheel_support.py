"""Mouse-wheel routing for scrollable Tkinter modal contents.

Tk handles the wheel on Text, Listbox and ttk.Treeview natively. Canvas
widgets do not, and their descendant Labels/Entries don't either. A single
application-wide binding routes wheel input to the nearest enclosing scrollable
Canvas without binding/unbinding individual modal children.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

_NATIVE_WHEEL_WIDGETS = (tk.Text, tk.Listbox, ttk.Treeview)
_VALUE_WHEEL_WIDGETS = (ttk.Combobox,ttk.Spinbox,tk.Spinbox,ttk.Scale,tk.Scale)


def wheel_direction(event) -> int:
    """Return the number of vertical units (positive is downward)."""
    number = getattr(event, 'num', None)
    if number == 4:
        return -3
    if number == 5:
        return 3
    delta = getattr(event, 'delta', 0)
    if not delta:
        return 0
    # Most Windows mice send 120 per notch; touchpads can emit smaller values.
    return -max(1, round(abs(delta) / 120)) * 3 if delta > 0 else max(1, round(abs(delta) / 120)) * 3


def wheel_canvas_for(widget):
    """Find the nearest scrollable Canvas; keep native widgets native."""
    current = widget
    while current is not None:
        if isinstance(current, _NATIVE_WHEEL_WIDGETS) and not getattr(current, '_scroll_outer', False):
            return None
        if isinstance(current, tk.Canvas):
            return current
        current = getattr(current, 'master', None)
    return None


def scroll_canvas_for_event(widget, direction: int) -> bool:
    """Route a wheel event to a Canvas only when it has room to move."""
    canvas = wheel_canvas_for(widget)
    if canvas is None or direction == 0:
        return False
    try:
        first, last = canvas.yview()
        if last - first >= 1 - 1e-8:
            return False
        if direction < 0 and first <= 1e-8:
            return False
        if direction > 0 and last >= 1 - 1e-8:
            return False
        canvas.yview_scroll(direction, 'units')
        return True
    except (tk.TclError, ValueError, RuntimeError):
        return False


def install_wheel_routing(root: tk.Misc) -> None:
    """Before native class bindings, require a click to change a selection."""
    if getattr(root,'_click_wheel_guard',None) is not None:return
    tag=f'ClickWheelGuard{id(root)}';state={'armed':None};root._click_wheel_guard=state
    def click(event):
        armed=state['armed']
        if armed is not None and str(event.widget).startswith(str(armed)+'.popdown'):return
        state['armed']=event.widget if isinstance(event.widget,_VALUE_WHEEL_WIDGETS) else None
    def focus_out(event):
        if state['armed'] is event.widget:
            if isinstance(event.widget,ttk.Combobox):
                try:
                    if str(root.tk.call('focus')).startswith(str(event.widget)+'.popdown'):return
                except tk.TclError:pass
            state['armed']=None
    def focus_in(_event):
        # A combobox temporarily focuses its Tcl-owned popup. When keyboard
        # navigation leaves that popup, the combobox itself gets no second
        # FocusOut event. Disarm on the new focus as well, preserving its own
        # popup and using current focus to avoid queued-event ordering races.
        armed=state['armed']
        if armed is None:return
        try:focused=str(root.tk.call('focus'))
        except tk.TclError:focused=''
        if focused!=str(armed) and not focused.startswith(str(armed)+'.popdown'):
            state['armed']=None
    def guard(event):
        widget=event.widget
        # Form lists and speaker previews show a complete page. Their wheel
        # belongs to the containing form, not a hidden second scroll position.
        if isinstance(widget, _NATIVE_WHEEL_WIDGETS) and getattr(widget, '_scroll_outer', False):
            if not (getattr(event,'state',0)&1):
                scroll_canvas_for_event(widget, wheel_direction(event))
                return 'break'
            return None
        if not isinstance(widget,_VALUE_WHEEL_WIDGETS):return None
        try:armed=state['armed'] is widget and str(widget.tk.call('focus'))==str(widget)
        except tk.TclError:armed=False
        if armed:return None
        if not (getattr(event,'state',0)&1):scroll_canvas_for_event(widget,wheel_direction(event))
        return 'break'
    def attach(widget):
        if tag not in widget.bindtags():widget.bindtags((tag,*widget.bindtags()))
        for path in widget.tk.splitlist(widget.tk.call('winfo','children',str(widget))):
            try:child=widget.nametowidget(path)
            except KeyError:continue  # Tk's own combobox popdowns lack Python wrappers
            attach(child)
    def mapped(event):
        if isinstance(event.widget,tk.Misc):attach(event.widget)
    root.bind_class(tag,'<ButtonPress-1>',click)
    root.bind_class(tag,'<FocusOut>',focus_out)
    for sequence in ('<MouseWheel>','<Button-4>','<Button-5>'):root.bind_class(tag,sequence,guard)
    root.bind_all('<ButtonPress-1>',click,add='+');root.bind_all('<Map>',mapped,add='+')
    root.bind_all('<FocusIn>',focus_in,add='+');attach(root)
    def on_wheel(event):
        if isinstance(event.widget,_VALUE_WHEEL_WIDGETS):return 'break'
        if getattr(event,'state',0)&1:return None
        if scroll_canvas_for_event(event.widget,wheel_direction(event)):return 'break'
        return None
    for sequence in ('<MouseWheel>','<Button-4>','<Button-5>'):root.bind_all(sequence,on_wheel,add='+')
