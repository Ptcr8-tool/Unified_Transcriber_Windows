"""JSON-safe recording preferences; no credentials or device libraries."""
from dataclasses import asdict, fields
from pathlib import Path

from gamebar_recording import GameBarOptions
from live_config import LiveOptions
from output_locations import (APP_ROOT, default_directory, default_output_root,
                              validate_output_root)


def recording_folder_preference(folder):
    """Keep defaults dynamic when the common output folder changes later."""
    path = Path(folder).expanduser()
    if path in (default_directory('recordings'), APP_ROOT / 'Recordings'):
        return ''
    return str(path)


def _configured_recordings(config):
    if hasattr(config, 'output_root'):
        selected = validate_output_root(config.output_root)
        return (Path(selected) if selected else default_output_root()) / 'Recordings'
    return default_directory('recordings')


def serialize_options(options):
    values={key:str(value) if isinstance(value,Path) else value
            for key,value in asdict(options).items()}
    if isinstance(options,GameBarOptions):
        values.pop('target_hwnd',None);values.pop('target_title',None)
    key = 'destination' if isinstance(options, GameBarOptions) else 'folder'
    if key in values and not recording_folder_preference(values[key]):
        values.pop(key)
    return values


def saved_options(config, kind):
    factory = LiveOptions if kind == 'live' else GameBarOptions
    data = config.capture_preferences.get(kind)
    if not isinstance(data,dict):
        return None
    accepted = {field.name for field in fields(factory)}
    values = {key:value for key,value in data.items() if key in accepted}
    if kind=='gamebar':
        values.pop('target_hwnd',None);values.pop('target_title',None)
    try:
        key = 'folder' if kind == 'live' else 'destination'
        # Older versions saved the app-root default as an absolute override.
        # Rebind only that known default; arbitrary custom folders stay fixed.
        if key not in values or Path(values[key]).expanduser() == APP_ROOT / 'Recordings':
            values[key] = _configured_recordings(config)
        for key in ('folder','captures','destination'):
            if key in values:
                values[key] = Path(values[key])
        options = factory(**values)
        options.validate()
        return options
    except (ValueError,TypeError,OSError):
        return None


def match_window(options, windows):
    """Require the saved title as well as HWND: handles can be reused after exit."""
    matches = [(title,hwnd) for title,hwnd in windows if title == options.target_title]
    exact = next((hwnd for _title,hwnd in matches if hwnd == options.target_hwnd), None)
    return exact or (matches[0][1] if len(matches) == 1 else None)
