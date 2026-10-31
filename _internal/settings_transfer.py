"""Versioned settings exchange with app/home-relative, cross-Windows-user paths."""
from copy import deepcopy
from dataclasses import asdict,fields,replace
import json
import ntpath
import os
from pathlib import Path,PurePosixPath,PureWindowsPath
import posixpath
import tempfile

from core import BASE,Config

FORMAT='unified-transcriber-settings'
PATH_FIELDS=('output_root','output_dir','live_recording_dir','gamebar_captures_dir')
MODEL_FIELDS=('model_openai','model_faster','qwen_model','qwen_aligner')
CAPTURE_PATHS={'live':('folder',),'gamebar':('destination','captures')}


def _flavor(value):return PureWindowsPath if ntpath.splitdrive(str(value))[0] or '\\' in str(value) else PurePosixPath


def _normalized(value):
    value=str(value)
    factory=_flavor(value)
    return factory(ntpath.normpath(value) if factory is PureWindowsPath else posixpath.normpath(value))


def encode_path(value,app_root,user_home):
    if not value:return ''
    raw=str(value)
    if raw=='~' or raw.startswith(('~/','~\\')):raw=str(_flavor(user_home)(user_home,raw[2:] if len(raw)>1 else ''))
    path=_normalized(raw)
    if not path.is_absolute():path=_normalized(_flavor(app_root)(app_root,raw))
    for base,root in (('app',app_root),('home',user_home)):
        try:relative=path.relative_to(_normalized(root))
        except ValueError:continue
        return {'base':base,'path':relative.as_posix()}
    return {'base':'absolute','path':str(path)}


def decode_path(value,app_root,user_home,source):
    if not value:return ''
    if isinstance(value,str):
        # Old settings.json and early exports can contain ordinary absolute paths.
        old_app=source.get('app_root')
        old_home=source.get('user_home')
        if old_app and old_home:
            value=encode_path(value,old_app,old_home)
        else:
            parts=_normalized(value).parts
            marker=next((i for i,part in enumerate(parts) if part.casefold()=='unified_transcriber_windows'),None)
            if marker is not None:value={'base':'app','path':PurePosixPath(*parts[marker+1:]).as_posix()}
            elif _flavor(value) is PureWindowsPath and len(parts)>3 and parts[1].casefold()=='users' and parts[2].casefold() not in ('public','default'):
                value={'base':'home','path':PurePosixPath(*parts[3:]).as_posix()}
            else:return str(value)
    if not isinstance(value,dict) or set(value)!= {'base','path'} or not isinstance(value['path'],str):
        raise ValueError('保存先の形式を確認してください。')
    base=value['base'];raw=value['path']
    if base=='absolute':
        # Explicit absolute paths may point to user-selected locations outside
        # app/home, but must not smuggle a relative path or an invalid string.
        if '\0' in raw or len(raw)>32768 or not _normalized(raw).is_absolute():
            raise ValueError('絶対パスの形式を確認してください。')
        return raw
    if base not in ('app','home'):raise ValueError('保存先の基準フォルダーを確認してください。')
    relative=PurePosixPath(raw.replace('\\','/'))
    if relative.is_absolute() or '..' in relative.parts or any(':' in part for part in relative.parts):
        raise ValueError('保存先に不正な相対パスが含まれています。')
    root=app_root if base=='app' else user_home
    return str(_flavor(root)(root,*relative.parts))


def _transform_paths(settings,transform):
    for key in PATH_FIELDS:
        if key in settings:settings[key]=transform(settings[key])
    for key in MODEL_FIELDS:
        if key in settings and (isinstance(settings[key],dict) or
                (isinstance(settings[key],str) and _normalized(settings[key]).is_absolute())):
            settings[key]=transform(settings[key])
    if 'python_overrides' in settings:
        if not isinstance(settings['python_overrides'],dict):raise ValueError('Python環境の設定を確認してください。')
        settings['python_overrides']={key:transform(value) for key,value in settings['python_overrides'].items()}
    captures=settings.get('capture_preferences',{})
    if not isinstance(captures,dict):raise ValueError('録音・録画設定を確認してください。')
    for kind,options in captures.items():
        if kind not in CAPTURE_PATHS or not isinstance(options,dict):raise ValueError('録音・録画設定を確認してください。')
        for key in CAPTURE_PATHS[kind]:
            if key in options:options[key]=transform(options[key])
        if kind=='gamebar':
            options.pop('target_hwnd',None);options.pop('target_title',None)


