"""Launch the local PDF tool."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("PDF 工具")
    app.setOrganizationName("LocalPdf")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = MainWindow()
    window.resize(1280, 820)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    main()
