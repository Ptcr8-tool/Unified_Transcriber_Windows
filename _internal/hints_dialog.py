"""Local recognition hints editor. It never modifies transcription output."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from core import default_directory, save_config
from term_hints import (COMMON_GROUP, MAX_ENTRIES, MAX_GROUPS, hint_summary,
                        read_dictionary, validate_hint_settings, write_dictionary)
from ui_controls import ToggleSwitch
from ui_icons import IconButton


class HintsDialog:
    def __init__(self, app):
        from unified_gui import modal
        self.app = app
        self.groups = deepcopy(app.config.hint_groups)
        self.selected = set(app.config.hint_selected_groups)
        self.current_group = COMMON_GROUP
        self.word_form_index = None
        self.win = modal(app.root, '認識用の単語辞書', 880)
        self.win.geometry('880x690')
        self.enabled = tk.BooleanVar(self.win, app.config.hint_enabled)
        self.term = tk.StringVar(self.win)
        self.reading = tk.StringVar(self.win)
        self.summary = tk.StringVar(self.win)
        self.group_label = tk.StringVar(self.win)

        outer = ttk.Frame(self.win, padding=16)
        outer.pack(fill='both', expand=True)
        # Allocate the action bar before the expanding lists so it stays visible
        # when Windows scaling or a small screen makes the dialog shorter.
        footer = ttk.Frame(outer)
        footer.pack(fill='x', side='bottom', pady=(5, 0))
        IconButton(footer, text='辞書を読み込む…', icon='import', command=self._import).pack(side='left')
        IconButton(footer, text='辞書を書き出す…', icon='archive', command=self._export).pack(side='left', padx=(6, 0))
        IconButton(footer, text='キャンセル', command=self.win.destroy).pack(side='right', padx=(6, 0))
        IconButton(footer, text='辞書を保存', style='Primary.TButton', command=self._save).pack(side='right')
        ttk.Label(outer, textvariable=self.summary, style='Small.TLabel', wraplength=820).pack(
            side='bottom', anchor='w', pady=(8, 4))
        ttk.Label(outer, text='認識用の単語辞書', style='Section.TLabel').pack(anchor='w')
        ttk.Label(outer, text='人名・団体名などを認識時のヒントとして渡します。正しい認識を保証するものではありません。\n'
                  '文章の置換は行いません。読み方は辞書の管理用で、認識時には使用しません。',
                  style='Muted.TLabel', wraplength=820).pack(anchor='w', pady=(5, 6))
        ToggleSwitch(outer, text='単語辞書を使用する', variable=self.enabled,
                     command=self._refresh_summary).pack(anchor='w', pady=(0, 8))
        content = ttk.Frame(outer)
        content.pack(fill='both', expand=True)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(1, weight=1)
        ttk.Label(content, text='グループ', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(content, textvariable=self.group_label, style='Section.TLabel').grid(row=0, column=1, sticky='w', padx=(12, 0))

        left = ttk.Frame(content)
        left.grid(row=1, column=0, sticky='nsew', pady=(5, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.group_tree = ttk.Treeview(left, columns=('group', 'use'), show='headings',
                                       selectmode='browse', height=9)
        self.group_tree.heading('group', text='名前')
        self.group_tree.heading('use', text='使用')
        self.group_tree.column('group', width=132, minwidth=80)
        self.group_tree.column('use', width=50, minwidth=42, stretch=False, anchor='center')
        self.group_tree.grid(row=0, column=0, sticky='nsew')
        group_scroll = ttk.Scrollbar(left, orient='vertical', command=self.group_tree.yview)
        group_scroll.grid(row=0, column=1, sticky='ns')
        self.group_tree.configure(yscrollcommand=group_scroll.set)
        self.group_tree.bind('<<TreeviewSelect>>', self._change_group)
        group_buttons = ttk.Frame(left)
        group_buttons.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(7, 0))
        IconButton(group_buttons, text='追加', icon='file-plus', command=self._add_group).pack(side='left')
        IconButton(group_buttons, text='削除', icon='trash', command=self._delete_group).pack(side='left', padx=(4, 0))
        IconButton(left, text='使用を切り替え', icon='select-all', command=self._toggle_group).grid(
            row=2, column=0, columnspan=2, sticky='ew', pady=(5, 0))
        ttk.Label(left, text='「共通」は常に使用', style='Small.TLabel').grid(row=3, column=0, sticky='w', pady=(4, 0))

        right = ttk.Frame(content)
        right.grid(row=1, column=1, sticky='nsew', padx=(12, 0), pady=(5, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.word_tree = ttk.Treeview(right, columns=('term', 'reading'), show='headings',
                                      selectmode='browse', height=9)
        self.word_tree.heading('term', text='正しい表記')
        self.word_tree.heading('reading', text='読み方（任意）')
        self.word_tree.column('term', width=230, minwidth=100)
        self.word_tree.column('reading', width=165, minwidth=80)
        self.word_tree.grid(row=0, column=0, sticky='nsew')
        word_scroll = ttk.Scrollbar(right, orient='vertical', command=self.word_tree.yview)
        word_scroll.grid(row=0, column=1, sticky='ns')
        self.word_tree.configure(yscrollcommand=word_scroll.set)
        self.word_tree.bind('<<TreeviewSelect>>', self._select_word)
        fields = ttk.Frame(right)
        fields.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text='表記').grid(row=0, column=0, sticky='w', padx=(0, 6))
        ttk.Entry(fields, textvariable=self.term).grid(row=0, column=1, sticky='ew')
        ttk.Label(fields, text='読み').grid(row=1, column=0, sticky='w', padx=(0, 6), pady=(5, 0))
        ttk.Entry(fields, textvariable=self.reading).grid(row=1, column=1, sticky='ew', pady=(5, 0))
        word_buttons = ttk.Frame(right)
        word_buttons.grid(row=2, column=0, columnspan=2, sticky='w', pady=(7, 0))
        IconButton(word_buttons, text='単語を追加', icon='file-plus', command=self._add_word).pack(side='left')
        IconButton(word_buttons, text='選択を更新', icon='edit', command=self._update_word).pack(side='left', padx=(4, 0))
        IconButton(word_buttons, text='選択を削除', icon='trash', command=self._delete_word).pack(side='left', padx=(4, 0))
        ttk.Label(right, text='入力後「単語を追加」または「選択を更新」で一覧に反映します。',
                  style='Small.TLabel').grid(row=3, column=0, sticky='w', pady=(5, 0))

        self._refresh_groups()
        self._refresh_words()

    def _config(self):
        # Start from the current config so unrelated preferences are not overwritten.
        selected = [name for name in self.groups if name != COMMON_GROUP and name in self.selected]
        cfg = replace(self.app.config, hint_enabled=bool(self.enabled.get()),
                      hint_groups=deepcopy(self.groups), hint_selected_groups=selected)
        cfg.validate()
        return cfg

    def _refresh_summary(self):
        try:
            info = hint_summary(self._config())
            message = f"認識に渡す単語: {info['included']} 件"
            if info['omitted']:
                message += f"（長さの上限により {info['omitted']} 件は省略）"
            if not self.enabled.get():
                message += ' · 辞書は現在オフです'
            self.summary.set(message + '。長い辞書はモデル側でも一部が省略される場合があります。')
        except (ValueError, TypeError) as exc:
            self.summary.set(str(exc))

    def _refresh_groups(self):
        self.group_tree.delete(*self.group_tree.get_children())
        index = None
        for n, name in enumerate(self.groups):
            used = name == COMMON_GROUP or name in self.selected
            iid = self.group_tree.insert('', 'end', values=(name, 'ON' if used else 'OFF'))
            if name == self.current_group:
                index = iid
        if index is not None:
            self.group_tree.selection_set(index)
            self.group_tree.focus(index)
        self._refresh_summary()

    def _refresh_words(self):
        self.group_label.set(f'{self.current_group} の単語')
        self.word_tree.delete(*self.word_tree.get_children())
        for i, entry in enumerate(self.groups[self.current_group]):
            self.word_tree.insert('', 'end', iid=str(i), values=(entry['term'], entry['reading']))
        self.term.set('')
        self.reading.set('')
        self.word_form_index = None
        self._refresh_summary()

    def _form_pending(self):
        form = {'term': self.term.get().strip(), 'reading': self.reading.get().strip()}
        if not (form['term'] or form['reading']):
            return False
        entries = self.groups[self.current_group]
        index = self.word_form_index
        return index is None or index >= len(entries) or form != entries[index]

    def _confirm_discard_form(self):
        return not self._form_pending() or messagebox.askyesno(
            '入力内容の確認', '入力欄の内容は一覧に反映されていません。\n'
            '入力内容を破棄して移動しますか？', parent=self.win)

    def _change_group(self, _event=None):
        selected = self.group_tree.selection()
        if not selected:
            return
        name = self.group_tree.item(selected[0], 'values')[0]
        if name in self.groups and name != self.current_group:
            if not self._confirm_discard_form():
                self._refresh_groups()
                return
            self.current_group = name
            self._refresh_words()

    def _select_word(self, _event=None):
        selected = self.word_tree.selection()
        if selected:
            index = int(selected[0])
            if index != self.word_form_index and not self._confirm_discard_form():
                if self.word_form_index is None:
                    self.word_tree.selection_remove(*selected)
                else:
                    self.word_tree.selection_set(str(self.word_form_index))
                return
            term, reading = self.word_tree.item(selected[0], 'values')
            self.term.set(term)
            self.reading.set(reading)
            self.word_form_index = index

    def _add_group(self):
        if not self._confirm_discard_form():
            return
        name = simpledialog.askstring('グループを追加', 'グループ名を入力してください。', parent=self.win)
        if name is None:
            return
        name = name.strip()
        if name in self.groups:
            messagebox.showerror('グループを追加', '同じ名前のグループが既にあります。', parent=self.win)
            return
        if len(self.groups) >= MAX_GROUPS:
            messagebox.showerror('グループを追加', f'グループは最大 {MAX_GROUPS} 件です。', parent=self.win)
            return
        candidate = dict(self.groups)
        candidate[name] = []
        try:
            validate_hint_settings(bool(self.enabled.get()), candidate, list(self.selected))
        except (ValueError, TypeError) as exc:
            messagebox.showerror('グループを追加', str(exc), parent=self.win)
            return
        self.groups = candidate
        self.current_group = name
        self.selected.add(name)
        self._refresh_groups()
        self._refresh_words()

    def _delete_group(self):
        name = self.current_group
        if name == COMMON_GROUP:
            messagebox.showinfo('グループを削除', '共通グループは削除できません。', parent=self.win)
            return
        if not messagebox.askyesno('グループを削除', f'「{name}」と登録済みの単語を削除しますか？', parent=self.win):
            return
        del self.groups[name]
        self.selected.discard(name)
        self.current_group = COMMON_GROUP
        self._refresh_groups()
        self._refresh_words()

    def _toggle_group(self):
        name = self.current_group
        if name == COMMON_GROUP:
            messagebox.showinfo('共通グループ', '共通グループは辞書が有効なとき常に使用されます。', parent=self.win)
            return
        if name in self.selected:
            self.selected.remove(name)
        else:
            self.selected.add(name)
        self._refresh_groups()

    def _word_values(self):
        term, reading = self.term.get().strip(), self.reading.get().strip()
        candidate = {'term': term, 'reading': reading}
        validate_hint_settings(False, {COMMON_GROUP: [candidate]}, [])
        return candidate

    def _selected_word_index(self):
        selected = self.word_tree.selection()
        return int(selected[0]) if selected else None

    def _add_word(self):
        try:
            entry = self._word_values()
            if sum(len(v) for v in self.groups.values()) >= MAX_ENTRIES:
                raise ValueError(f'単語は最大 {MAX_ENTRIES} 件です。')
            if any(item['term'].casefold() == entry['term'].casefold() for item in self.groups[self.current_group]):
                raise ValueError('同じ単語がこのグループに登録されています。')
            self.groups[self.current_group].append(entry)
            self._refresh_words()
        except (ValueError, TypeError) as exc:
            messagebox.showerror('単語を追加', str(exc), parent=self.win)

    def _update_word(self):
        index = self._selected_word_index()
        if index is None:
            messagebox.showinfo('単語を更新', '一覧から更新する単語を選択してください。', parent=self.win)
            return
        try:
            entry = self._word_values()
            entries = self.groups[self.current_group]
            if any(i != index and item['term'].casefold() == entry['term'].casefold()
                   for i, item in enumerate(entries)):
                raise ValueError('同じ単語がこのグループに登録されています。')
            entries[index] = entry
            self._refresh_words()
        except (ValueError, TypeError) as exc:
            messagebox.showerror('単語を更新', str(exc), parent=self.win)

    def _delete_word(self):
        index = self._selected_word_index()
        if index is None:
            messagebox.showinfo('単語を削除', '一覧から削除する単語を選択してください。', parent=self.win)
            return
        del self.groups[self.current_group][index]
        self._refresh_words()

    def _import(self):
        source = filedialog.askopenfilename(parent=self.win, title='辞書を読み込む',
            initialdir=str(default_directory('exports')), filetypes=[('辞書ファイル', '*.json')])
        if not source:
            return
        try:
            data = read_dictionary(source)
            if not messagebox.askyesno('辞書を読み込む',
                    '現在編集中の辞書を、読み込んだ辞書に置き換えますか？\n'
                    '「辞書を保存」を押すまでは設定に反映されません。', parent=self.win):
                return
            self.groups = data['hint_groups']
            self.selected = set(data['hint_selected_groups'])
            self.enabled.set(data['hint_enabled'])
            self.current_group = COMMON_GROUP
            self._refresh_groups()
            self._refresh_words()
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror('辞書を読み込めません', str(exc), parent=self.win)

    def _export(self):
        if self._form_pending():
            messagebox.showinfo('辞書を書き出す',
                '入力欄の内容は一覧に反映されていません。\n'
                '「単語を追加」または「選択を更新」してから書き出してください。', parent=self.win)
            return
        target = filedialog.asksaveasfilename(parent=self.win, title='辞書を書き出す',
            initialdir=str(default_directory('exports')), initialfile='recognition-dictionary.json',
            defaultextension='.json', filetypes=[('辞書ファイル', '*.json')])
        if not target:
            return
        try:
            write_dictionary(Path(target), self._config())
            messagebox.showinfo('書き出し完了', f'辞書を書き出しました。\n{target}', parent=self.win)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror('辞書を書き出せません', str(exc), parent=self.win)

    def _save(self):
        if self.app.busy:
            messagebox.showinfo('辞書を保存', '処理が終了してから辞書を保存してください。', parent=self.win)
            return
        index = self._selected_word_index()
        current = self.groups[self.current_group][index] if index is not None else None
        form = {'term': self.term.get().strip(), 'reading': self.reading.get().strip()}
        if (form['term'] or form['reading']) and form != current:
            if not messagebox.askyesno('入力内容の確認',
                    '入力欄の内容は一覧に反映されていません。\nこの内容を追加せずに保存しますか？', parent=self.win):
                return
        try:
            cfg = self._config()
            save_config(cfg)
            self.app.config = cfg
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror('辞書を保存できません', str(exc), parent=self.win)
            return
        self.app.status.set('認識用の単語辞書を保存しました。')
        self.win.destroy()
