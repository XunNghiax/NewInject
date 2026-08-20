import sys
from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtCore import Qt

# Nhập khẩu giao diện từ gui.py cũ (đảm bảo file gui.py phải nằm cùng thư mục)
from ui.gui import CapCutInjectorGUI

class AdvancedToolsPanel(QWidget):
    """
    Lớp đóng gói toàn bộ giao diện từ gui.py cũ thành một QWidget duy nhất.
    Cho phép nhúng vào gui_v2.py mà không làm hỏng code gốc.
    """
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Khởi tạo instance của giao diện cũ
        self.legacy_gui = CapCutInjectorGUI()
        
        # Bỏ đi các thuộc tính của Window (như thanh tiêu đề) để biến nó thành Widget
        self.legacy_gui.setWindowFlags(Qt.WindowType.Widget)
        
        # Nhúng thẳng giao diện cũ vào layout hiện tại
        layout.addWidget(self.legacy_gui)
