"""Retire successful on-screen results at an explicit close/reopen boundary.

The application owns one live GUI instance. Closing to the notification area is
not a process exit, so a repeat launch restores that same workspace. Only its
successful rows and committed preview may be cleared. This module never writes,
unlinks, moves or opens a source/output file; recovery files and logs are intact.
All functions run on the Tk owner thread.
"""
from __future__ import annotations

from pathlib import Path

SUCCESS = frozenset(('完了', '録音済み', '録画済み', 'ライブ完了'))


def _state(app):
    state = getattr(app, '_workspace_cleanup_state', None)
    if state is None:
        state = {'preview_ok': False, 'preview_key': None, 'capture_key': None,
                 'warning': False, 'protected': set()}
        app._workspace_cleanup_state = state
    return state


def begin_work(app):
    state = _state(app)
    state['capture_key'] = None
    state['warning'] = False


def reset_preview(app):
    state = _state(app)
    state['preview_ok'] = False
    state['preview_key'] = None


def observe_event(app, event):
    """Track committed output, including failures after a successful ASR save.

    A codec conversion may fail after the row has become 完了. Protect that
    capture instead of interpreting an earlier successful ASR as full success.
    Unknown/uncommitted previews are deliberately preserved.
    """
    kind = event[0]
    if kind not in ('new-file', 'engine-progress', 'transcript-saved', 'job',
                    'capture-queue', 'capture-moved', 'errorlog', 'done'):
        return
    state = _state(app)
    if kind == 'new-file':
        reset_preview(app)
        if state['capture_key'] is None:
            state['warning'] = False
    elif kind == 'engine-progress':
        if getattr(event[1], 'snippet', '').strip():
            state['preview_ok'] = False
    elif kind == 'transcript-saved':
        _, key, _paths, status = event
        state['preview_key'] = key
        state['preview_ok'] = status in SUCCESS and not state['warning']
        if state['preview_ok']:
            state['protected'].discard(key)
        else:
            state['protected'].add(key)
    elif kind == 'job':
        _, key, status = event
        if key == state['preview_key'] and status not in SUCCESS:
            state['preview_ok'] = False
    elif kind == 'capture-queue':
        key = str(Path(event[1]).resolve())
        state['capture_key'] = key
        if state['warning'] or event[2] not in SUCCESS | {'処理中'}:
            state['protected'].add(key)
    elif kind == 'capture-moved':
        old, new = (str(Path(value).resolve()) for value in event[1:3])
        if old in state['protected']:
            state['protected'].discard(old)
            state['protected'].add(new)
        for name in ('preview_key', 'capture_key'):
            if state[name] == old:
                state[name] = new
    elif kind == 'errorlog':
        state['warning'] = True
        state['preview_ok'] = False
        key = state['capture_key'] or state['preview_key']
        if key is not None:
            state['protected'].add(key)
    elif kind == 'done':
        stop = getattr(app, 'stop_event', None)
        if (getattr(app, 'all_stop_requested', False)
                or (stop is not None and stop.is_set())
                or any(word in event[1] for word in ('失敗', 'エラー', '中止', '一部完了'))):
            state['preview_ok'] = False
            if state['capture_key'] is not None:
                state['protected'].add(state['capture_key'])


def clear_after_reopen(app):
    """Consume an explicit X-close boundary, not ordinary minimize/restore.

    Reopening while anything is still active leaves the entire workspace alone.
    Do not schedule cleanup later: once reopened, results belong to the current
    visible workspace and must not disappear during the user's next operation.
    """
    if not getattr(app, '_clear_completed_after_close', False):
        return 0
    app._clear_completed_after_close = False
    if any(getattr(app, name, False) for name in
           ('busy', 'closing', 'live_active', 'gamebar_active', 'all_stop_waiting')):
        return 0
    worker = getattr(app, 'worker', None)
    processes = getattr(app, 'processes', None)
    if (worker is not None and worker.is_alive()) or (processes is not None and processes.active()):
        return 0
    # Never clear before queued final statuses or UI callbacks have been applied.
    for name in ('events', 'ui_callbacks'):
        pending = getattr(app, name, None)
        if pending is not None and not pending.empty():
            return 0
    if app.root.grab_current() is not None:
        return 0
    state = _state(app)
    pending_edits = set(getattr(app, 'pending_speaker_edits', ()))
    rows = tuple(app.tree.get_children())
    removed = []
    for key in rows:
        values = app.tree.item(key, 'values')
        if (len(values) < 3 or values[2] not in SUCCESS
                or key in state['protected']
                or pending_edits.intersection(app.job_outputs.get(key, ()))):
            continue
        removed.append(key)
    preview_key = state['preview_key']
    clear_preview = bool(state['preview_ok'] and
        (preview_key in removed or preview_key not in app.job_sources))
    # A displayed snippet is not proof of a committed transcription.
    # Keep unknown/unsaved previews even when older queue rows succeeded.
    if pending_edits:
        clear_preview = False
    for key in removed:
        app.tree.delete(key)
        app.job_sources.pop(key, None)
        app.job_outputs.pop(key, None)
        app.job_options.pop(key, None)
        with app.queue_policy_lock:
            app.job_policies.pop(key, None)
        if getattr(app, 'active_job_key', None) == key:
            app.active_job_key = None
    if clear_preview:
        app._reset_recognition_preview()
    if removed:
        app._refresh_queue_display()
    if removed or clear_preview:
        app.log('[画面整理] 正常完了した表示だけをクリアしました。保存済みファイルと診断ログは保持しています。\n')
        if not app.tree.get_children():
            english = getattr(app.config, 'ui_language', 'ja') == 'en'
            app.status.set('Ready. Add files or start recording.' if english else
                           '準備完了。ファイルを追加するか、録音を開始してください。')
    return len(removed)
