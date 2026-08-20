import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from ui.gui_v2 import MainWindowV2

def main():
    app = QApplication(sys.argv)
    window = MainWindowV2()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
