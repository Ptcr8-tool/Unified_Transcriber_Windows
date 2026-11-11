"""Nonmodal feedback for the stop shortcut; normal stop is never escalated here.

Acknowledgements describe an accepted stop request, not a completed save. No
sounds, focus changes, modal dialogs, external programs or network are used.
"""
from __future__ import annotations

import time

_REPEAT_SECONDS = 2.0
_MESSAGES = {
    'ja': {
        'live': ('録音停止を受け付けました',
                 '録音の保存処理を進めています。設定済みの文字起こし・形式変換は続けます。'),
        'live_wait': ('録音の終了処理中です',
                      '録音の保存・後処理を進めています。すべて中断する場合はTOPの「すべて停止」を使用してください。'),
        'gamebar': ('録画停止を受け付けました',
                    '録画・録音の保存処理を進めています。設定済みの文字起こし・形式変換は続けます。'),
        'gamebar_wait': ('録画の終了処理中です',
                         '録画・録音の保存・後処理を進めています。すべて中断する場合はTOPの「すべて停止」を使用してください。'),
        'process': ('処理の停止を受け付けました',
                    '実行中の処理を終了しています。保存済みのファイルは保持します。'),
        'process_wait': ('処理を停止中です',
                         '終了処理を進めています。保存済みのファイルは保持します。'),
        'all': ('すべての処理を停止中です',
                'すでに全停止を受け付けています。保存済みのファイルは保持します。'),
        'idle': ('停止する処理はありません',
                 '現在、録音・録画や処理は実行していません。'),
        'error': ('停止要求に失敗しました',
                  '停止完了ではありません。録音画面とログで状態を確認してください。'),
    },
    'en': {
        'live': ('Recording stop requested',
                 'Saving the recording. Configured transcription and format conversion will continue.'),
        'live_wait': ('Finishing the recording',
                      'Saving and post-processing continue. Use Stop all on the main screen to cancel the remaining work.'),
        'gamebar': ('Video recording stop requested',
                    'Saving video and audio. Configured transcription and format conversion will continue.'),
        'gamebar_wait': ('Finishing the video recording',
                         'Saving and post-processing continue. Use Stop all on the main screen to cancel the remaining work.'),
        'process': ('Stop requested',
                    'Finishing the current operation. Files already saved will be kept.'),
        'process_wait': ('Stopping the current operation',
                         'The stop request is already being processed. Files already saved will be kept.'),
        'all': ('Stopping all operations',
                'Stop all has already been requested. Files already saved will be kept.'),
        'idle': ('Nothing to stop', 'No recording or operation is currently running.'),
        'error': ('Stop request failed',
                  'Stopping is not confirmed. Check the recording window and logs.'),
    },
}


def stop_with_feedback(app) -> None:
    """Called on the GUI thread from the hotkey queue (and tray Stop action).

    Always request normal stop before attempting a notification. The tray API
    queues work without waiting for Explorer, so recording finalization and Tk
    keep running. Notification failures cannot undo/cancel a successful stop.
    """
    if getattr(app, 'closing', False):
        return
    busy = bool(getattr(app, 'busy', False))
    already = bool(getattr(app, 'live_stopping', False))
    event = getattr(app, 'stop_event', None)
    already = already or bool(event is not None and event.is_set())
    if not busy:
        kind = 'idle'
    elif getattr(app, 'all_stop_requested', False):
        kind = 'all'
        already = True
    elif getattr(app, 'gamebar_active', False):
        kind = 'gamebar'
        already = already or app.gamebar_stop.is_set()
    elif getattr(app, 'live_active', False):
        kind = 'live'
    else:
        kind = 'process'
    try:
        app.stop()
    except OSError as exc:
        kind = 'error'
        app.log('[ショートカット停止エラー] ' + str(exc) + '\n')
    now = time.monotonic()
    previous = getattr(app, '_shortcut_stop_notice', None)
    fresh_stop = busy and not already and kind != 'error'
    # A new recording always gets feedback, even if it starts shortly after the
    # previous one. Repeated presses during finalization never cause all-stop.
    if (not fresh_stop and previous and previous[0] == kind
            and now - previous[1] < _REPEAT_SECONDS):
        return
    app._shortcut_stop_notice = (kind, now)
    language = getattr(getattr(app, 'config', None), 'ui_language', 'ja')
    catalog = _MESSAGES['en' if language == 'en' else 'ja']
    title, body = catalog.get(kind + '_wait' if already else kind, catalog[kind])
    app.log('[ショートカット通知] ' + title + ' / ' + body + '\n')
    if kind == 'idle':
        app.status.set(title)
    tray = getattr(app, 'tray', None)
    try:
        queued = tray is not None and tray.notify(title, body)
    except Exception as exc:
        queued = False
        app.log('[通知エラー] ' + type(exc).__name__ + '\n')
    if not queued:
        app.log('[通知] Windows通知を送信できませんでした。停止処理の状態は画面とログで確認できます。\n')
