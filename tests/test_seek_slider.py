"""真实 Qt 鼠标事件验收试听跳转；不加载或播放音频。"""

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from djtool.ui import theme
from djtool.ui.widgets import SeekSlider


class SeekSliderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply(cls.app, 'dark')

    def setUp(self):
        self.slider = SeekSlider()
        self.slider.setRange(0, 1000)
        self.slider.resize(400, 32)
        self.slider.show()
        self.app.processEvents()
        self.requests = QSignalSpy(self.slider.seekRequested)
        self.releases = QSignalSpy(self.slider.sliderReleased)

    def tearDown(self):
        self.slider.close()
        self.slider.deleteLater()
        self.app.processEvents()

    def point(self, fraction):
        return QPoint(round((self.slider.width() - 1) * fraction),
                      self.slider.height() // 2)

    def click(self, fraction):
        QTest.mouseClick(self.slider, Qt.MouseButton.LeftButton, pos=self.point(fraction))
        self.app.processEvents()

    def test_click_quarter_and_three_quarters_seeks_immediately(self):
        for fraction, expected in ((0.25, 250), (0.75, 750)):
            with self.subTest(fraction=fraction):
                before = self.requests.count()
                QTest.mousePress(self.slider, Qt.MouseButton.LeftButton,
                                 pos=self.point(fraction))
                self.assertEqual(self.requests.count(), before + 1)
                self.assertTrue(self.slider.isSliderDown())
                self.assertAlmostEqual(self.requests.at(before)[0], expected, delta=20)
                self.assertEqual(self.slider.value(), self.requests.at(before)[0])
                QTest.mouseRelease(self.slider, Qt.MouseButton.LeftButton,
                                   pos=self.point(fraction))
                self.assertFalse(self.slider.isSliderDown())
        self.assertEqual(self.releases.count(), 2)

    def test_click_extreme_ends_reaches_exact_range_endpoints(self):
        self.slider.setValue(500)
        self.click(0)
        self.assertEqual(self.slider.value(), 0)
        self.click(1)
        self.assertEqual(self.slider.value(), 1000)
        self.assertEqual([self.requests.at(i)[0] for i in range(self.requests.count())],
                         [0, 1000])

    def test_press_track_then_drag_and_release_keeps_final_position(self):
        QTest.mousePress(self.slider, Qt.MouseButton.LeftButton, pos=self.point(0.25))
        QTest.mouseMove(self.slider, self.point(0.75))
        self.assertAlmostEqual(self.slider.sliderPosition(), 750, delta=20)
        QTest.mouseRelease(self.slider, Qt.MouseButton.LeftButton, pos=self.point(0.75))
        self.assertEqual(self.requests.count(), 1)
        self.assertAlmostEqual(self.requests.at(0)[0], 250, delta=20)
        self.assertEqual(self.releases.count(), 1)
        self.assertAlmostEqual(self.slider.value(), 750, delta=20)

    def test_existing_handle_uses_native_drag_without_click_seek(self):
        self.slider.setValue(200)
        _, _, handle = self.slider._parts()
        QTest.mousePress(self.slider, Qt.MouseButton.LeftButton, pos=handle.center())
        self.assertTrue(self.slider.isSliderDown())
        self.assertEqual(self.requests.count(), 0)
        QTest.mouseMove(self.slider, self.point(0.75))
        QTest.mouseRelease(self.slider, Qt.MouseButton.LeftButton, pos=self.point(0.75))
        self.assertEqual(self.releases.count(), 1)
        self.assertAlmostEqual(self.slider.value(), 750, delta=20)
        self.assertEqual(self.requests.count(), 0)

    def test_programmatic_progress_updates_never_request_seek(self):
        self.slider.setValue(300)
        self.slider.setSliderPosition(600)
        self.slider.setRange(0, 2000)
        self.assertEqual(self.requests.count(), 0)
        self.assertEqual(self.releases.count(), 0)

    def test_disabled_and_unknown_duration_cannot_seek(self):
        self.slider.setEnabled(False)
        self.click(0.75)
        self.assertEqual(self.slider.value(), 0)
        self.slider.setEnabled(True)
        self.slider.setRange(0, 0)
        self.click(0.75)
        self.assertEqual(self.requests.count(), 0)
        self.assertEqual(self.releases.count(), 0)
        self.assertFalse(self.slider.isSliderDown())

    def test_right_button_does_not_request_seek(self):
        QTest.mouseClick(self.slider, Qt.MouseButton.RightButton, pos=self.point(0.75))
        self.assertEqual(self.requests.count(), 0)

    def test_rtl_and_inverted_appearance_follow_the_rendered_direction(self):
        self.slider.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.click(0.25)
        self.assertAlmostEqual(self.slider.value(), 750, delta=20)
        self.slider.setInvertedAppearance(True)
        self.slider.setValue(500)
        self.click(0.25)
        self.assertAlmostEqual(self.slider.value(), 250, delta=20)



if __name__ == '__main__':
    unittest.main()
