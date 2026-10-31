"""No AI downloads: verify real engine argument surfaces, segmentation and GUI device routing."""
import queue
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import BY_ID, Config, PROFILES, build_command
from engines.openai_resemblyzer import fixed_windows, labels_for_transcript_segments
from engines.segment_resemblyzer import TranscriptItem, utterance_windows, compute_cluster_labels
from unified_gui import UnifiedApp


class GranularityTests(unittest.TestCase):
    def test_new_default_is_turbo_and_utterance(self):
        cfg = Config()
        self.assertEqual(cfg.model_openai, 'turbo')
        self.assertEqual(cfg.model_faster, 'large-v3-turbo')
        self.assertEqual(cfg.diarization_unit, 'utterance')

    def test_all_res_profiles_have_both_switches(self):
        for profile in PROFILES:
            for mode in ('fixed', 'utterance'):
                with self.subTest(profile=profile.id, mode=mode):
                    cfg = Config(profile=profile.id, diarization_unit=mode)
                    args = build_command(Path('input.wav'), Path('output.txt'), cfg,
                                         Path('python.exe'), 'cpu')
                    if profile.diarizer == 'pyannote':
                        self.assertNotIn('--diarization-unit', args)
                    else:
                        self.assertEqual(args[args.index('--diarization-unit')+1], mode)
                        self.assertEqual(args[args.index('--segment-length')+1], '20.0')
                        self.assertIn('--threshold', args)

    def test_auto_fixed_speaker_count_routes_to_all_res(self):
        for profile in (p for p in PROFILES if p.diarizer != 'pyannote'):
            fixed = build_command(Path('in.wav'), Path('out.txt'),
                                  Config(profile=profile.id, speaker_mode='fixed', speakers=3),
                                  Path('python.exe'), 'cpu')
            auto = build_command(Path('in.wav'), Path('out.txt'),
                                 Config(profile=profile.id, speaker_mode='auto'),
                                 Path('python.exe'), 'cpu')
            self.assertIn('--speakers', fixed)
            self.assertNotIn('--speakers', auto)

    def test_invalid_unit_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(diarization_unit='neither').validate()

    def test_utterance_windows_use_silence_or_max_length_not_clock_grid(self):
        items = [TranscriptItem(.20, .42, 'UNKNOWN', 'お'),
                 TranscriptItem(.45, 1.6, 'UNKNOWN', 'はよう。'),
                 TranscriptItem(1.85, 2.5, 'UNKNOWN', '続き'),
                 TranscriptItem(4.0, 5.5, 'UNKNOWN', 'こんばんは'),
                 TranscriptItem(5.8, 6.1, 'UNKNOWN', '！')]
        self.assertEqual(utterance_windows(items), [(.2, 1.6), (1.85, 2.5), (4.0, 6.1)])
        self.assertEqual(utterance_windows([]), [])
        self.assertEqual(utterance_windows([
            TranscriptItem(0, 5, 'UNKNOWN','a'), TranscriptItem(5, 10, 'UNKNOWN','b')
        ], max_seconds=9), [(0,5),(5,10)])

    def test_openai_fixed_windows_and_overlap_label_assignment(self):
        windows = fixed_windows([0] * 200, 10, 10.)
        self.assertEqual(windows, [{'start':0., 'end':10.}, {'start':10., 'end':20.}])
        segments = [{'start': 1., 'end': 4.}, {'start':9., 'end':11.},
                    {'start':15., 'end': 18.}]
        indices, labels = labels_for_transcript_segments(segments, windows, [0,1], [2,5])
        self.assertEqual(indices,[0,1,2])
        self.assertEqual(labels,[2,2,5])
        with self.assertRaises(ValueError):
            fixed_windows([0]*100, 10, .5)

    def test_cluster_automatic_and_speaker_cap(self):
        try:
            import numpy as np
            import sklearn
        except ImportError:
            self.skipTest('optional NumPy/scikit-learn dependencies missing')
        vectors = np.asarray([[1,0], [.999,.01], [0,1], [.01,.999]], dtype=np.float32)
        self.assertEqual(len(set(compute_cluster_labels(vectors, 6, None, .25))), 2)
        self.assertEqual(len(set(compute_cluster_labels(vectors, 1, None, .25))), 1)
        self.assertEqual(len(set(compute_cluster_labels(vectors, 2, 2, .25))), 2)


    def test_asr_parsers_accept_modes(self):
        from engines.openai_resemblyzer import build_parser
        args = build_parser().parse_args(['a.wav', '--diarization-unit','fixed',
                                          '--segment-length','8'])
        self.assertEqual((args.diarization_unit, args.segment_length), ('fixed',8.))
        from engines.segment_resemblyzer import parse_args
        with mock.patch.object(sys, 'argv', ['prog','a.wav', '--diarization-unit','utterance',
                                            '--speakers','2']):
            args = parse_args()
        self.assertEqual(args.diarization_unit, 'utterance')
        self.assertEqual(args.speakers, 2)
        self.assertEqual(args.model, 'large-v3-turbo')


class CUDASelectionTests(unittest.TestCase):
    def test_auto_selection_rejects_cuda_diagnostics_failure(self):
        app = UnifiedApp.__new__(UnifiedApp)
        app.device_cache = {}
        app.events = queue.Queue()
        cfg = Config(profile='faster-res')
        with mock.patch('unified_gui.subprocess.run', return_value=subprocess.CompletedProcess([],2)) as runner:
            self.assertEqual(app._resolve_device(cfg, Path('python.exe')), 'cpu')
            self.assertEqual(app._resolve_device(cfg, Path('python.exe')), 'cpu')
            runner.assert_called_once()
            self.assertIn('cuda_diagnostic.py', str(runner.call_args))
            self.assertIn('faster', str(runner.call_args))
        self.assertEqual(app.events.get_nowait()[0], 'log')

    def test_auto_selection_allows_passed_cuda_checks(self):
        app = UnifiedApp.__new__(UnifiedApp)
        app.device_cache = {}
        app.events = queue.Queue()
        cfg = Config(profile='qwen-res')
        with mock.patch('unified_gui.subprocess.run', return_value=subprocess.CompletedProcess([],0)) as runner:
            self.assertEqual(app._resolve_device(cfg, Path('python.exe')), 'cuda')
            self.assertIn('qwen', str(runner.call_args))


if __name__ == '__main__':
    unittest.main()
