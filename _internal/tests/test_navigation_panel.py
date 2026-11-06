"""First-use navigation paths through the anchored category/action menu."""
from pathlib import Path
import sys
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio_ui
from test_v25_studio_ui import StudioGUIFixture


class NavigationPanelTests(StudioGUIFixture):
    def menu(self, label):
        return next(button for button in self.app.ui.drawer_buttons
                    if button.cget('text') == label)

    def open_menu(self, label):
        button = self.menu(label)
        button.toggle()
        self.root.update()
        self.assertTrue(button.opened)
        self.assertTrue(button.panel.winfo_ismapped())
        return button

    def key(self, widget, name):
        widget.event_generate(f'<KeyPress-{name}>')
        # Enter and Escape can destroy the menu on key press, before release.
        if widget.winfo_exists():
            widget.event_generate(f'<KeyRelease-{name}>')
        self.root.update()

    @staticmethod
    def bounds(widget):
        return (widget.winfo_rootx(), widget.winfo_rooty(),
                widget.winfo_width(), widget.winfo_height())

    def test_categories_show_parent_location_and_explanations_for_every_operation(self):
        for trigger in self.app.ui.drawer_buttons:
            with self.subTest(menu=trigger.cget('text')):
                trigger.toggle()
                self.root.update()
                for category in list(trigger.group_buttons) or [None]:
                    if category is not None:
                        trigger.group_buttons[category].invoke()
                        self.root.update()
                        self.assertEqual(trigger.group_title.cget('text'), category)
                        self.assertIn(category, trigger.location.cget('text'))
                        self.assertTrue(trigger.group_buttons[category].instate(['selected']))
                    self.assertIn(trigger.cget('text'), trigger.location.cget('text'))
                    self.assertTrue(trigger.group_description.cget('text').strip())
                    self.assertTrue(trigger.action_buttons)
                    for label in trigger.action_buttons:
                        with self.subTest(action=label):
                            self.assertTrue(trigger.action_descriptions[label].cget('text').strip())
                trigger.close()

    def test_short_menus_expose_all_five_actions_without_selecting_a_category(self):
        for label in ('録音・録画', 'ツール'):
            with self.subTest(label=label):
                menu = self.open_menu(label)
                self.assertFalse(menu.group_buttons)
                self.assertEqual(len(menu.action_buttons), 5)
                actions = list(menu.action_buttons.values())
                for action in actions:
                    self.assertIs(self.root.focus_get(), action)
                    self.assertIn(action.cget('text'), menu.location.cget('text'))
                    self.key(action, 'Down')
                self.key(self.root.focus_get(), 'Escape')
                self.assertIs(self.root.focus_get(), menu)

    def test_action_home_and_end_skip_disabled_endpoints(self):
        menu = self.open_menu('ツール')
        actions = list(menu.action_buttons.values())
        self.key(actions[0], 'Down')
        actions[0].state(['disabled'])
        actions[-1].state(['disabled'])
        self.key(actions[1], 'End')
        self.assertIs(self.root.focus_get(), actions[-2])
        self.key(actions[-2], 'Home')
        self.assertIs(self.root.focus_get(), actions[1])

    def test_leaving_hovered_action_restores_focused_path(self):
        menu = self.open_menu('ツール')
        focused, hovered = list(menu.action_buttons.values())[:2]
        description = menu.action_descriptions[hovered.cget('text')]

        def point_at(widget):
            widget.event_generate('<Motion>', warp=True, x=5, y=5)
            self.root.update()

        point_at(hovered)
        hovered_path = menu.location.cget('text')
        self.assertIn(hovered.cget('text'), hovered_path)
        with patch.object(menu.location, 'configure', wraps=menu.location.configure) as changes:
            point_at(description)
            for change in changes.call_args_list:
                self.assertEqual(change.kwargs['text'], hovered_path)
        point_at(menu.close_button)
        self.assertIs(self.root.focus_get(), focused)
        self.assertIn(focused.cget('text'), menu.location.cget('text'))

    def test_compact_menu_keeps_parent_visible_while_long_operation_list_scrolls(self):
        self.root.geometry('900x640+0+0')
        self.root.update()
        button = self.open_menu('設定・診断')
        button.group_buttons['操作・設定'].invoke()
        self.root.update()
        category = button.group_buttons['操作・設定']
        before = self.bounds(category)
        self.assertGreaterEqual(len(button.action_buttons), 6)
        self.assertLess(button._clip.yview()[1], 1)
        button._clip.yview_moveto(1)
        self.root.update()
        self.assertEqual(self.bounds(category), before)
        self.assertGreater(button._clip.yview()[0], 0)
        last = list(button.action_buttons.values())[-1]
        self.assertGreaterEqual(last.winfo_rooty(), button._clip.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty()+last.winfo_height(),
                             button._clip.winfo_rooty()+button._clip.winfo_height())
        px, py, pw, ph = self.bounds(button.panel)
        rx, ry, rw, rh = self.bounds(self.root)
        self.assertGreaterEqual(px, rx)
        self.assertGreaterEqual(py, ry)
        self.assertLessEqual(px+pw, rx+rw)
        self.assertLessEqual(py+ph, ry+rh)

    def test_opening_and_switching_categories_never_move_the_working_area(self):
        self.add_samples(2)
        self.root.geometry('900x640')
        self.root.update()
        view = self.app.ui
        watched = (view.mode_card, view.profile_box, self.app.tree, view.workspace)
        before = [self.bounds(widget) for widget in watched]
        for label in ('設定・診断', '選択項目', 'ツール'):
            button = self.open_menu(label)
            for category in button.group_buttons.values():
                category.invoke()
                self.root.update()
                self.assertEqual([self.bounds(widget) for widget in watched], before)
                self.assertTrue(view.profile_box.winfo_ismapped())
            button.close()
        self.root.update()
        self.assertEqual([self.bounds(widget) for widget in watched], before)

    def test_escape_and_explicit_close_return_focus_to_the_trigger(self):
        button = self.open_menu('ツール')
        self.key(self.root.focus_get(), 'Escape')
        self.assertFalse(button.opened)
        self.assertIs(self.root.focus_get(), button)
        button = self.open_menu('ツール')
        button.close_button.focus_force()
        self.root.update()
        self.key(button.close_button, 'Return')
        self.assertFalse(button.opened)
        self.assertIs(self.root.focus_get(), button)

    def test_menu_keeps_usable_size_beyond_primary_screen_coordinates(self):
        # A secondary monitor can place the working window beyond the primary
        # screen width returned by Tk. Its menu must still fit that window.
        self.root.geometry('900x640+1700+20')
        self.root.update()
        button = self.open_menu('設定・診断')
        px, py, pw, ph = self.bounds(button.panel)
        rx, ry, rw, rh = self.bounds(self.root)
        self.assertGreater(pw, 600)
        self.assertGreaterEqual(px, rx)
        self.assertGreaterEqual(py, ry)
        self.assertLessEqual(px+pw, rx+rw)
        self.assertLessEqual(py+ph, ry+rh)

    def test_click_in_working_area_dismisses_nonmodal_menu(self):
        self.add_samples(1)
        button = self.open_menu('設定・診断')
        self.assertIsNone(self.root.grab_current())
        self.app.tree.event_generate('<ButtonPress-1>', x=20, y=20)
        self.app.tree.event_generate('<ButtonRelease-1>', x=20, y=20)
        self.root.update()
        self.assertFalse(button.opened)
        self.assertIsNone(button.panel)

    def test_arrow_keys_move_between_categories_and_actions_then_enter_opens_dialog(self):
        button = self.open_menu('設定・診断')
        first_category = button.active_group
        self.key(self.root.focus_get(), 'Down')
        self.assertNotEqual(button.active_group, first_category)
        self.key(self.root.focus_get(), 'Up')
        self.assertEqual(button.active_group, first_category)
        self.key(self.root.focus_get(), 'Down')
        selected_category = button.group_buttons[button.active_group]
        self.key(selected_category, 'Right')
        first_action = next(iter(button.action_buttons.values()))
        self.assertIs(self.root.focus_get(), first_action)
        self.key(first_action, 'Down')
        self.assertIsNot(self.root.focus_get(), first_action)
        self.key(self.root.focus_get(), 'Up')
        self.assertIs(self.root.focus_get(), first_action)
        self.key(first_action, 'Left')
        self.assertIs(self.root.focus_get(), selected_category)
        dialog = MagicMock()
        self.key(selected_category, 'Home')
        self.key(self.root.focus_get(), 'Right')
        with patch.dict(self.app.ui.dialogs, {'bulk': dialog}):
            self.key(self.root.focus_get(), 'Return')
        dialog.assert_called_once_with(self.app)
        self.assertFalse(button.opened)

    def test_tab_reaches_action_without_changing_selected_category(self):
        button = self.open_menu('設定・診断')
        last_category = list(button.group_buttons.values())[-1]
        last_category.invoke()
        last_category.focus_force()
        self.root.update()
        self.key(last_category, 'Tab')
        self.assertIn(self.root.focus_get(), button.action_buttons.values())
        self.assertEqual(button.active_group, last_category.cget('text'))

    def test_switching_menus_replaces_previous_popup(self):
        first = self.open_menu('ツール')
        previous_panel = first.panel
        second = self.open_menu('設定・診断')
        self.assertFalse(first.opened)
        self.assertFalse(previous_panel.winfo_exists())
        self.assertTrue(second.opened)
        self.assertEqual(sum(button.opened for button in self.app.ui.drawer_buttons), 1)

    def test_resize_closes_menu_and_reopening_releases_only_its_own_root_bindings(self):
        sequences = ('<ButtonPress-1>', '<Configure>', '<Unmap>')
        sentinel = self.root.bind('<ButtonPress-1>', lambda _event: None, add='+')
        baseline = {sequence: self.root.bind(sequence).strip() for sequence in sequences}
        for index in range(3):
            button = self.open_menu('設定・診断')
            menu_callbacks = [token for owner, _sequence, token in button._bindings
                              if owner is self.root]
            self.assertTrue(menu_callbacks)
            if index == 0:
                self.root.geometry('980x700')
                self.root.update()
                self.assertFalse(button.opened)
            else:
                button.close_button.invoke()
                self.root.update()
            self.assertEqual({sequence: self.root.bind(sequence).strip() for sequence in sequences}, baseline)
            # Other application timers may renew their callbacks while the
            # menu is open. Only the menu-owned bindings should be removed.
            for callback in menu_callbacks:
                self.assertNotIn(callback, self.root._tclCommands)
                self.assertFalse(self.root.tk.call('info', 'commands', callback))
            self.assertIn(sentinel, self.root._tclCommands)
        self.root.unbind('<ButtonPress-1>', sentinel)

    def test_light_and_dark_menu_keep_text_and_selected_category_readable(self):
        view = self.app.ui
        for dark, palette in ((False, studio_ui.LIGHT), (True, studio_ui.DARK)):
            with self.subTest(dark=dark):
                view._apply_appearance(dark)
                button = self.open_menu('設定・診断')
                button.group_buttons['操作・設定'].invoke()
                self.root.update()
                self.assertEqual(button._clip.cget('background'), palette['surface'])
                self.assertEqual(view.style.lookup('Small.TLabel', 'foreground'), palette['muted'])
                self.assertEqual(view.style.lookup('Category.TButton', 'foreground', ('selected',)),
                                 palette['blue'])
                self.assertNotEqual(palette['ink'], palette['surface'])
                self.assertTrue(button.group_description.cget('text').strip())
                button.close()


if __name__ == '__main__':
    unittest.main()
