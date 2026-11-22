"""试听输出路由：只影响 DJTOOL，不改 macOS 或 Serato 的音频设置。"""
import re

from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QMediaDevices


def device_key(device):
    """Qt 的设备 ID 是不透明字节；按十六进制保存，避免按显示名字认设备。"""
    return bytes(device.id()).hex()


class AudioOutputController(QObject):
    changed = Signal()

    def __init__(self, output, parent=None, *, settings=None, media_devices=None):
        super().__init__(parent)
        self.output, self.settings = output, settings
        self.media_devices = media_devices if media_devices is not None else QMediaDevices(self)
        preferred = settings.value('audio_output_device_id', '') if settings is not None else ''
        self.preferred_id = preferred if isinstance(preferred, str) and re.fullmatch(r'(?:[0-9a-f]{2})*', preferred) else ''
        self.preferred_name = str(settings.value('audio_output_name', '')) if settings is not None else ''
        self.devices = []
        self.default_device = None
        self.active_device = None
        self.unavailable = False
        self._applied_id = None
        self.media_devices.audioOutputsChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.devices = [device for device in self.media_devices.audioOutputs() if device_key(device)]
        self.default_device = self.media_devices.defaultAudioOutput()
        selected = next((device for device in self.devices if device_key(device) == self.preferred_id), None)
        self.unavailable = bool(self.preferred_id and selected is None)
        # 临时拔出时保留偏好，回退系统默认；同一设备重连后自动恢复。
        self.active_device = selected if selected is not None else self.default_device
        active_id = device_key(self.active_device)
        if active_id != self._applied_id:
            self.output.setDevice(self.active_device)
            self._applied_id = active_id
        self.changed.emit()

    def choose(self, identifier):
        # 菜单出现到点击之间设备可能已拔出；再次读列表，拒绝过期选项。
        devices = self.media_devices.audioOutputs()
        selected = next((device for device in devices if device_key(device) == identifier), None)
        if identifier and selected is None:
            self.refresh()
            return False
        self.preferred_id = identifier
        self.preferred_name = selected.description() if selected is not None else ''
        if self.settings is not None:
            self.settings.setValue('audio_output_device_id', self.preferred_id)
            self.settings.setValue('audio_output_name', self.preferred_name)
        self.refresh()
        return True

    def description(self):
        name = self.active_device.description() or '暂无可用设备'
        if self.unavailable:
            return f'上次选择的设备未连接，当前使用系统默认：{name}'
        return ('系统默认：' if not self.preferred_id else '') + name
