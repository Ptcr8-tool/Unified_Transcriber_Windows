"""Original SVG-derived Tk icons; recolor PNG palettes using only the stdlib."""
from functools import lru_cache
from pathlib import Path
import struct
import tkinter as tk
from tkinter import ttk
import zlib

ASSETS=Path(__file__).resolve().parent/'assets'/'icons'
SIZES=(18,20,24,28,36,48)
ICON_NAMES=frozenset(('mic video tools sliders file-plus folder-plus folder folder-open folder-same '
    'play stop copy merge waveform table text convert users download cpu gauge keyboard cursor logs panels '
    'archive help trash select-all columns edit close refresh monitor clock save arrow-up arrow-down reset '
    'link unlink key import message expand collapse skip sun moon').split())
ACTION_ICONS={
    'live':'mic','gamebar':'video','import':'import','capture_setup':'download','cancel_live_asr':'stop',
    'text_merge':'merge','audio_merge':'waveform','csv_merge':'table','convert':'convert','speaker':'users',
    'bulk':'download','setup':'link','cuda':'cpu','limits':'gauge','shortcuts':'keyboard','dnd':'cursor',
    'logs':'folder-open','detached_log':'panels','export_logs':'archive','help':'help',
    'add_folder':'folder-plus','watch':'clock','file_output':'sliders','file_policy':'save',
    'remove':'trash','select_all':'select-all','clear':'trash','result':'text','open_source':'folder',
    'open_result':'folder-open','result_speaker':'users',
    'settings_export':'archive','settings_import':'import',
}


def clean_label(text):
    """Replace the previous font glyph prefix with an image, retaining the label."""
    return str(text).lstrip('●■▶▣＋↑↓▼ ').strip()


def icon_for_label(text):
    original=str(text);label=clean_label(original)
    if original.startswith('↑'):return 'arrow-up'
    if original.startswith('↓'):return 'arrow-down'
    rules=(
        (('発話を',),'message'),(('すべて開く',),'expand'),(('すべて閉じる',),'collapse'),
        (('入力と同じ場所',),'folder-same'),(('コピー',),'copy'),
        (('停止','認識を中断','録音を終了','自動表示を終了'),'stop'),
        (('ショートカット','キー入力','このキー'),'keyboard'),
        (('環境変数','HF_TOKEN'),'key'),(('セットアップ',),'download'),
        (('割り当て解除',),'unlink'),(('割り当て',),'link'),
        (('保存','適用','上書き'),'save'),(('キャンセル','閉じる','解除'),'close'),
        (('スキップ',),'skip'),(('リネーム',),'edit'),
        (('削除','クリア'),'trash'),(('更新','並べ直す'),'refresh'),
        (('自動名に戻す',),'reset'),(('テスト','解析','診断'),'cpu'),
        (('画面','プレビュー'),'monitor'),(('監視',),'clock'),
        (('参照','場所を選ぶ','フォルダーを選ぶ'),'folder-open'),
        (('取り込み',),'import'),(('追加',),'file-plus'),(('結合',),'merge'),
        (('変換',),'convert'),(('話者',),'users'),(('ログ',),'logs'),
        (('フォルダーを開く','保存先を開く','出力先を開く','元ファイルの場所'),'folder-open'),
        (('Game Bar','録画'),'video'),(('リアルタイム','録音'),'mic'),
        (('開始',),'play'),(('後で',),'clock'),(('設定',),'sliders'),
    )
    return next((name for words,name in rules if any(word in label for word in words)),'sliders')


@lru_cache(maxsize=300)
def _mask(name,size):
    return (ASSETS/'png'/f'{name}-{size}.png').read_bytes()


