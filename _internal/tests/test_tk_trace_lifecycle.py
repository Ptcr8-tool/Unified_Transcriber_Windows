"""Exercise real Tcl traces without requiring a graphical display."""
import gc
from pathlib import Path
import sys
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio_ui import bind_timer_cleanup, bind_variable_trace


class WindowLifecycle:
    """Supply only the window event boundary; traces use the real Tcl engine."""
    def __init__(self, interpreter):
        self.tk = interpreter.tk
        self._tclCommands = []
        self.destroy_callbacks = []

    def bind(self, event, callback, add=None):
        if event != '<Destroy>' or add != '+':
            raise AssertionError('Unexpected lifecycle binding')
        self.destroy_callbacks.append(callback)

    def destroy(self):
        for callback in self.destroy_callbacks:
            callback(SimpleNamespace(widget=self))
        self.destroy_callbacks.clear()


class TraceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.interpreter = tk.Tcl()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.destroy()
        self.windows.clear()
        self.interpreter = None
        gc.collect()

    def window(self):
        result = WindowLifecycle(self.interpreter)
        self.windows.append(result)
        return result

    def test_destroy_releases_dialog_callback_before_later_worker_gc(self):
        window = self.window()
        finalized = []

        class Dialog:
            def __init__(self, interpreter):
                self.value = tk.StringVar(interpreter, value='sample')
                bind_variable_trace(window, self.value, 'write', self.changed)

            def changed(self, *_):
                pass

        dialog = Dialog(self.interpreter)
        reference = weakref.ref(dialog)
        weakref.finalize(dialog, lambda: finalized.append(threading.get_ident()))
        dialog = None
        gc.collect()
        self.assertIsNotNone(reference(), 'Tcl must retain the active callback')
        window.destroy()
        self.assertIsNone(reference(), 'Closing the dialog must release its callback')
        worker = threading.Thread(target=gc.collect, name='audio-gc-regression')
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(finalized, [threading.get_ident()])

    def test_shared_variable_keeps_value_and_other_owners_observers(self):
        first, second = self.window(), self.window()
        value = tk.StringVar(self.interpreter, value='initial')
        calls = []
        external = value.trace_add('write', lambda *_: calls.append('external'))
        bind_variable_trace(first, value, 'write', lambda *_: calls.append('first'))
        bind_variable_trace(second, value, 'write', lambda *_: calls.append('second'))
        try:
            value.set('before')
            self.assertCountEqual(calls, ['first', 'second', 'external'])
            calls.clear()
            first.destroy()
            self.assertEqual(value.get(), 'before')
            value.set('after')
            self.assertCountEqual(calls, ['second', 'external'])
            calls.clear()
            second.destroy()
            value.set('last')
            self.assertEqual(calls, ['external'])
            self.assertEqual(value.get(), 'last')
        finally:
            value.trace_remove('write', external)

    def test_cleanup_is_idempotent_and_ignores_descendant_destroy(self):
        window = self.window()
        value = tk.StringVar(self.interpreter)
        calls = []
        bind_timer_cleanup(window)
        bind_variable_trace(window, value, 'write', lambda *_: calls.append(value.get()))
        bind_timer_cleanup(window)
        self.assertEqual(len(window.destroy_callbacks), 1)
        window.destroy_callbacks[0](SimpleNamespace(widget=object()))
        value.set('still open')
        self.assertEqual(calls, ['still open'])
        window.destroy()
        window.destroy()
        value.set('closed')
        self.assertEqual(calls, ['still open'])
        self.assertEqual(value.trace_info(), [])

    def test_destroy_tolerates_an_explicitly_removed_owned_trace(self):
        window = self.window()
        value = tk.StringVar(self.interpreter)
        token = bind_variable_trace(window, value, 'write', lambda *_: None)
        value.trace_remove('write', token)
        window.destroy()
        self.assertEqual(value.trace_info(), [])

    def test_window_can_close_from_its_own_variable_callback(self):
        window = self.window()
        value = tk.StringVar(self.interpreter)
        bind_variable_trace(window, value, 'write', lambda *_: window.destroy())
        value.set('close')
        self.assertEqual(value.get(), 'close')
        self.assertEqual(value.trace_info(), [])


if __name__ == '__main__':
    unittest.main()
