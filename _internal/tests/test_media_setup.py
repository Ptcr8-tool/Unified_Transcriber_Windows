"""Offline coverage for secure media setup and every ASR extraction route."""
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_backend
import setup_live_capture
from engines import media_runtime, openai_resemblyzer, pyannote_backend, segment_resemblyzer


class MediaSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.environment = self.base / '_internal' / 'environments' / 'existing'
        self.python = self.environment / 'Scripts' / 'python.exe'
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b'existing interpreter')
        self.windows = SimpleNamespace(name='nt')

    def test_ready_ai_environment_still_repairs_shared_tools_without_changing_user_data(self):
        saved = {}
        for name in ('Data/settings.json', 'Models/keep.bin'):
            target = self.base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'existing user data')
            saved[target] = target.read_bytes()
        order = []
        with patch.object(setup_backend, 'os', self.windows), \
                patch.object(setup_backend, 'BASE', self.base), \
                patch.object(setup_backend.sys, 'version_info', (3, 11)), \
                patch.object(setup_backend, 'environment_folder', return_value=self.environment), \
                patch.object(setup_backend, 'environment_build', return_value=nullcontext(self.python)), \
                patch.object(setup_backend, 'ensure_media_tools', side_effect=lambda base, **kwargs: order.append(('media', base))), \
                patch.object(setup_backend, 'ensure_pip', side_effect=lambda python: order.append(('pip', python))), \
                patch.object(setup_backend, 'repair_compression_dependencies'), \
                patch.object(setup_backend, 'torch_ready', return_value=True), \
                patch.object(setup_backend, 'run') as run, redirect_stdout(io.StringIO()):
            setup_backend.setup('faster-res', 'cpu')
        self.assertEqual(order[:2], [('media', self.base), ('pip', self.python)])
        commands = [call.args for call in run.call_args_list]
        self.assertFalse(any('venv' in command for command in commands))
        self.assertFalse(any('imageio-ffmpeg' in command for command in commands))
        self.assertFalse(any(any(value.startswith('torch==') for value in command) for command in commands))
        for target, original in saved.items():
            self.assertEqual(target.read_bytes(), original)

    def recording_stack(self, stack):
        stack.enter_context(patch.object(setup_live_capture, 'os', self.windows))
        stack.enter_context(patch.object(setup_live_capture, 'BASE', self.base))
        stack.enter_context(patch.object(setup_live_capture, 'environment_build', return_value=nullcontext(self.python)))
        stack.enter_context(patch.object(setup_live_capture.sys, 'version_info', (3, 11)))
        stack.enter_context(patch.object(setup_live_capture, 'ensure_pip'))
        stack.enter_context(patch.object(setup_live_capture, 'repair_compression_dependencies'))
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_ready_recording_environment_still_repairs_tools_without_pip_reinstall(self):
        with ExitStack() as stack:
            self.recording_stack(stack)
            ensure = stack.enter_context(patch.object(setup_live_capture, 'ensure_media_tools'))
            run = stack.enter_context(patch.object(setup_live_capture.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0)))
            logged = stack.enter_context(patch.object(setup_live_capture, 'run_logged'))
            setup_live_capture.setup()
        self.assertEqual(ensure.call_args.args, (self.base,))
        self.assertEqual(run.call_count, 1)
        logged.assert_called_once()
        for call in run.call_args_list:
            command = call.args[0]
            self.assertNotIn('pip', command)
            self.assertNotIn('imageio_ffmpeg', command[-1])
            self.assertFalse(call.kwargs['shell'])

    def test_fresh_windows_recording_dependencies_do_not_install_old_media_wheel(self):
        with ExitStack() as stack:
            self.recording_stack(stack)
            stack.enter_context(patch.object(setup_live_capture, 'ensure_media_tools'))
            stack.enter_context(patch.object(setup_live_capture.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)))
            run = stack.enter_context(patch.object(setup_live_capture, 'run_logged'))
            setup_live_capture.setup()
        install = run.call_args_list[0].args[0]
        self.assertIn('sounddevice==0.5.6', install)
        self.assertIn('PyAudioWPatch==0.2.12.9', install)
        self.assertFalse(any('imageio' in value for value in install))

    def test_failed_media_repair_stops_before_running_ready_recording_dependencies(self):
        with ExitStack() as stack:
            self.recording_stack(stack)
            stack.enter_context(patch.object(setup_live_capture, 'ensure_media_tools',
                side_effect=RuntimeError('download verification failed')))
            run = stack.enter_context(patch.object(setup_live_capture.subprocess, 'run'))
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                setup_live_capture.setup()
        run.assert_not_called()


class EngineMediaRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ffmpeg = str(Path(self.temporary.name) / 'verified tools' / 'ffmpeg.exe')
        self.environment = {'Path': 'old-path', 'IMAGEIO_FFMPEG_EXE': 'old-wheel.exe'}
        self.windows = SimpleNamespace(name='nt', environ=self.environment)

    def test_windows_uses_verified_pair_for_all_engine_extractors_and_nested_libraries(self):
        with patch.object(media_runtime, 'os', self.windows), \
                patch.object(media_runtime, 'verified_media_binary', return_value=self.ffmpeg) as verify, \
                patch.object(media_runtime.shutil, 'which') as which:
            for engine in (segment_resemblyzer, pyannote_backend, openai_resemblyzer):
                with self.subTest(engine=engine.__name__), patch.object(engine, 'extract_pcm_audio') as run:
                    if engine is openai_resemblyzer:
                        engine.extract_wav(Path('input.mp4'), Path('output.wav'))
                    else:
                        engine.extract_audio(engine.check_ffmpeg(), Path('input.mp4'), Path('output.wav'))
                    self.assertEqual(run.call_args.args[0], self.ffmpeg)
            self.assertEqual(verify.call_count, 3)
            verify.assert_called_with(media_runtime.APP_BASE, 'ffmpeg')
            which.assert_not_called()
        self.assertEqual(self.environment['Path'].split(';')[0], str(Path(self.ffmpeg).parent))
        self.assertEqual(self.environment['Path'].count(str(Path(self.ffmpeg).parent)), 1)
        self.assertEqual(self.environment['IMAGEIO_FFMPEG_EXE'], self.ffmpeg)

    def test_windows_missing_or_corrupt_pair_cannot_fall_back_to_path_or_imageio(self):
        original = dict(self.environment)
        with patch.object(media_runtime, 'os', self.windows), \
                patch.object(media_runtime, 'verified_media_binary', return_value=None), \
                patch.object(media_runtime.shutil, 'which', return_value='old-system.exe') as which:
            for engine in (segment_resemblyzer, pyannote_backend, openai_resemblyzer):
                with self.subTest(engine=engine.__name__), patch.object(engine, 'extract_pcm_audio') as run:
                    with self.assertRaisesRegex(RuntimeError, 'セットアップ'):
                        if engine is openai_resemblyzer:
                            engine.extract_wav(Path('input.mp4'), Path('output.wav'))
                        else:
                            engine.check_ffmpeg()
                    run.assert_not_called()
            which.assert_not_called()
        self.assertEqual(self.environment, original)

    def test_non_windows_source_host_retains_system_and_imageio_support(self):
        with patch.object(media_runtime, 'os', SimpleNamespace(name='posix')), \
                patch.object(media_runtime, 'verified_media_binary') as verify, \
                patch.object(media_runtime.shutil, 'which', return_value='/usr/bin/ffmpeg'):
            self.assertEqual(media_runtime.checked_ffmpeg(), '/usr/bin/ffmpeg')
            verify.assert_not_called()
        wrapper = SimpleNamespace(get_ffmpeg_exe=lambda: '/test/imageio/ffmpeg')
        with patch.object(media_runtime, 'os', SimpleNamespace(name='posix')), \
                patch.object(media_runtime.shutil, 'which', return_value=None), \
                patch.dict(sys.modules, {'imageio_ffmpeg': wrapper}):
            self.assertEqual(media_runtime.checked_ffmpeg(), '/test/imageio/ffmpeg')

    def test_openai_whisper_receives_decoded_audio_instead_of_launching_bare_ffmpeg(self):
        audio = object()
        model = SimpleNamespace(transcribe=Mock(return_value={
            'segments': [{'start': 0, 'end': 1, 'text': 'example'}], 'language': 'en'}))
        modules = {
            'librosa': SimpleNamespace(load=lambda *args, **kwargs: (audio, 16000)),
            'torch': SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
            'whisper': SimpleNamespace(load_model=lambda *args, **kwargs: model),
        }
        with patch.dict(sys.modules, modules), \
                patch.object(openai_resemblyzer, 'extract_wav'), \
                patch.object(openai_resemblyzer, 'emit_gui_progress'), \
                patch.object(openai_resemblyzer, 'embed_transcript_segments', return_value=([], [])), \
                patch.object(openai_resemblyzer, 'format_transcript', return_value=['example']), \
                redirect_stdout(io.StringIO()):
            result = openai_resemblyzer.create_transcript(Path('input.mp4'), 'turbo', 'en', 6, .25, None, 'cpu')
        self.assertEqual(result, ['example'])
        self.assertIs(model.transcribe.call_args.args[0], audio)

    def test_openai_whisper_receives_hints_without_changing_results(self):
        audio = object()
        model = SimpleNamespace(transcribe=Mock(return_value={
            'segments': [{'start': 0, 'end': 1, 'text': 'original'}], 'language': 'ja'}))
        modules = {
            'librosa': SimpleNamespace(load=lambda *args, **kwargs: (audio, 16000)),
            'torch': SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
            'whisper': SimpleNamespace(load_model=lambda *args, **kwargs: model),
        }
        with patch.dict(sys.modules, modules), \
                patch.dict('os.environ', {'TRANSCRIBER_ASR_HINTS': 'サンプル社'}), \
                patch.object(openai_resemblyzer, 'extract_wav'), \
                patch.object(openai_resemblyzer, 'emit_gui_progress'), \
                patch.object(openai_resemblyzer, 'embed_transcript_segments', return_value=([], [])), \
                patch.object(openai_resemblyzer, 'format_transcript', return_value=['original']), \
                redirect_stdout(io.StringIO()):
            result = openai_resemblyzer.create_transcript(Path('input.mp4'), 'turbo', 'ja', 6, .25, None, 'cpu')
        self.assertEqual(result, ['original'])
        self.assertEqual(model.transcribe.call_args.kwargs['initial_prompt'], 'サンプル社')


if __name__ == '__main__':
    unittest.main()