def tint_png(data,color):
    """Change only PLTE. The 256-level tRNS alpha and compressed pixels stay intact."""
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid icon PNG')
    rgb=bytes.fromhex(color.lstrip('#'))
    if len(rgb)!=3:raise ValueError('Expected an RGB color')
    offset=8
    while offset+12<=len(data):
        length=struct.unpack('>I',data[offset:offset+4])[0]
        kind=data[offset+4:offset+8]
        end=offset+12+length
        if kind==b'PLTE':
            palette=rgb*(length//3)
            chunk=kind+palette
            return data[:offset]+struct.pack('>I',len(palette))+chunk+struct.pack('>I',zlib.crc32(chunk))+data[end:]
        offset=end
    raise ValueError('Icon PNG has no palette')


def set_icon_palette(widget,colors):
    widget._root()._studio_icon_colors=dict(colors)


def icon_image(widget,name,role='ink',size=18):
    root=widget._root()
    colors=getattr(root,'_studio_icon_colors',{})
    color='#ffffff' if role=='white' else colors.get(role,'#17283f')
    scale=max(1,float(root.tk.call('tk','scaling'))/1.333333)
    pixels=min(SIZES,key=lambda value:abs(value-size*scale))
    if name not in ICON_NAMES:name='sliders'
    key=(name,color,pixels)
    cache=getattr(root,'_studio_action_icons',None)
    if cache is None:cache=root._studio_action_icons={}
    if key not in cache:
        try:image=tk.PhotoImage(master=root,data=tint_png(_mask(name,pixels),color))
        except (OSError,ValueError,tk.TclError):
            # A partial/manual update must not make the labelled actions unusable.
            image=tk.PhotoImage(master=root,width=pixels,height=pixels)
        cache[key]=image
    return cache[key]


def attach_icon(widget,name,*,role=None,icon_only=False):
    widget._studio_icon_name=name
    widget._studio_icon_role=role
    widget._studio_icon_only=icon_only
    refresh_icon(widget)


def refresh_icon(widget):
    name=getattr(widget,'_studio_icon_name',None)
    if not name:return
    style=str(widget.cget('style'))
    role=getattr(widget,'_studio_icon_role',None) or (
        'white' if style=='Primary.TButton' else 'red' if style=='Stop.TButton' else
        'muted' if style=='Quiet.TButton' else 'ink')
    normal=icon_image(widget,name,role)
    disabled=icon_image(widget,name,'disabled_ink')
    selected=icon_image(widget,name,'blue' if style in ('Category.TButton','Theme.TRadiobutton') else role)
    widget._studio_icon_images=(normal,disabled,selected)
    # Bypass the subclass's text/style hook while refreshing image data.
    ttk.Widget.configure(widget,image=(normal,'disabled',disabled,'selected',selected),
                         compound='image' if getattr(widget,'_studio_icon_only',False) else 'left')


class _IconControl:
    def __init__(self,master=None,*,icon=None,icon_role=None,**kwargs):
        self._explicit_icon=icon
        original=kwargs.get('text','')
        if 'text' in kwargs:kwargs['text']=clean_label(original)
        super().__init__(master,**kwargs)
        attach_icon(self,icon or icon_for_label(original),role=icon_role)

    def configure(self,cnf=None,**kwargs):
        if isinstance(cnf,str) or (cnf is None and not kwargs):return super().configure(cnf)
        options=dict(cnf or {});options.update(kwargs)
        refresh='text' in options or 'style' in options or 'icon' in options
        if 'icon' in options:self._explicit_icon=options.pop('icon')
        if 'text' in options:
            text=options['text'];options['text']=clean_label(text)
            self._studio_icon_name=self._explicit_icon or icon_for_label(text)
        elif self._explicit_icon:self._studio_icon_name=self._explicit_icon
        result=super().configure(**options)
        if refresh:refresh_icon(self)
        return result

    config=configure


class IconButton(_IconControl,ttk.Button):
    """A regular ttk button with the same callbacks, focus and disabled behavior."""


class IconMenubutton(_IconControl,ttk.Menubutton):
    pass


class IconMenu(tk.Menu):
    """Keep backing-menu commands/state intact and give category entries icons too."""
    def __init__(self,*args,**kwargs):
        self._entry_icons={}
        super().__init__(*args,**kwargs)

    def add(self,itemType,cnf=None,**kwargs):
        options=dict(cnf or {});options.update(kwargs)
        name=options.pop('icon',None)
        if itemType!='separator':
            label=options.get('label','')
            name=name or icon_for_label(label)
            options['label']=clean_label(label)
            options.update(image=icon_image(self,name,'disabled_ink' if options.get('state')=='disabled' else 'ink'),compound='left')
        super().add(itemType,**options)
        if name:self._entry_icons[self.index('end')]=name

    def entryconfigure(self,index,cnf=None,**kwargs):
        if isinstance(cnf,str) or (cnf is None and not kwargs):return super().entryconfigure(index,cnf)
        options=dict(cnf or {});options.update(kwargs)
        identity=self.index(index)
        name=options.pop('icon',None) or self._entry_icons.get(identity)
        if 'label' in options:options['label']=clean_label(options['label'])
        if name:
            self._entry_icons[identity]=name
            state=options.get('state',self.entrycget(index,'state'))
            options['image']=icon_image(self,name,'disabled_ink' if state=='disabled' else 'ink')
        return super().entryconfigure(index,**options)

    entryconfig=entryconfigure

    def refresh_icons(self):
        for index in self._entry_icons:self.entryconfigure(index,state=self.entrycget(index,'state'))

    def icon_name(self,index):return self._entry_icons.get(index,'sliders')
