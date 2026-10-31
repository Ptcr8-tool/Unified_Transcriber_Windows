"""User paths through grouped menus preserve the existing action inventory."""
from pathlib import Path
import sys
from tkinter import ttk
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_v25_studio_ui as fixtures
from ui_controls import ToggleSwitch


class GroupedNavigation(fixtures.StudioGUIFixture):
    def test_every_existing_menu_action_is_reachable_once_through_categories(self):
        view=self.app.ui
        expected={'live','gamebar','capture_setup','cancel_live_asr','import','text_merge','audio_merge',
            'csv_merge','convert','speaker','bulk','setup','cuda','limits','shortcuts','hints','dnd','logs',
            'detached_log','export_logs','help','background','add_folder','watch','file_output','file_policy',
            'remove','select_all','clear','result','open_source','open_result','result_speaker','settings_export','settings_import','app_updates'}
        self.assertEqual(set(view.actions),expected)
        labels=[]
        for button in view.drawer_buttons:
            button.toggle();self.root.update()
            groups=list(button.group_buttons)
            if groups:
                for group in groups:
                    button.group_buttons[group].invoke();self.root.update()
                    labels.extend(label for label,widget in button.action_buttons.items() if isinstance(widget,ttk.Button))
            else:labels.extend(button.action_buttons)
            button.close()
        expected_labels=[action.cget('label') for action in view.actions.values()]
        self.assertCountEqual(labels,expected_labels)

    def test_category_action_buttons_open_the_original_dialog_and_close_drawer(self):
        view=self.app.ui
        tools=next(button for button in view.drawer_buttons if button.cget('text')=='ツール')
        cases=(('結合・音声の抽出','TXT結合…','concat',('text',)),
               ('結合・音声の抽出','音声結合・動画から音声を抽出…','concat',('audio',)),
               ('変換・話者名','TXT ⇔ CSV変換…','convert',()),
               ('変換・話者名','話者名を設定…','speaker',()))
        for group,label,key,args in cases:
            with self.subTest(label=label):
                tools.toggle();self.root.update();tools.group_buttons[group].invoke();self.root.update()
                factory=MagicMock()
                with patch.dict(view.dialogs,{key:factory}):tools.action_buttons[label].invoke()
                factory.assert_called_once_with(self.app,*args)
                self.assertFalse(tools.opened)

    def test_busy_menu_categories_keep_environment_disabled_and_logs_available(self):
        a=self.app;view=a.ui;a._set_busy(True)
        settings=next(button for button in view.drawer_buttons if button.cget('text')=='設定・診断')
        settings.toggle();self.root.update()
        settings.group_buttons['環境の準備・確認'].invoke();self.root.update()
        self.assertTrue(all(str(button.cget('state'))=='disabled' for button in settings.action_buttons.values()))
        settings.group_buttons['ログ'].invoke();self.root.update()
        self.assertTrue(all(str(button.cget('state'))=='normal' for button in settings.action_buttons.values()))
        settings.close();a._set_busy(False)
        settings.toggle();self.root.update()
        self.assertEqual(settings.active_group,'ログ')

    def test_display_toggle_and_keyboard_action_keep_working_after_group_switches(self):
        view=self.app.ui;button=view.selection_button
        button.toggle();self.root.update();button.group_buttons['表示'].invoke();self.root.update()
        toggle=next(w for w in button.action_buttons.values() if isinstance(w,ToggleSwitch))
        toggle.invoke();self.assertTrue(view.detailed_columns.get())
        button.group_buttons['一覧の操作'].invoke();self.root.update()
        action=button.action_buttons['すべて選択']
        paths=self.add_samples(2);action.focus_force()
        action.event_generate('<KeyPress-space>');action.event_generate('<KeyRelease-space>')
        self.root.after(160,self.root.quit);self.root.mainloop()
        self.assertCountEqual(self.app.tree.selection(),[str(path) for path in paths])
        self.assertFalse(button.opened)
        button.toggle();self.root.update();button.group_buttons['表示'].invoke();self.root.update()
        toggle=next(w for w in button.action_buttons.values() if isinstance(w,ToggleSwitch))
        self.assertTrue(toggle.instate(['selected']))

    def test_compact_header_menu_keeps_first_file_visible_and_restores_model_picker(self):
        path=self.add_samples(1)[0];view=self.app.ui
        self.root.geometry('900x640');self.root.update()
        settings=next(button for button in view.drawer_buttons if button.cget('text')=='設定・診断')
        settings.toggle();self.root.update();settings.group_buttons['操作・設定'].invoke()
        self.root.after(160,self.root.quit);self.root.mainloop()
        row=self.app.tree.bbox(str(path));self.assertTrue(row)
        self.assertLessEqual(row[1]+row[3],self.app.tree.winfo_height())
        self.assertLessEqual(settings._clip.winfo_height(), view._header_drawer_height())
        if settings._body.winfo_reqheight() > settings._clip.winfo_height():
            settings._clip.yview_moveto(1.0);self.root.update()
            self.assertAlmostEqual(settings._clip.yview()[1],1.0,places=3)
        settings.close();self.root.update();self.assertTrue(view.profile_box.winfo_ismapped())


if __name__=='__main__':unittest.main()