def export_settings(config,*,app_root=None,user_home=None,include_token=False,token=''):
    config.validate()
    app_root=str(app_root or BASE);user_home=str(user_home or Path.home())
    values=asdict(config)
    values.pop('hf_token_json',None)
    # These describe the local machine's installation, rather than user preferences.
    values.pop('capture_setup_complete',None);values.pop('onboarding_seen',None)
    _transform_paths(values,lambda value:encode_path(value,app_root,user_home))
    if include_token and (token or config.hf_token_json):values['hf_token_json']=token or config.hf_token_json
    return {'format':FORMAT,'version':1,'source':{'app_root':app_root,'user_home':user_home},'settings':values}


def import_settings(data,current=None,*,app_root=None,user_home=None,path_exists=None):
    if not isinstance(data,dict):raise ValueError('設定ファイルはJSON形式で指定してください。')
    if 'format' in data:
        if data['format']!=FORMAT or data.get('version')!=1:raise ValueError('この設定ファイルの形式・バージョンには対応していません。')
        values=deepcopy(data.get('settings'));source=data.get('source',{})
    else:values=deepcopy(data);source={}
    if not isinstance(values,dict) or not isinstance(source,dict):raise ValueError('設定の内容を確認してください。')
    if any(not isinstance(value,str) for value in source.values()):raise ValueError('移行元フォルダーの設定を確認してください。')
    app_root=str(app_root or BASE);user_home=str(user_home or Path.home())
    defaults=Config();current=current or defaults
    accepted={field.name for field in fields(Config)}
    values={key:value for key,value in values.items() if key in accepted}
    _transform_paths(values,lambda value:decode_path(value,app_root,user_home,source))
    values.pop('capture_setup_complete',None);values.pop('onboarding_seen',None)
    for key,value in values.items():
        default=getattr(defaults,key)
        valid=(isinstance(value,int) and not isinstance(value,bool)) if key in ('cpu_limit_percent','gpu_soft_percent') and value is not None else (
            isinstance(value,(int,float)) and not isinstance(value,bool) if isinstance(default,float) else
            isinstance(value,int) and not isinstance(value,bool) if isinstance(default,int) and not isinstance(default,bool) else
            isinstance(value,type(default)) if default is not None else value is None)
        if not valid:raise ValueError(f'設定項目「{key}」の値を確認してください。')
    exists=path_exists or (lambda value:Path(value).is_file())
    missing=[]
    if 'python_overrides' in values:
        for key,value in list(values['python_overrides'].items()):
            if not exists(value):missing.append(key);del values['python_overrides'][key]
    for kind,options in values.get('capture_preferences',{}).items():
        from gamebar_recording import GameBarOptions
        from live_config import LiveOptions
        default_options=LiveOptions() if kind=='live' else GameBarOptions()
        for key in list(options):
            if not hasattr(default_options,key):del options[key];continue
            expected=getattr(default_options,key);value=options[key]
            valid=(isinstance(value,str) if isinstance(expected,Path) else
                isinstance(value,(int,float)) and not isinstance(value,bool) if isinstance(expected,float) else
                isinstance(value,int) and not isinstance(value,bool) if isinstance(expected,int) and not isinstance(expected,bool) else
                isinstance(value,type(expected)))
            if not valid:raise ValueError('録音・録画の設定値を確認してください。')
        for key in ('device_id','mic_device_id','loopback_device_id'):
            if key in options:options[key]=''
    cfg=replace(current,**values)
    cfg.validate()
    from capture_preferences import saved_options
    for kind in cfg.capture_preferences:
        if saved_options(cfg,kind) is None:raise ValueError('録音・録画の設定値を確認してください。')
    return cfg,missing


def write_export(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',newline='\n',dir=path.parent,
                prefix='.'+path.name+'.',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name)
            json.dump(data,stream,ensure_ascii=False,indent=2)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
