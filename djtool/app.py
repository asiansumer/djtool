"""应用入口；测试可以指定隔离曲库，日常使用记住用户选择的位置。"""

import argparse
import sys
from PySide6.QtCore import QMetaObject, QSettings, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication


def configure_application(app):
    # macOS 退出时需要等音频线程清理。通过 Qt 队列调用原生 quit，
    # 先让 Python 回调释放 GIL，避免音频线程与关闭窗口互相等待。
    app.setQuitOnLastWindowClosed(False)
    app.lastWindowClosed.connect(
        lambda: QMetaObject.invokeMethod(app, 'quit', Qt.ConnectionType.QueuedConnection)
    )
    app.setStyle('Fusion')
    app.setFont(QFont('.AppleSystemUIFont', 13))
    from djtool.ui import theme
    app.setWindowIcon(QIcon(str(theme.ASSETS / 'app-icon.svg')))
    theme.apply(app, theme.load(QSettings('DJTOOL', 'DJTOOL')))


def main():
    if len(sys.argv)==3 and sys.argv[1]=='--check-runtime':
        from djtool.runtime_check import run
        return run(sys.argv[2])
    if len(sys.argv)>=4 and sys.argv[1]=='--check-download':
        from djtool.download_check import run
        return run(sys.argv[2], sys.argv[3:])
    parser = argparse.ArgumentParser(description='DJTOOL：收歌、整理曲库、编排 Set')
    parser.add_argument('--library', help='打开指定曲库；不更改日常曲库设置')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName('DJTOOL')
    app.setOrganizationName('DJTOOL')
    configure_application(app)
    from djtool.ui.main_window import MainWindow
    window = MainWindow(args.library, remember=not args.library)
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
