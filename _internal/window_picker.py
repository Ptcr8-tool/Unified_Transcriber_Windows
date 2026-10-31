"""Selectable static previews of every recording candidate, loaded off the Tk thread."""
from concurrent.futures import ThreadPoolExecutor
import queue
import tkinter as tk
from tkinter import ttk
from ui_icons import IconButton

from screen_capture import windows
from window_preview import load_thumbnail


class WindowPickerDialog:
    def __init__(self,parent,selected,apply,*,accept_text='この画面を選択'):
        from unified_gui import modal
        from studio_ui import scrollable_body
        self.previous_grab=parent.grab_current()
        self.win=modal(parent,'録画する画面を選ぶ',980)
        self.win.geometry('980x720')
        self.apply=apply
        self.selected=selected
        self.generation=0
        self.closed=False
        self.cards={};self.photos={};self.candidates=[]
        self.results=queue.Queue()
        self.pool=ThreadPoolExecutor(max_workers=3,thread_name_prefix='window-preview')
        self.futures=[]
        self.body,foot=scrollable_body(self.win,padding=16)
        top=ttk.Frame(self.body);top.pack(fill='x',pady=(0,12))
        ttk.Label(top,text='プレビューから録画対象を選択',style='Section.TLabel').pack(side='left')
        IconButton(top,text='一覧・画像を更新',command=self.refresh).pack(side='right')
        ttk.Label(self.body,text='静止画です。最小化・保護された画面などは画像を取得できない場合があります。',
                  style='Muted.TLabel',wraplength=900).pack(anchor='w',pady=(0,12))
        self.grid=ttk.Frame(self.body);self.grid.pack(fill='both',expand=True)
        self.grid.bind('<Configure>',self._layout)
        for col in range(3):self.grid.columnconfigure(col,weight=1,uniform='preview')
        self.status=tk.StringVar(self.win)
        ttk.Label(foot,textvariable=self.status,style='Small.TLabel',wraplength=530).pack(side='left')
        IconButton(foot,text='キャンセル',command=self.close).pack(side='right',padx=(8,0))
        IconButton(foot,text=accept_text,style='Primary.TButton',command=self.accept).pack(side='right')
        self.win.protocol('WM_DELETE_WINDOW',self.close)
        self.win.bind('<Escape>',lambda _e:self.close())
        self.win.bind('<Return>',lambda _e:self.accept())
        self.win.bind('<Destroy>',lambda e:self._destroy() if e.widget is self.win else None,add='+')
        self.refresh();self.win.after(70,self._poll)

    def refresh(self):
        self.generation+=1;generation=self.generation
        for future in self.futures:future.cancel()
        self.futures.clear()
        for widget in self.grid.winfo_children():widget.destroy()
        self.cards.clear();self.photos.clear()
        try:self.candidates=[(title,hwnd) for title,hwnd in windows() if '文字起こし統合スタジオ' not in title]
        except OSError as exc:
            self.status.set(str(exc));return
        self.status.set(f'{len(self.candidates)}画面 · 画像を読み込み中…' if self.candidates else '録画対象の画面がありません。')
        for index,(title,hwnd) in enumerate(self.candidates):
            card=ttk.Frame(self.grid,padding=5,style='Card.TFrame')
            card.grid(row=index//3,column=index%3,sticky='nsew',padx=5,pady=5)
            image=ttk.Label(card,text='プレビューを取得中…',anchor='center',width=27,padding=(0,30))
            image.pack(fill='both',expand=True)
            caption=ttk.Label(card,text=title,wraplength=260,padding=(5,6))
            caption.pack(fill='x')
            button=ttk.Radiobutton(card,text='この画面',value=hwnd,
                command=lambda h=hwnd:self.select(h))
            # A shared IntVar also gives native keyboard selection.
            if not hasattr(self,'selection_var'):self.selection_var=tk.IntVar(self.win,self.selected or 0)
            button.configure(variable=self.selection_var)
            button.pack(anchor='w',padx=5)
            for widget in (card,image,caption):
                widget.bind('<Button-1>',lambda _e,h=hwnd:self.select(h))
                widget.bind('<Double-Button-1>',lambda _e,h=hwnd:(self.select(h),self.accept()))
            self.cards[hwnd]=(card,image,caption,button)
            future=self.pool.submit(self._load,generation,hwnd)
            self.futures.append(future)
        self.select(self.selected)

    def _load(self,generation,hwnd):
        try:
            data,width,height=load_thumbnail(hwnd)
            self.results.put((generation,hwnd,data,f'{width} × {height}'))
        except Exception as exc:
            self.results.put((generation,hwnd,None,'画像を取得できません\n'+str(exc)[:90]))

    def _layout(self,event):
        columns=3 if event.width>=880 else 2
        for column in range(3):
            self.grid.columnconfigure(column,weight=1 if column<columns else 0,
                                      uniform='preview' if column<columns else '')
        for index,(_card,_image,label,_button) in enumerate(self.cards.values()):
            _card.grid_configure(row=index//columns,column=index%columns)
            label.configure(wraplength=max(190,event.width//columns-30))

    def _poll(self):
        if self.closed:return
        while True:
            try:generation,hwnd,data,caption=self.results.get_nowait()
            except queue.Empty:break
            if generation!=self.generation or hwnd not in self.cards:continue
            label=self.cards[hwnd][1]
            if data is not None:
                try:
                    photo=tk.PhotoImage(master=self.win,data=data,format='PPM')
                    self.photos[hwnd]=photo
                    label.configure(image=photo,text='',padding=0,width=0)
                except tk.TclError:label.configure(text='画像を読み込めません。')
            else:label.configure(text=caption,wraplength=245)
        self.win.after(70,self._poll)

    def select(self,hwnd):
        self.selected=hwnd
        if hasattr(self,'selection_var'):self.selection_var.set(hwnd or 0)
        for key,(_card,_image,label,_button) in self.cards.items():
            label.configure(style='Badge.TLabel' if key==hwnd else 'TLabel')
        title=next((title for title,key in self.candidates if key==hwnd),'')
        self.status.set(title or '録画する画面を選択してください。')

    def accept(self):
        if self.selected not in self.cards:return
        title=next(title for title,key in self.candidates if key==self.selected)
        selected=self.selected
        self.close()
        self.apply(title,selected)

    def _destroy(self):
        self.closed=True
        self.pool.shutdown(wait=False,cancel_futures=True)
        if self.previous_grab is not None:
            try:
                if self.previous_grab.winfo_exists():self.previous_grab.grab_set()
            except tk.TclError:pass

    def close(self):self.win.destroy()
