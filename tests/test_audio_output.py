"""试听输出路由验收；设备、音频输出和偏好均隔离，不访问真实音频硬件。"""

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PySide6.QtCore import QByteArray, QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

from djtool.audio_output import AudioOutputController, device_key


class FakeDevice:
    def __init__(self, identifier=b'', name=''):
        self.identifier = identifier
        self.name = name

    def id(self):
        return QByteArray(self.identifier)

    def description(self):
        return self.name


class FakeMediaDevices(QObject):
    audioOutputsChanged = Signal()

    def __init__(self, devices=(), default=None):
        super().__init__()
        self.devices = list(devices)
        self.default = default if default is not None else FakeDevice()

    def audioOutputs(self):
        return list(self.devices)

    def defaultAudioOutput(self):
        return self.default

    def change(self, devices, default):
        self.devices, self.default = list(devices), default
        self.audioOutputsChanged.emit()


class AudioOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.temporary.name) / 'audio.ini')
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        self.speaker = FakeDevice(b'\x00speaker\xff', 'Mac 扬声器')
        self.headphones = FakeDevice(b'\x01headphones', '耳机')
        self.devices = FakeMediaDevices([self.speaker, self.headphones], self.speaker)
        self.output = Mock(spec=['setDevice'])
        self.controller = AudioOutputController(
            self.output, settings=self.settings, media_devices=self.devices)

    def tearDown(self):
        self.controller.deleteLater()
        self.devices.deleteLater()
        self.app.processEvents()
        self.settings.sync()
        self.temporary.cleanup()

    def test_initial_system_default_and_explicit_choice_bind_expected_device(self):
        self.output.setDevice.assert_called_once_with(self.speaker)
        self.assertEqual(self.controller.preferred_id, '')
        self.assertIn('系统默认', self.controller.description())
        self.assertTrue(self.controller.choose(device_key(self.headphones)))
        self.output.setDevice.assert_called_with(self.headphones)
        self.assertEqual(self.controller.active_device, self.headphones)
        self.assertNotIn('系统默认', self.controller.description())

    def test_duplicate_display_names_do_not_merge_devices_or_change_identity(self):
        self.headphones.name = self.speaker.name
        self.assertTrue(self.controller.choose(device_key(self.headphones)))
        self.output.setDevice.assert_called_with(self.headphones)
        self.assertNotEqual(device_key(self.speaker), device_key(self.headphones))
        renamed = FakeDevice(self.headphones.identifier, '重命名的耳机')
        self.output.reset_mock()
        self.devices.change([self.speaker, renamed], self.speaker)
        self.assertIs(self.controller.active_device, renamed)
        self.assertEqual(self.controller.description(), '重命名的耳机')
        self.output.setDevice.assert_not_called()

    def test_device_choice_persists_in_an_isolated_ini_and_survives_restart(self):
        self.controller.choose(device_key(self.headphones))
        self.settings.sync()
        reopened = QSettings(self.settings_path, QSettings.Format.IniFormat)
        self.assertEqual(reopened.value('audio_output_device_id'),
                         self.headphones.identifier.hex())
        self.assertEqual(reopened.value('audio_output_name'), '耳机')
        other_output = Mock(spec=['setDevice'])
        restored = AudioOutputController(other_output, settings=reopened,
                                         media_devices=self.devices)
        other_output.setDevice.assert_called_once_with(self.headphones)
        self.assertEqual(restored.preferred_id, device_key(self.headphones))
        restored.deleteLater()

    def test_unplug_falls_back_without_forgetting_preference_then_reconnects(self):
        preferred = device_key(self.headphones)
        self.controller.choose(preferred)
        self.output.reset_mock()
        self.devices.change([self.speaker], self.speaker)
        self.output.setDevice.assert_called_once_with(self.speaker)
        self.assertTrue(self.controller.unavailable)
        self.assertEqual(self.controller.preferred_id, preferred)
        self.assertEqual(self.settings.value('audio_output_device_id'), preferred)
        self.assertEqual(self.settings.value('audio_output_name'), '耳机')
        self.assertIn('未连接', self.controller.description())
        reconnected = FakeDevice(self.headphones.identifier, '耳机（重新连接）')
        self.output.reset_mock()
        self.devices.change([self.speaker, reconnected], self.speaker)
        self.output.setDevice.assert_called_once_with(reconnected)
        self.assertFalse(self.controller.unavailable)
        self.assertEqual(self.controller.preferred_id, preferred)

    def test_system_default_follows_new_default_and_does_not_rebind_same_id(self):
        self.output.reset_mock()
        self.devices.change([self.speaker, self.headphones], self.headphones)
        self.output.setDevice.assert_called_once_with(self.headphones)
        self.output.reset_mock()
        same_device_new_object = FakeDevice(self.headphones.identifier, '耳机新名称')
        self.devices.change([self.speaker, same_device_new_object], same_device_new_object)
        self.controller.refresh()
        self.output.setDevice.assert_not_called()
        self.assertEqual(self.controller.description(), '系统默认：耳机新名称')

    def test_explicit_device_is_unchanged_when_system_default_switches(self):
        self.controller.choose(device_key(self.headphones))
        self.output.reset_mock()
        different_default = FakeDevice(b'usb', 'USB 输出')
        self.devices.change([self.speaker, self.headphones, different_default], different_default)
        self.output.setDevice.assert_not_called()
        self.assertIs(self.controller.active_device, self.headphones)

    def test_choosing_system_default_clears_stored_preference_and_resumes_following(self):
        self.controller.choose(device_key(self.headphones))
        self.output.reset_mock()
        self.assertTrue(self.controller.choose(''))
        self.output.setDevice.assert_called_once_with(self.speaker)
        self.assertEqual(self.settings.value('audio_output_device_id'), '')
        self.assertEqual(self.settings.value('audio_output_name'), '')
        self.assertFalse(self.controller.unavailable)
        self.devices.change([self.speaker, self.headphones], self.headphones)
        self.output.setDevice.assert_called_with(self.headphones)

    def test_missing_device_from_stale_menu_is_rejected_without_overwriting_preference(self):
        self.controller.choose(device_key(self.speaker))
        self.devices.devices = [self.speaker]
        self.output.reset_mock()
        self.assertFalse(self.controller.choose(device_key(self.headphones)))
        self.assertEqual(self.controller.preferred_id, device_key(self.speaker))
        self.assertEqual(self.settings.value('audio_output_device_id'), device_key(self.speaker))
        self.output.setDevice.assert_not_called()

    def test_no_hardware_has_a_clear_status_and_does_not_crash(self):
        no_device = FakeDevice()
        self.output.reset_mock()
        self.devices.change([], no_device)
        self.output.setDevice.assert_called_once_with(no_device)
        self.assertEqual(self.controller.devices, [])
        self.assertIn('暂无可用设备', self.controller.description())
        self.output.reset_mock()
        self.devices.change([FakeDevice()], no_device)
        self.assertEqual(self.controller.devices, [])
        self.output.setDevice.assert_not_called()

    def test_invalid_saved_device_identifiers_fall_back_to_default(self):
        for invalid in (123, ['device'], {'id': 'aa'}, 'aa-bb', 'AABB', 'a', 'xyz', None):
            with self.subTest(invalid=invalid):
                self.settings.setValue('audio_output_device_id', invalid)
                output = Mock(spec=['setDevice'])
                controller = AudioOutputController(output, settings=self.settings,
                                                   media_devices=self.devices)
                self.assertEqual(controller.preferred_id, '')
                self.assertFalse(controller.unavailable)
                output.setDevice.assert_called_once_with(self.speaker)
                controller.deleteLater()

    def test_saved_missing_device_falls_back_and_reconnects_after_app_restart(self):
        self.settings.setValue('audio_output_device_id', device_key(self.headphones))
        self.settings.setValue('audio_output_name', '耳机')
        offline = FakeMediaDevices([self.speaker], self.speaker)
        output = Mock(spec=['setDevice'])
        controller = AudioOutputController(output, settings=self.settings, media_devices=offline)
        self.assertTrue(controller.unavailable)
        output.setDevice.assert_called_once_with(self.speaker)
        offline.change([self.speaker, self.headphones], self.speaker)
        output.setDevice.assert_called_with(self.headphones)
        self.assertFalse(controller.unavailable)
        controller.deleteLater()
        offline.deleteLater()

    def test_controller_can_be_used_without_persistent_settings(self):
        output = Mock(spec=['setDevice'])
        controller = AudioOutputController(output, media_devices=self.devices)
        self.assertTrue(controller.choose(device_key(self.headphones)))
        output.setDevice.assert_called_with(self.headphones)
        controller.deleteLater()


if __name__ == '__main__':
    unittest.main()
