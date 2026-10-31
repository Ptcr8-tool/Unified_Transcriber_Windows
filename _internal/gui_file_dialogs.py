"""File-oriented dialogs for the transcription studio.

The app supplies its shared UI services through ``_ui()``.  The small binding
keeps these dialogs independent of the entry point (which can run as __main__)
and preserves existing monkeypatch and extension points in unified_gui.
"""
from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ui_icons import IconButton
from studio_ui import UI_FONT, bind_variable_trace, scrollable_body
from ui_controls import ToggleSwitch, destination_buttons
from core import default_directory, SUPPORTED_EXTENSIONS
from media_tools import AUDIO_FORMATS, BITRATES
from merge_metadata import oldest_first, show_datetime


def _report_error(app, message):
    """Keep failures diagnostic without touching Tk from worker threads."""
    events = getattr(app, 'events', None)
    if events is not None:
        events.put(('errorlog', message + '\n' + traceback.format_exc()))


class FormListPages:
    """Show a complete page inside a form, with selection retained across pages."""
    PAGE_SIZE = 20

    def __init__(self, widget, parent, paths, render_item, *, tree=False):
        self.widget, self._paths, self.render_item, self.tree = widget, paths, render_item, tree
        self.page = 0
        self.visible = {}
        self.selected = set()
        widget._scroll_outer = True
        controls = ttk.Frame(parent)
        controls.pack(fill='x', pady=(3, 0))
        self.previous = IconButton(controls, text='前のページ', command=lambda:self.go(-1))
        self.previous.pack(side='left')
        self.next = IconButton(controls, text='次のページ', command=lambda:self.go(1))
        self.next.pack(side='left', padx=(6, 0))
        self.label = tk.StringVar(parent)
        ttk.Label(controls, textvariable=self.label, style='Small.TLabel').pack(side='left', padx=8)
        self.refresh()

    @property
    def paths(self):
        return self._paths()

    def capture(self):
        ids = self.widget.selection() if self.tree else self.widget.curselection()
        selected_now = {self.visible[str(i)] for i in ids if str(i) in self.visible}
        self.selected.difference_update(self.visible.values())
        self.selected.update(selected_now)

    def selected_indices(self):
        self.capture()
        return [i for i, path in enumerate(self.paths) if path in self.selected]

    def refresh(self, *, capture=True):
        if capture:
            self.capture()
        self.selected.intersection_update(self.paths)
        pages = max(1, (len(self.paths) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * self.PAGE_SIZE
        if self.tree:
            self.widget.delete(*self.widget.get_children())
        else:
            self.widget.delete(0, 'end')
        self.visible = {}
        for offset, path in enumerate(self.paths[start:start+self.PAGE_SIZE]):
            index = start + offset
            key = str(index if self.tree else offset)
            self.render_item(index, path, key)
            self.visible[key] = path
            if path in self.selected:
                if self.tree:self.widget.selection_add(key)
                else:self.widget.selection_set(offset)
        # Size to the visible page: the form owns vertical scrolling.
        self.widget.configure(height=max(5, len(self.visible)))
        self.previous.configure(state='normal' if self.page else 'disabled')
        self.next.configure(state='normal' if self.page + 1 < pages else 'disabled')
        self.label.set(f'{len(self.paths)} 件 · {self.page + 1} / {pages} ページ')

    def go(self, direction):
        self.capture()
        self.page += direction
        self.refresh(capture=False)


class WatchDialog:
    def __init__(self, app: 'UnifiedApp'):
        self.app=app
        self.win=self._ui()['modal'](app.root,'フォルダー監視',660)
        self.win.geometry('660x365')
        box=ttk.Frame(self.win,padding=22)
        box.pack(fill='both',expand=True)
        box.columnconfigure(0,weight=1)
        ttk.Label(box,text='監視するフォルダー',font=(UI_FONT,11,'bold')).grid(
            row=0,column=0,columnspan=2,sticky='w',pady=6)
        self.path=tk.StringVar(value=str(self._ui()['BASE'] / 'input'))
        ttk.Entry(box,textvariable=self.path).grid(row=1,column=0,sticky='ew',pady=8)
        IconButton(box,text='参照…',command=self._browse).grid(row=1,column=1,padx=(8,0))
        ttk.Label(box,text='このフォルダーに追加された音声・動画を現在選択中の方式で順次処理します。\n'
                          '元ファイルが書き込み完了後に処理し、成功した音声とTXTは\n'
                          '「processed」フォルダーへ、失敗した元ファイルは「failed」へ移動します。\n'
                          'サブフォルダーの監視はしません。監視を停止するまで動作します。',
                  wraplength=580).grid(row=2,column=0,columnspan=2,sticky='w',pady=(10,17))
        footer=ttk.Frame(box)
        footer.grid(row=3,column=0,columnspan=2,sticky='e')
        IconButton(footer,text='キャンセル',command=self.win.destroy).pack(side='right',padx=(8,0))
        IconButton(footer,text='監視開始',command=self._start).pack(side='right')

    def _browse(self):
        path=filedialog.askdirectory(parent=self.win,title='監視対象フォルダーを指定')
        if path:self.path.set(path)

    def _start(self):
        path=Path(self.path.get().strip()).expanduser()
        if not self.path.get().strip():
            self._ui()['show_error'](self.win,'監視するフォルダーを指定してください。'); return
        if path.exists() and not path.is_dir():
            self._ui()['show_error'](self.win,'監視先はフォルダーにしてください。'); return
        self.win.destroy()
        self.app.start_watch(path)


class ConcatDialog:
    """Ordered audio/TXT/CSV join with explicit output folder policy."""
    DESTINATIONS = (('first', '最初のファイルと同じフォルダー'),
                    ('merged', 'デフォルト'),
                    ('custom', '任意のフォルダー'))

    def __init__(self, app: 'UnifiedApp', kind: str):
        self.app, self.kind = app, kind
        self.paths: list[Path] = []
        self.file_info = {}
        self.metadata_queue = queue.Queue()
        self.metadata_pending = set()
        self.metadata_jobs = queue.Queue()
        self.metadata_lock = threading.Lock()
        self.metadata_worker_count = 0
        self.metadata_stop = threading.Event()
        self.sort_mode = tk.StringVar(app.root, 'oldest')
        self.manual_order = False
        audio = kind == 'audio'
        title = '音声・動画 → 音声結合' if audio else ('CSV結合（TXTも入力可能）' if kind == 'csv' else 'TXTファイルの結合')
        self.win = self._ui()['modal'](app.root, title, 810)
        self.win.bind('<Destroy>', lambda event: self.metadata_stop.set()
                      if event.widget is self.win else None, add='+')
        self.win.geometry('1000x790' if audio else '1000x675')
        self.fmt = tk.StringVar(self.win, 'mp3')
        self.bitrate = tk.StringVar(self.win, '192k')
        self.channels = tk.StringVar(self.win, '1（モノラル）')
        self.sample_rate = tk.StringVar(self.win, '48000')
        self.auto_transcribe = tk.BooleanVar(self.win, True)
        self.headings = tk.BooleanVar(self.win, True)
        self.move_originals = tk.BooleanVar(self.win, False)
        self.destination_mode = tk.StringVar(self.win, 'first')
        self.custom_folder = tk.StringVar(self.win, str(default_directory('merged')))
        self.filename = tk.StringVar(self.win, '')
        self.output = tk.StringVar(self.win, '')  # preview of final full pathname
        self.name_edited = False
        self._setting_name = False
        main, foot = scrollable_body(self.win, padding=(16,12))
        ttk.Label(main, text='結合したいファイルを順番に追加してください。上下ボタンで順番を変更できます。',
                  wraplength=750).pack(anchor='w', pady=(0,7))
        fileframe = ttk.Frame(main)
        fileframe.pack(fill='both', expand=True)
        fileframe.columnconfigure(0, weight=1)
        fileframe.rowconfigure(0, weight=1)
        self.listbox = ttk.Treeview(fileframe, columns=('order','filename','recorded','created','modified','sort_source'),
                                    show='headings',selectmode='extended',height=5)
        for col,title,width in (('order','順',38),('filename','ファイル名',235),
                 ('recorded','メディア作成・録音日時',155),('created','ファイル作成日時',145),
                 ('modified','更新日時',145),('sort_source','並び替えの根拠',215)):
            self.listbox.heading(col,text=title)
            self.listbox.column(col,width=width,stretch=(col=='filename'))
        self.listbox.grid(row=0,column=0,sticky='nsew')
        horizontal = ttk.Scrollbar(fileframe, orient='horizontal', command=self.listbox.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.listbox.configure(xscrollcommand=horizontal.set)
        buttons=ttk.Frame(fileframe)
        buttons.grid(row=0,column=1,sticky='ns',padx=(12,0))
        for label, action in (('追加…',self._add_dialog), ('削除',self._remove),
                              ('↑ 上へ',lambda:self._move(-1)),('↓ 下へ',lambda:self._move(1)),
                              ('クリア',self._clear)):
            IconButton(buttons,text=label,command=action,width=12).pack(fill='x',pady=2)
        if self._ui()['DND_FILES'] is not None and hasattr(self.listbox,'drop_target_register'):
            try:
                self.listbox.drop_target_register(self._ui()['DND_FILES'])
                self.listbox.dnd_bind('<<Drop>>',self._drop)
            except (tk.TclError,AttributeError):
                _report_error(self.app, '結合画面のドラッグ＆ドロップ登録に失敗')
        self.pages = FormListPages(self.listbox, main, lambda:self.paths, self._render_item, tree=True)
        orderbar=ttk.Frame(main)
        orderbar.pack(fill='x',pady=(5,2))
        ttk.Label(orderbar,text='結合順：').pack(side='left')
        for value,label in (('oldest','録音日時の古い順（標準）'),('added','手動順・追加順')):
            ttk.Radiobutton(orderbar,text=label,variable=self.sort_mode,value=value,
                            command=self._sort_changed).pack(side='left',padx=(0,12))
        IconButton(orderbar,text='日時順に並べ直す',command=self._sort_oldest).pack(side='left')
        ttk.Label(main,text='録音日時タグ → ファイル作成日時 → 更新日時の順で代用（取得元を明示）。動画・音声タグは非同期で検査します。',
                  foreground='#63718a',wraplength=920).pack(anchor='w',pady=(3,5))
        self.win.after(130,self._poll_metadata)

        folder_box = ttk.LabelFrame(main, text='保存場所（既定：先頭ファイルと同じ場所）',padding=(10,8))
        folder_box.pack(fill='x',pady=(0,6))
        radios=ttk.Frame(folder_box)
        radios.pack(fill='x')
        for value,label in self.DESTINATIONS:
            ttk.Radiobutton(radios,text=label,value=value,variable=self.destination_mode,
                            command=self._refresh_output).pack(side='left',padx=(0,15))
        picker=ttk.Frame(folder_box)
        picker.pack(fill='x',pady=(6,0))
        ttk.Label(picker,text='保存フォルダー').pack(side='left')
        self.folder_display = tk.StringVar(self.win)
        ttk.Entry(picker,textvariable=self.folder_display,state='readonly').pack(side='left',fill='x',expand=True,padx=6)
        IconButton(picker,text='任意の場所を選ぶ…',command=self._browse_output).pack(side='right')
        destination_buttons(folder_box,same_command=lambda:(self.destination_mode.set('first'),self._refresh_output()),
            default_command=lambda:(self.destination_mode.set('merged'),self._refresh_output())).pack(anchor='w',pady=(5,0))
        namerow=ttk.Frame(main)
        namerow.pack(fill='x',pady=(1,6))
        ttk.Label(namerow,text='出力ファイル名').pack(side='left')
        ttk.Entry(namerow,textvariable=self.filename).pack(side='left',fill='x',expand=True,padx=(9,8))
        IconButton(namerow,text='自動名に戻す',command=self._reset_filename).pack(side='right')
        ttk.Label(main,textvariable=self.output,foreground='#63718a',wraplength=760).pack(anchor='w',pady=(0,6))
        bind_variable_trace(self.win, self.filename, 'write',self._filename_changed)
        bind_variable_trace(self.win, self.fmt, 'write',lambda *_:self._on_format())
        if audio:
            opts=ttk.LabelFrame(main,text='音声エンコード設定',padding=(10,8))
            opts.pack(fill='x',pady=(3,6))
            for row,(label,var,choices) in enumerate((
                    ('形式',self.fmt,AUDIO_FORMATS),('ビットレート',self.bitrate,BITRATES),
                    ('チャンネル',self.channels,('1（モノラル）','2（ステレオ）')),
                    ('サンプルレート',self.sample_rate,('16000','44100','48000')))):
                col=(row%2)*2; rr=row//2
                ttk.Label(opts,text=label).grid(row=rr,column=col,sticky='w',padx=(3,6),pady=3)
                ttk.Combobox(opts,textvariable=var,values=choices,state='readonly',width=18).grid(
                    row=rr,column=col+1,sticky='w',padx=(0,16),pady=3)
            ToggleSwitch(main,variable=self.auto_transcribe,
                text='結合後に自動的に文字起こしする（標準ON）').pack(anchor='w',pady=(2,3))
        elif self.kind=='text':
            ToggleSwitch(main,variable=self.headings,
                text='各ファイルの前に元ファイル名の見出しを挿入').pack(anchor='w',pady=(2,3))
        else:
            ttk.Label(main,text='CSVと話者分離済TXTの混在に対応。元ファイル名・話者・時刻を保持します。',
                      foreground='#63718a').pack(anchor='w')
        ToggleSwitch(main,variable=self.move_originals,
            text='結合成功後、元ファイルを各フォルダーの _old に移動').pack(anchor='w',pady=(7,2))
        ttk.Label(main,text='同名の元ファイルは連番で保存。結合に失敗・中止した場合は移動しません。',
            style='Muted.TLabel').pack(anchor='w',pady=(0,3))
        IconButton(foot,text='キャンセル',command=self.win.destroy).pack(side='right',padx=(8,0))
        IconButton(foot,text='結合を開始',style='Primary.TButton',command=self._start).pack(side='right')
        self._refresh_output()

    def _suffix(self):
        return '.'+(self.fmt.get() if self.kind=='audio' else 'csv' if self.kind=='csv' else 'txt')

    def _automatic_name(self):
        first=self.paths[0].stem if self.paths else 'ファイル名'
        return f'結合{len(self.paths)}ファイル_{first}{self._suffix()}' if self.paths else '結合0ファイル_ファイル名'+self._suffix()

    def _folder(self):
        mode=self.destination_mode.get()
        return (self.paths[0].parent if mode=='first' and self.paths else
                Path(self.custom_folder.get()).expanduser() if mode=='custom' else default_directory('merged'))

    def _filename_changed(self,*_):
        if not self._setting_name:self.name_edited=True
        self._refresh_output(update_name=False)

    def _reset_filename(self):
        self.name_edited=False
        self._refresh_output()

    def _refresh_output(self,update_name=True):
        if update_name and not self.name_edited:
            self._setting_name=True
            self.filename.set(self._automatic_name())
            self._setting_name=False
        folder=self._folder()
        self.folder_display.set(str(folder))
        self.output.set(str(folder / self.filename.get()))

    def _on_format(self):
        if self.kind=='audio' and self.name_edited:
            current=self.filename.get()
            if Path(current).suffix.lower() in tuple('.'+x for x in AUDIO_FORMATS):
                self._setting_name=True
                self.filename.set(str(Path(current).with_suffix(self._suffix())))
                self._setting_name=False
        self._refresh_output()

    def _inspect_file_async(self,path:Path):
        if path in self.file_info or path in self.metadata_pending:
            return
        self.metadata_pending.add(path)
        self.metadata_jobs.put(path)
        with self.metadata_lock:
            if self.metadata_worker_count >= 4:
                return
            self.metadata_worker_count += 1
        threading.Thread(target=self._metadata_worker,daemon=True).start()

    def _metadata_worker(self):
        while True:
            with self.metadata_lock:
                try:
                    path=self.metadata_jobs.get_nowait() if not self.metadata_stop.is_set() else None
                except queue.Empty:
                    path=None
                if path is None:
                    self.metadata_worker_count-=1
                    return
            try:
                engine=self.app.live_capture_python()
                details=self._ui()['inspect_merge_file'](path,engine_python=engine if engine.is_file() else None)
            except (OSError,ValueError) as exc:
                details=exc
            except Exception as exc:
                details=exc
                self.app.events.put(('errorlog','結合ファイルの情報取得エラー:\n'+traceback.format_exc()))
            self.metadata_queue.put((path,details))

    def _poll_metadata(self):
        if not self.win.winfo_exists():return
        changed=False
        try:
            while True:
                path,info=self.metadata_queue.get_nowait()
                self.metadata_pending.discard(path)
                if not isinstance(info,Exception):
                    self.file_info[path]=info
                    changed=True
        except queue.Empty:
            pass
        if changed:
            if self.sort_mode.get()=='oldest' and not self.manual_order:
                self.paths=oldest_first(self.paths,self.file_info)
            self._render()
        self.win.after(150,self._poll_metadata)

    def _sort_changed(self):
        if self.sort_mode.get()=='oldest':
            self._sort_oldest()
        else:
            self.manual_order=True
            self._render()

    def _sort_oldest(self):
        self.manual_order=False
        self.sort_mode.set('oldest')
        self.paths=oldest_first(self.paths,self.file_info)
        self._render()

    def _accept(self,paths):
        suffixes=({'.txt'} if self.kind=='text' else
                  {'.txt','.csv'} if self.kind=='csv' else set(SUPPORTED_EXTENSIONS))
        newly=[]; pending=[(Path(raw).expanduser(),0) for raw in reversed(list(paths))]
        known=set(self.paths); visited=set(); scanned=0; limited=False; errors=[]
        # Keep recursive folder support without following directory cycles forever.
        # Bound filesystem work and thread creation for large or hostile trees.
        limit=self._ui()['MERGE_SCAN_ENTRY_LIMIT']; max_depth=self._ui()['MERGE_SCAN_DEPTH_LIMIT']
        while pending and scanned<limit:
            path,depth=pending.pop(); scanned+=1
            try:
                if path.is_dir():
                    directory=path.resolve()
                    if directory in visited:continue
                    visited.add(directory)
                    if depth>=max_depth:
                        limited=True;continue
                    entries=[]
                    with os.scandir(path) as children:
                        for child in children:
                            if len(entries)>=limit-scanned:
                                limited=True;break
                            entries.append(Path(child.path))
                    pending.extend((child,depth+1) for child in reversed(sorted(entries)))
                elif path.is_file() and path.suffix.lower() in suffixes:
                    resolved=path.resolve()
                    if resolved not in known:
                        known.add(resolved);newly.append(resolved)
            except (OSError,RuntimeError) as exc:
                errors.append(f'{path}: {exc}')
        if pending:limited=True
        if limited or errors:
            details=(f'フォルダーの探索上限（{limit:,}項目または深さ{max_depth}階層）に達しました。残りは分けて追加してください。' if limited else '')
            if errors:
                details+=('\n' if details else '')+f'読み取れなかった項目: {len(errors)}件\n'+errors[0][:300]
            if not messagebox.askyesno('ファイルの追加',details+f'\n\n検出した {len(newly)} 件を追加しますか？',parent=self.win):
                return
        self.paths.extend(newly)
        for path in newly:self._inspect_file_async(path)
        if self.sort_mode.get()=='oldest' and not self.manual_order:
            self.paths=oldest_first(self.paths,self.file_info)
        self._render()

    def _add_dialog(self):
        ft=([('TXT','*.txt')] if self.kind=='text' else
            [('TXT・CSV','*.txt *.csv')] if self.kind=='csv' else self._ui()['MEDIA_TYPES'])
        self._accept(filedialog.askopenfilenames(parent=self.win,title='結合するファイルを選択',filetypes=ft))

    def _drop(self,event):
        try:self._accept(self._ui()['parse_drop_paths'](event.data,self.win.tk.splitlist))
        except (tk.TclError,ValueError,OSError) as exc:self._ui()['show_error'](self.win,str(exc))
        return 'copy'

    def _render_item(self, index, path, key):
        info=self.file_info.get(path)
        values=(f'{index+1:02d}',path.name,
             show_datetime(info.recorded) if info else '確認中…',
             show_datetime(info.file_created) if info else '確認中…',
             show_datetime(info.modified) if info else '確認中…',
             info.sort_source if info else '確認中…')
        self.listbox.insert('', 'end', iid=key, values=values)

    def _render(self):
        self.pages.refresh()
        self._refresh_output()

    def _selected_indices(self):
        return self.pages.selected_indices()

    def _remove(self):
        for index in reversed(self._selected_indices()):self.paths.pop(index)
        self._render()

    def _clear(self):
        self.paths.clear()
        self._render()

    def _move(self,direction):
        selected=self._selected_indices()
        if len(selected)!=1:return
        at=selected[0];target=at+direction
        if not (0<=target<len(self.paths)):return
        self.paths[at],self.paths[target]=self.paths[target],self.paths[at]
        self.manual_order=True
        self.sort_mode.set('added')
        self.pages.page=target//self.pages.PAGE_SIZE
        self._render()
        self.listbox.selection_set(str(target))

    def _browse_output(self):
        directory=filedialog.askdirectory(parent=self.win,title='保存先フォルダーを選択',
                 initialdir=str(self._folder()))
        if directory:
            self.custom_folder.set(directory)
            self.destination_mode.set('custom')
            self._refresh_output()

    def _start(self):
        if len(self.paths)<2:
            self._ui()['show_error'](self.win,'2件以上のファイルを追加してください。');return
        filename=self.filename.get().strip()
        if not filename or Path(filename).name!=filename or '/' in filename or '\\' in filename:
            self._ui()['show_error'](self.win,'出力ファイル名にはフォルダー区切りを含めないでください。');return
        if Path(filename).suffix.lower()!=self._suffix():
            self._ui()['show_error'](self.win,'出力形式と拡張子が一致しません。');return
        destination=self._folder()/filename
        if any(destination.resolve()==p.resolve() for p in self.paths):
            self._ui()['show_error'](self.win,'入力と出力のパスは同じにできません。');return
        if destination.exists():
            self._ui()['show_error'](self.win,'出力先に既存ファイルがあります。名前を変更してください。');return
        if self.kind=='text':
            self.app.start_text_merge(list(self.paths),destination,self.headings.get(),move_originals=self.move_originals.get())
        elif self.kind=='csv':
            self.app.start_csv_merge(list(self.paths),destination,move_originals=self.move_originals.get())
        else:
            self.app.start_audio_merge(list(self.paths),destination,
                fmt=self.fmt.get(),bitrate=self.bitrate.get(),
                channels=1 if self.channels.get().startswith('1') else 2,
                sample_rate=int(self.sample_rate.get()),
                auto_transcribe=self.auto_transcribe.get(),move_originals=self.move_originals.get())
        if self.app.busy:self.win.destroy()


class TranscriptConvertDialog:
    """Batch TXT⇔CSV conversion with per-file collision confirmation."""
    def __init__(self, app:'UnifiedApp'):
        self.app=app
        self.paths:list[Path]=[]
        self.win=self._ui()['modal'](app.root,'文字起こしTXT ⇔ CSV 変換',720)
        self.win.geometry('720x540')
        self.mode=tk.StringVar(self.win,'auto')
        self.destination=tk.StringVar(self.win,'first')
        self.custom_folder=tk.StringVar(self.win,str(default_directory('transcriptions')))
        frame, foot = scrollable_body(self.win, padding=16)
        ttk.Label(frame,text='TXT→CSVはメタデータ・区切り・空行まで保持する拡張CSVです。元ファイルは変更しません。',
                  wraplength=650).pack(anchor='w',pady=(0,8))
        toolbar=ttk.Frame(frame);toolbar.pack(fill='x',pady=5)
        IconButton(toolbar,text='TXT/CSVを追加…',command=self._browse).pack(side='left')
        IconButton(toolbar,text='選択を削除',command=self._remove).pack(side='left',padx=8)
        IconButton(toolbar,text='クリア',command=self._clear).pack(side='left')
        listframe=ttk.Frame(frame);listframe.pack(fill='both',expand=True)
        self.listbox=tk.Listbox(listframe,font=(UI_FONT,10),selectmode='extended',height=6)
        self.listbox.pack(side='left',fill='both',expand=True)
        if self._ui()['DND_FILES'] is not None and hasattr(self.listbox,'drop_target_register'):
            try:
                self.listbox.drop_target_register(self._ui()['DND_FILES'])
                self.listbox.dnd_bind('<<Drop>>',self._drop)
            except (tk.TclError,AttributeError):
                _report_error(self.app, '変換画面のドラッグ＆ドロップ登録に失敗')
        self.pages = FormListPages(self.listbox, frame, lambda:self.paths, self._render_item)
        options=ttk.LabelFrame(frame,text='変換方向',padding=(10,6));options.pack(fill='x',pady=6)
        for val,label in [('auto','拡張子から自動判定（標準）'),('txt2csv','TXT → CSV'),('csv2txt','CSV → TXT')]:
            ttk.Radiobutton(options,text=label,variable=self.mode,value=val).pack(side='left',padx=(0,10))
        folder=ttk.LabelFrame(frame,text='保存先',padding=(10,6));folder.pack(fill='x',pady=6)
        folder_choices = ttk.Frame(folder);folder_choices.pack(fill='x')
        for value,label in [('first','各入力ファイルと同じフォルダー'),('merged','デフォルト'),('custom','任意')]:
            ttk.Radiobutton(folder_choices,text=label,variable=self.destination,value=value).pack(side='left',padx=(0,12))
        destination_buttons(folder,same_command=lambda:self.destination.set('first'),
            default_command=lambda:self.destination.set('merged')).pack(anchor='w',pady=(5,0))
        IconButton(folder,text='任意フォルダーを選ぶ…',command=self._choose_folder).pack(anchor='e',pady=(5,0))
        ttk.Label(frame,text='情報保持CSVは、Excelで行を並べ替えても元の行順を復元します。話者や本文の編集は発話行だけ反映。独自列のデータ欠落は防ぎます。',
                  foreground='#63718a',wraplength=650).pack(anchor='w')
        IconButton(foot,text='閉じる',command=self.win.destroy).pack(side='right')
        IconButton(foot,text='変換を開始',style='Primary.TButton',command=self._start).pack(side='right',padx=(0,8))

    def _accept(self,paths):
        newly=[]; pending=[(Path(raw).expanduser(),0) for raw in reversed(list(paths))]
        known=set(self.paths); visited=set(); scanned=0; limited=False; errors=[]
        limit=self._ui()['MERGE_SCAN_ENTRY_LIMIT']; max_depth=self._ui()['MERGE_SCAN_DEPTH_LIMIT']
        while pending and scanned<limit:
            path,depth=pending.pop(); scanned+=1
            try:
                if path.is_dir():
                    directory=path.resolve()
                    if directory in visited:continue
                    visited.add(directory)
                    if depth>=max_depth:
                        limited=True;continue
                    entries=[]
                    with os.scandir(path) as children:
                        for child in children:
                            if len(entries)>=limit-scanned:
                                limited=True;break
                            entries.append(Path(child.path))
                    pending.extend((child,depth+1) for child in reversed(sorted(entries)))
                elif path.is_file() and path.suffix.lower() in ('.txt','.csv'):
                    resolved=path.resolve()
                    if resolved not in known:
                        known.add(resolved);newly.append(resolved)
            except (OSError,RuntimeError) as exc:
                errors.append(f'{path}: {exc}')
        if pending:limited=True
        if limited or errors:
            details=(f'フォルダーの探索上限（{limit:,}項目または深さ{max_depth}階層）に達しました。残りは分けて追加してください。' if limited else '')
            if errors:
                details+=('\n' if details else '')+f'読み取れなかった項目: {len(errors)}件\n'+errors[0][:300]
            if not messagebox.askyesno('ファイルの追加',details+f'\n\n検出した {len(newly)} 件を追加しますか？',parent=self.win):
                return
        self.paths.extend(newly)
        self._render()

    def _render(self):
        self.pages.refresh()

    def _render_item(self, _index, path, _key):
        self.listbox.insert('end',str(path))

    def _browse(self):
        self._accept(filedialog.askopenfilenames(parent=self.win,title='変換対象を選択',
                      filetypes=[('TXT・CSV','*.txt *.csv')]))

    def _drop(self,event):
        try:self._accept(self._ui()['parse_drop_paths'](event.data,self.win.tk.splitlist))
        except Exception as exc:
            _report_error(self.app, '変換対象のドラッグ＆ドロップに失敗')
            self._ui()['show_error'](self.win,str(exc))
        return 'copy'

    def _remove(self):
        for i in reversed(self.pages.selected_indices()):self.paths.pop(i)
        self._render()

    def _clear(self):
        self.paths.clear();self._render()

    def _choose_folder(self):
        folder=filedialog.askdirectory(parent=self.win,initialdir=self.custom_folder.get())
        if folder:self.custom_folder.set(folder);self.destination.set('custom')

    def _start(self):
        if not self.paths:
            self._ui()['show_error'](self.win,'変換対象のTXT・CSVを追加してください。');return
        direction=self.mode.get()
        if direction=='txt2csv' and any(p.suffix.lower()!='.txt' for p in self.paths):
            self._ui()['show_error'](self.win,'TXT→CSVではTXTファイルのみ選択してください。');return
        if direction=='csv2txt' and any(p.suffix.lower()!='.csv' for p in self.paths):
            self._ui()['show_error'](self.win,'CSV→TXTではCSVファイルのみ選択してください。');return
        jobs=[]
        for source in self.paths:
            ext='.csv' if source.suffix.lower()=='.txt' else '.txt'
            folder=source.parent if self.destination.get()=='first' else (
                default_directory('transcriptions') if self.destination.get()=='merged' else Path(self.custom_folder.get()))
            jobs.append((source,folder/source.with_suffix(ext).name))
        self.app.start_transcript_conversion(jobs)
        if self.app.busy:self.win.destroy()


