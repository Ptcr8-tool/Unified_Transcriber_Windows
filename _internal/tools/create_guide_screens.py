"""Create documentation screenshots from real widgets and synthetic data.

Run in a desktop or virtual display with Tk and Pillow installed. No recording,
AI inference, downloads, registry writes or configuration saves are performed.
"""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import os
import sys
import tempfile
import tkinter as tk

from PIL import ImageGrab

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))
import unified_gui as gui
from core import Config


def main():
    images = CODE_DIR / 'docs' / 'images'
    images.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='sample-guide-')))
        config = Config(profile='faster-res', output_dir='C:/Sample/Transcriptions', theme_mode='light',
            onboarding_seen=True, capture_setup_complete=True,
            live_recording_dir='C:/Sample/Recordings', gamebar_captures_dir='C:/Sample/Captures')
        stack.enter_context(patch.dict(os.environ, {'HF_TOKEN': ''}))
        stack.enter_context(patch.object(gui, 'load_config', return_value=config))
        for target in ('unified_gui.save_config', 'core.save_config',
                       'studio_workflow.save_config', 'hotkey_ui.save_config',
                       'unified_gui.ensure_output_directories'):
            stack.enter_context(patch(target))
        stack.enter_context(patch.object(gui, 'CODE_DIR', folder))
        diagnostic = SimpleNamespace(session_path=Path('C:/Sample/Logs/session.log'),
            error_path=Path('C:/Sample/Logs/errors.log'), folder=folder,
            write=MagicMock(), error=MagicMock(), token='')
        stack.enter_context(patch.object(gui, 'SessionLogs', return_value=diagnostic))
        stack.enter_context(patch('screen_capture.windows', return_value=[('サンプル会議', 100)]))
        root = gui.TkinterDnD.Tk() if gui.TkinterDnD is not None else tk.Tk()
        app = gui.UnifiedApp(root)
        root.geometry('1180x900+20+20')
        root.update()

        def capture(name, win):
            win.lift()
            root.update_idletasks()
            root.update()
            x, y = win.winfo_rootx(), win.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x+win.winfo_width(), y+win.winfo_height()),
                xdisplay=os.environ.get('DISPLAY')).save(images / name)
            print(name)

        def finish(win):
            for job in root.tk.call('after', 'info'):
                try:
                    root.tk.call('after', 'cancel', job)
                except tk.TclError:
                    pass
            if win.winfo_exists():
                win.destroy()
            root.update()

        samples = []
        for name in ('sample_meeting_01.wav', 'sample_interview_02.mp4', 'sample_recording_03.wav'):
            sample = folder/name
            sample.write_bytes(b'RIFF sample')
            samples.append(sample)
        app._add_paths(samples)
        app.tree.set(str(samples[0]), 'state', '完了')
        app.tree.selection_set(str(samples[0]))
        for key in app.tree.get_children():
            app.tree.set(key, 'destination', 'C:/Sample/Transcriptions')
        app.status.set('サンプル会議の文字起こしが完了しました。')
        for widget, value in ((app.recognized_box,
            '[00:00:01] SPEAKER_00: サンプル会議を開始します。\n\n'
            '[00:00:05] SPEAKER_01: 本日の確認事項を共有します。\n\n'
            '[00:00:10] SPEAKER_00: 次の項目に進みます。\n'),
            (app.logbox, 'sample_meeting_01.wav\n文字起こしが完了しました。\n'
             '保存先: C:/Sample/Transcriptions\n')):
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('end', value)
            widget.configure(state='disabled')
        capture('main-light.png', root)
        app.ui.theme_var.set('dark')
        app.ui._apply_appearance(True)
        capture('main-dark.png', root)
        app.ui.theme_var.set('light')
        app.ui._apply_appearance(False)
        root.geometry('900x640+20+20')
        capture('main-compact.png', root)
        root.geometry('1180x900+20+20')
        root.update()
        dialog = gui.SettingsDialog(app)
        dialog.output_tab.master.select(dialog.output_tab)
        dialog.win.geometry('850x780+20+20')
        capture('settings.png', dialog.win)
        finish(dialog.win)
        from settings_ui import SettingsExportDialog
        dialog = SettingsExportDialog(app)
        dialog.path.set('C:/Sample/transcriber-settings.json')
        capture('settings-export.png', dialog.win)
        finish(dialog.win)

        dialog = gui.LiveCaptureDialog(app)
        dialog.win.geometry('820x1300+20+20')
        dialog.source.set('マイク＋PC再生音（同時録音）')
        dialog.recording_channels.set('L：PC音 / R：マイク')
        capture('guide-recording.png', dialog.win)
        finish(dialog.win)

        dialog = gui.GameBarRecordingDialog(app)
        dialog.win.geometry('820x1340+20+20')
        dialog.realtime.set(True)
        dialog._refresh_live_controls()
        capture('guide-gamebar.png', dialog.win)
        finish(dialog.win)

        sample = folder / 'sample_meeting.txt'
        sample.write_text('[00:00:01] SPEAKER_00: サンプル会議を開始します。\n'
            '[00:00:05] SPEAKER_01: 本日の確認事項を共有します。\n'
            '[00:00:10] SPEAKER_00: 次の項目に進みます。\n', encoding='utf-8')
        dialog = gui.SpeakerEditDialog(app, sample)
        dialog.input.set('C:/Sample/Transcriptions/sample_meeting.txt')
        dialog.output.set('C:/Sample/SpeakerEdits/sample_meeting_names.txt')
        for speaker, variable in dialog.rename_vars.items():
            variable.set('話者A' if speaker == 'SPEAKER_00' else '話者B')
        dialog._expand_all()
        dialog.win.geometry('860x780+20+20')
        capture('guide-speakers.png', dialog.win)
        finish(dialog.win)

        dialog = gui.ConcatDialog(app, 'text')
        dialog.win.geometry('1120x900+20+20')
        dialog.listbox.column('filename', width=240, minwidth=180)
        dialog.destination_mode.set('custom')
        dialog.custom_folder.set('C:/Sample/Merged')
        for index in (1, 2):
            dialog.listbox.insert('', 'end', values=(index, f'sample_part_{index:02}.txt',
                '2000-01-01 09:00', '2000-01-01 09:00', '2000-01-01 09:00', 'サンプルの録音日時'))
        dialog.folder_display.set('C:/Sample/Merged')
        dialog.filename.set('sample_joined.txt')
        dialog.folder_display.set('C:/Sample/Merged')
        dialog.output.set('C:/Sample/Merged/sample_joined.txt')
        dialog.move_originals.set(True)
        capture('guide-merge.png', dialog.win)
        finish(dialog.win)

        dialog = gui.BulkSetupDialog(app)
        dialog._load_preset('recommended-cpu')
        dialog.win.geometry('860x760+20+20')
        capture('guide-setup.png', dialog.win)
        finish(dialog.win)

        # New setup/update windows use the same real controls. Only the installed
        # version lookup is synthetic; no install or update action is invoked.
        import installer_ui
        from installation import InstallationInfo
        args = SimpleNamespace(target=Path('C:/Sample/UnifiedTranscriber'),
            uninstall=False, update=False, wait_pid=None)
        fresh = InstallationInfo(args.target, 'new', None, ('install',))
        with patch('installer_ui.inspect_installation', return_value=fresh):
            window = tk.Toplevel(root)
            wizard = installer_ui.InstallerWindow(window, args)
            wizard.minimal.set(True)
            window.geometry('880x810+20+20')
            capture('guide-install.png', window)
            wizard.close()
        installed = InstallationInfo(args.target, 'current', installer_ui.APP_VERSION,
            ('repair', 'uninstall', 'cancel'), registered_program=True, create_shortcut=True)
        with patch('installer_ui.inspect_installation', return_value=installed):
            window = tk.Toplevel(root)
            wizard = installer_ui.InstallerWindow(window, args)
            window.geometry('880x810+20+20')
            capture('guide-maintenance.png', window)
            wizard.close()
        from update_ui import UpdateController, UpdateSettingsDialog
        controller = UpdateController(app)
        dialog = UpdateSettingsDialog(controller)
        dialog.win.geometry('720x550+20+20')
        capture('guide-updates.png', dialog.win)
        dialog.close()
        controller.close()
        app.busy = False
        app.close()


if __name__ == '__main__':
    main()
