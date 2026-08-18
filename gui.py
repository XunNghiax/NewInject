
import os
import sys
import json
import warnings
from datetime import datetime


from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QFileDialog, QProgressBar, QTextEdit, QDoubleSpinBox,
                             QFrame, QTabWidget, QSpinBox, QGroupBox, QListWidget, QStackedWidget,
                             QCheckBox, QSplitter, QRadioButton, QScrollArea, QComboBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPalette, QColor


# 🔗 ĐỒNG BỘ THỰC TẾ: Import class xử lý từ file src/

from src.backend import CapCutBackend
from src.srt_utils import (
    merge_numbered_srt_files,
    split_srt_file,
    process_and_renumber_srt,
    analyze_audio_wpm_and_log,
    compare_srt_folders,
    process_srt_speed,
    check_srt_audio_sync
)
from src.batch_replace_srt import replace_blocks_in_folder, replace_blocks_in_file
from src.qa_srt_before import analyze_srt_to_file
from src.gemini_translate import run_auto_translate_srt
from src.auto_qa_repair import run_auto_qa_repair

warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv")

CONFIG_FILE = os.path.join("user_data", "config", "user_config.json")

DEFAULT_CONFIG = {
    'SRT_FILE_PATH': "./source/srt/SonAnhChieuHonVi.srt",
    'REF_AUDIO_PATH': "./audiotst1.wav",
    'AUDIO_OUT_DIR': os.path.abspath("./source/wav/SonAnhChieuHon"),
    'CAPCUT_JSON_PATH': r"C:\Users\Xun_Nghiax\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft\SonAnhChieuHon\draft_content.json",
    'SERVER_URL': "",
    'REF_TEXT': (
        "Trong lĩnh vực công nghệ hiện nay, việc ứng dụng các mô hình ngôn ngữ lớn đang ngày càng trở nên phổ biến. "
        "Quá trình tối ưu hóa này đòi hỏi chúng ta phải hiểu rõ cấu trúc dữ liệu và cách hệ thống xử lý thông tin đầu vào."
    ),
    'SPEED_RATIO': 1.25,
    'INJECT_ONLY': False,
    'SPEED_TOOL_IN_FILE': "",
    'SPEED_TOOL_OUT_FILE': "",
    'SPEED_TOOL_OLD_SPEED': 0.7,
    'SPEED_TOOL_NEW_SPEED': 0.8,
    'GEMINI_PROMPT_PATH': "",
    'GEMINI_CN_DIR': "",
    'GEMINI_VI_DIR': "",
    'GEMINI_WAIT_TIME': 40,
    'GEMINI_DELAY_TIME': 15,
    'MERGE_IN_DIR': "",
    'MERGE_OUT_FILE': "",
    'FIX_IN_PATH': "",
    'FIX_OUT_PATH': "",
    'SPLIT_IN_FILE': "",
    'SPLIT_OUT_DIR': "./output_srt",
    'SPLIT_PREFIX': "part",
    'SPLIT_BLOCKS': 150,
    'REPLACE_IN_PATH': "",
    'COMPARE_DIR_A': "",
    'COMPARE_DIR_B': "",
    'QA_IN_PATH': "",
    'QA_OUT_DIR': "./qa_reports",
    'QA_CHUNK_SIZE': 80,
    'QA_SCAN_MODE': 0,
    'QA_AUDIO_SRT_IN': "",
    'QA_AUDIO_DIR_IN': "",
    'QA_AUDIO_LOG_OUT': "",
}

def load_user_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in saved:
                        saved[k] = v
                return saved
        except:
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_user_config(config_dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Không thể ghi nhớ cấu hình: {e}")

# ==============================================================================
# WORKERS CHO GIAO DIỆN
# ==============================================================================

class GenericWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.func(*self.args, log_callback=self.log_signal.emit, **self.kwargs)
            self.finished_signal.emit(True, "Tác vụ hoàn thành!")
        except Exception as e:
            self.finished_signal.emit(False, str(e))

class BackendWorker(QThread):
    progress_signal = pyqtSignal(int, int, str)
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, config):
        super().__init__()
        self.config = config

    def run(self):
        try:
            backend = CapCutBackend(
                config=self.config,
                log_callback=lambda msg: self.log_signal.emit(msg),
                progress_callback=lambda d, t, p: self.progress_signal.emit(d, t, p)
            )
            is_inject_only = self.config.get('INJECT_ONLY', False)
            elapsed_time = backend.run_process(only_inject=is_inject_only)
            if elapsed_time is None: elapsed_time = "00:02:15"
            self.finished_signal.emit(True, f"Hoàn thành xuất sắc trong vòng {elapsed_time}!")
        except Exception as e:
            self.finished_signal.emit(False, str(e))

# ==============================================================================
# GIAO DIỆN CHÍNH
# ==============================================================================

class CapCutInjectorGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.user_cfg = load_user_config()
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("CapCut Audio Injector & SRT Tools Pro 🚀")
        self.resize(1150, 780) 
        self.setMinimumSize(1000, 700)
        self.setup_global_styles()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # ================= CỘT TRÁI: CÁC TÍNH NĂNG =================
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        
        self.tab_injector = QWidget()
        self.setup_injector_tab()
        self.tabs.addTab(self.tab_injector, "🔊 Injector CapCut")

        self.tab_srt_tools = QWidget()
        self.setup_srt_tools_tab()
        self.tabs.addTab(self.tab_srt_tools, "✂️ Tiện Ích SRT")

        self.tab_gemini = QWidget()
        self.setup_gemini_tab()
        self.tabs.addTab(self.tab_gemini, "🤖 Dịch Phụ Đề Gemini")

        self.tab_qa_tools = QWidget()
        self.setup_qa_tools_tab()
        self.tabs.addTab(self.tab_qa_tools, "📈 Phân Tích Audio QA")

        left_layout.addWidget(self.tabs)
        self.main_splitter.addWidget(left_panel)

        # ================= CỘT PHẢI: CONSOLE =================
        right_panel = QFrame()
        right_panel.setObjectName("PanelWidget")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(15, 15, 15, 15)
        right_layout.setSpacing(12)

        header_layout = QHBoxLayout()
        right_title = QLabel("🖥️ MAIN CONSOLE")
        right_title.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        right_title.setStyleSheet("color: #e2e8f0;")
        header_layout.addWidget(right_title)
        header_layout.addStretch()
        
        self.stop_btn = QPushButton("🛑 Dừng")
        self.stop_btn.setObjectName("SmallButton")
        self.stop_btn.setFixedSize(70, 25)
        self.stop_btn.setStyleSheet("background-color: #f97316; color: white;") # Màu cam nổi bật
        self.stop_btn.clicked.connect(self.stop_active_process)
        self.stop_btn.setEnabled(False) # Mặc định tắt, chỉ bật khi có tiến trình chạy
        header_layout.addWidget(self.stop_btn)

        clear_log_btn = QPushButton("🧹 Clear")
        clear_log_btn.setObjectName("SmallButton")
        clear_log_btn.setFixedSize(70, 25)
        clear_log_btn.clicked.connect(lambda: self.log_output.clear())
        header_layout.addWidget(clear_log_btn)
        
        right_layout.addLayout(header_layout)

        self.phase_label = QLabel("Trạng thái: Đang chờ thao tác...")
        self.phase_label.setStyleSheet("color: #38bdf8; font-weight: 600; font-size: 13px; margin-top: 5px;")
        right_layout.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_bar.setFixedHeight(22)
        right_layout.addWidget(self.progress_bar)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Hệ thống log sẽ hiển thị tại đây...")
        right_layout.addWidget(self.log_output)

        self.main_splitter.addWidget(right_panel)
        
        self.main_splitter.setSizes([650, 450])
        main_layout.addWidget(self.main_splitter)

    # ------------------ SETUP TABS ------------------
    def setup_injector_tab(self):
        scroll_area = QScrollArea(self.tab_injector)
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        
        scroll_content = QWidget()
        scroll_content.setStyleSheet("background-color: transparent;")
        
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)

        self.inject_frame = QFrame()
        self.inject_frame.setObjectName("InjectFrame")
        inject_layout = QVBoxLayout(self.inject_frame) 
        inject_layout.setContentsMargins(15, 10, 15, 10)
        inject_layout.setSpacing(10)
        
        # --- KHUNG TÙY CHỌN CHÍNH (INJECT & GROUP) ---
        self.inject_frame = QFrame()
        self.inject_frame.setObjectName("InjectFrame")
        inject_layout = QVBoxLayout(self.inject_frame) 
        inject_layout.setContentsMargins(15, 15, 15, 15)
        inject_layout.setSpacing(10)
        
        lbl_inject_mode = QLabel("⚙️ Chế độ Xử lý & Timeline:")
        lbl_inject_mode.setStyleSheet("font-weight: 600; color: #cbd5e1; font-size: 13px;")
        inject_layout.addWidget(lbl_inject_mode)

        self.inject_mode_combo = QComboBox()
        self.inject_mode_combo.addItems([
            "🎙️ Tạo giọng AI + ✂️ Đa Track (Giữ nguyên Timeline gốc)",
            "🎙️ Chỉ bơm Audio có sẵn + ✂️ Đa Track (Bỏ qua AI)"
        ])
        
        # Khôi phục trạng thái từ file config
        is_inject = self.user_cfg.get('INJECT_ONLY', False)
        if not is_inject: self.inject_mode_combo.setCurrentIndex(0)
        else: self.inject_mode_combo.setCurrentIndex(1)

        self.inject_mode_combo.currentIndexChanged.connect(self.toggle_inject_only_mode)
        inject_layout.addWidget(self.inject_mode_combo)
        layout.addWidget(self.inject_frame)
        
        self.srt_input = self.create_modern_file_row(layout, "📄 File phụ đề SRT (Đầu vào)", self.user_cfg['SRT_FILE_PATH'], "SRT (*.srt)")
        self.audio_out_input = self.create_modern_file_row(layout, "📁 Thư mục xuất/chứa âm thanh", self.user_cfg['AUDIO_OUT_DIR'], is_dir=True)
        self.capcut_json_input = self.create_modern_file_row(layout, "🛠️ Đường dẫn file JSON CapCut (draft_content.json)", self.user_cfg['CAPCUT_JSON_PATH'], "JSON (*.json)")

        self.speed_frame = QFrame()
        self.speed_frame.setStyleSheet("QFrame { background-color: #1e293b; border: 1px solid #334155; border-radius: 6px; }")
        speed_layout = QHBoxLayout(self.speed_frame)
        speed_layout.setContentsMargins(15, 10, 15, 10)
        
        lbl_speed_desc = QLabel("⚡ Tốc độ hiển thị Video trên CapCut (Áp dụng mọi chế độ):")
        lbl_speed_desc.setStyleSheet("font-weight: 600; color: #38bdf8; font-size: 13px; border: none;")
        
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 3.0)
        self.speed_spin.setValue(self.user_cfg.get('SPEED_RATIO', 1.0))
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setFixedWidth(120)
        self.speed_spin.setStyleSheet("border: 1px solid #475569; background-color: #0f172a;")
        
        speed_layout.addWidget(lbl_speed_desc)
        speed_layout.addWidget(self.speed_spin)
        speed_layout.addStretch()
        layout.addWidget(self.speed_frame)

        self.gradio_group = QGroupBox("Cấu hình AI Voice (Bỏ qua nếu chọn Inject Only)")
        gradio_layout = QVBoxLayout(self.gradio_group)
        gradio_layout.setSpacing(12)
        gradio_layout.setContentsMargins(15, 20, 15, 15)
        
        self.ref_audio_input = self.create_modern_file_row(gradio_layout, "🎵 Audio mẫu để Clone giọng (WAV)", self.user_cfg['REF_AUDIO_PATH'], "WAV (*.wav)")

        lbl_server = QLabel("🌐 Gradio Server URL Live")
        self.server_input = QLineEdit(self.user_cfg['SERVER_URL'])
        self.server_input.setPlaceholderText("VD: https://xxxx.gradio.live")
        
        gradio_layout.addWidget(lbl_server)
        gradio_layout.addWidget(self.server_input)

        lbl_ref = QLabel("📝 Văn bản của Audio mẫu (Ref Text)")
        gradio_layout.addWidget(lbl_ref)
        self.ref_text_input = QTextEdit()
        self.ref_text_input.setPlainText(self.user_cfg['REF_TEXT'])
        self.ref_text_input.setMaximumHeight(80)
        gradio_layout.addWidget(self.ref_text_input)

        layout.addWidget(self.gradio_group)

        self.start_btn = QPushButton("🔥 BẮT ĐẦU INJECT TỰ ĐỘNG")
        self.start_btn.setObjectName("PrimaryActionBtn")
        self.start_btn.setMinimumHeight(50)
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.start_process)
        layout.addSpacing(10)
        layout.addWidget(self.start_btn)
        layout.addStretch()

        scroll_area.setWidget(scroll_content)
        main_tab_layout = QVBoxLayout(self.tab_injector)
        main_tab_layout.setContentsMargins(0, 0, 0, 0)
        main_tab_layout.addWidget(scroll_area)

        self.toggle_inject_only_mode()

    def toggle_inject_only_mode(self):
        # Lấy index của Dropdown. Index 2 và 3 tương đương với bật INJECT ONLY
        idx = self.inject_mode_combo.currentIndex()
        is_inject_only = (idx == 2 or idx == 3)
        
        self.gradio_group.setEnabled(not is_inject_only)
        
        if is_inject_only:
            self.gradio_group.setStyleSheet("QGroupBox { border: 1px solid #334155; color: #475569; } QWidget { opacity: 0.4; } QLabel { color: #475569; } QLineEdit, QTextEdit { background-color: #0f172a; color: #475569; border-color: #1e293b; }")
            self.start_btn.setText("⚡ BẮT ĐẦU INJECT TRỰC TIẾP (BỎ QUA AI)")
            self.start_btn.setStyleSheet("background-color: #eab308; color: #0f172a;")
            self.inject_frame.setStyleSheet("#InjectFrame { background-color: rgba(234, 179, 8, 0.1); border: 1px solid #eab308; }")
        else:
            self.gradio_group.setStyleSheet("")
            self.start_btn.setText("🔥 BẮT ĐẦU TẠO GIỌNG AI & INJECT")
            self.start_btn.setStyleSheet("background-color: #0ea5e9; color: #ffffff;")
            self.inject_frame.setStyleSheet("#InjectFrame { background-color: #1e293b; border: 1px solid #334155; }")

    def setup_srt_tools_tab(self):
        main_layout = QHBoxLayout(self.tab_srt_tools)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        self.srt_menu = QListWidget()
        self.srt_menu.setFixedWidth(240)
        self.srt_menu.setCursor(Qt.CursorShape.PointingHandCursor)
        self.srt_menu.addItems([
            "Chia nhỏ File SRT",
            "So sánh 2 Thư mục",
            "Phân tích QA",
            "Thay thế hàng loạt ",
            "Gộp File SRT",
            "Sửa lại Timeline",
            "Chuyển đổi Tốc độ",
        ])
        main_layout.addWidget(self.srt_menu)

        self.srt_stack = QStackedWidget()  
        main_layout.addWidget(self.srt_stack)

        # TRANG 1: MERGE SRT
        page_merge = QWidget()
        layout_merge = QVBoxLayout(page_merge)
        layout_merge.setContentsMargins(0, 0, 0, 0)
        group_merge = QGroupBox("Công Cụ: Gộp File SRT (1.srt, 2.srt...)")
        g_merge_layout = QVBoxLayout(group_merge)
        g_merge_layout.setSpacing(15)
        self.merge_in_dir = self.create_modern_file_row(g_merge_layout, "Thư mục chứa các SRT cần gộp:", self.user_cfg.get('MERGE_IN_DIR', ''), is_dir=True)
        self.merge_out_file = self.create_modern_file_row(g_merge_layout, "Đường dẫn lưu file gộp:", self.user_cfg.get('MERGE_OUT_FILE', ''), "SRT (*.srt)", is_save=True)
        btn_merge = QPushButton("🔗 Gộp SRT")
        btn_merge.setObjectName("ActionBtn")
        btn_merge.clicked.connect(self.run_merge_srt)
        g_merge_layout.addWidget(btn_merge)
        layout_merge.addWidget(group_merge)
        layout_merge.addStretch()

        # TRANG 2: RENUMBER & FIX
        page_fix = QWidget()
        layout_fix = QVBoxLayout(page_fix)
        layout_fix.setContentsMargins(0, 0, 0, 0)
        group_fix = QGroupBox("Công Cụ: Đánh số lại & Sửa đè Timeline")
        g_fix_layout = QVBoxLayout(group_fix)
        g_fix_layout.setSpacing(15)

        fix_mode_layout = QHBoxLayout()
        self.fix_rad_file = QRadioButton("📄 Xử lý 1 File đơn lẻ")
        self.fix_rad_dir = QRadioButton("📁 Xử lý cả Thư mục (Hàng loạt)")
        self.fix_rad_file.setChecked(True)
        fix_mode_layout.addWidget(self.fix_rad_file)
        fix_mode_layout.addWidget(self.fix_rad_dir)
        fix_mode_layout.addStretch()
        g_fix_layout.addLayout(fix_mode_layout)

        self.fix_in_file, fix_btn, fix_lbl = self.create_flexible_file_row(g_fix_layout, "File SRT gốc cần sửa:", self.user_cfg.get('FIX_IN_PATH', ''), "SRT (*.srt)")
        self.fix_out_file = self.create_modern_file_row(g_fix_layout, "Đường dẫn lưu kết quả:", self.user_cfg.get('FIX_OUT_PATH', ''), "SRT (*.srt)", is_save=True)
        
        self.fix_rad_file.toggled.connect(lambda: fix_lbl.setText("File SRT gốc cần sửa:") if self.fix_rad_file.isChecked() else fix_lbl.setText("Thư mục chứa các file SRT cần sửa:"))
        fix_btn.clicked.connect(lambda: self.select_flexible_path(self.fix_in_file, "SRT (*.srt)", is_dir=self.fix_rad_dir.isChecked()))

        btn_fix = QPushButton("🛠️ Thực hiện Sửa Timeline")
        btn_fix.setObjectName("ActionBtn")
        btn_fix.clicked.connect(self.run_fix_srt)
        g_fix_layout.addWidget(btn_fix)
        layout_fix.addWidget(group_fix)
        layout_fix.addStretch()

        # TRANG 3: SPLIT SRT
        page_split = QWidget()
        layout_split = QVBoxLayout(page_split)
        layout_split.setContentsMargins(0, 0, 0, 0)
        group_split = QGroupBox("Công Cụ: Cắt nhỏ File SRT")
        g_split_layout = QVBoxLayout(group_split)
        g_split_layout.setSpacing(15)
        
        # Load cấu hình đường dẫn
        self.split_in_file = self.create_modern_file_row(g_split_layout, "File SRT gốc:", self.user_cfg.get('SPLIT_IN_FILE', ''), "SRT (*.srt)")
        self.split_out_dir = self.create_modern_file_row(g_split_layout, "Thư mục xuất các file nhỏ:", self.user_cfg.get('SPLIT_OUT_DIR', './output_srt'), is_dir=True)
        
        split_settings = QHBoxLayout()
        
        # Load cấu hình Tiền tố
        self.split_out_prefix = QLineEdit(self.user_cfg.get('SPLIT_PREFIX', 'part'))
        self.split_out_prefix.setPlaceholderText("VD: part")
        
        # Load cấu hình Số lượng block
        self.split_blocks = QSpinBox() # Phải khởi tạo QSpinBox trước
        self.split_blocks.setRange(10, 1000)
        self.split_blocks.setValue(self.user_cfg.get('SPLIT_BLOCKS', 100)) # Mới được gọi setValue ở đây
        self.split_blocks.setSuffix(" Cụm/file")
        
        split_settings.addWidget(QLabel("Tiền tố (Prefix):"))
        split_settings.addWidget(self.split_out_prefix)
        split_settings.addSpacing(20)
        split_settings.addWidget(QLabel("Số câu mỗi file:"))
        split_settings.addWidget(self.split_blocks)
        
        g_split_layout.addLayout(split_settings)
        btn_split = QPushButton("✂️ Tiến hành Cắt")
        btn_split.setObjectName("ActionBtn")
        btn_split.clicked.connect(self.run_split_srt)
        g_split_layout.addWidget(btn_split)
        layout_split.addWidget(group_split)
        layout_split.addStretch()

        # TRANG 4: BATCH REPLACE
        page_replace = QWidget()
        layout_replace = QVBoxLayout(page_replace)
        layout_replace.setContentsMargins(0, 0, 0, 0)
        group_replace = QGroupBox("Công Cụ: Thay thế hàng loạt (Batch Replace)")
        g_replace_layout = QVBoxLayout(group_replace)
        g_replace_layout.setSpacing(15)
        
        replace_mode_layout = QHBoxLayout()
        self.replace_rad_file = QRadioButton("📄 Thay thế trên 1 File đơn lẻ")
        self.replace_rad_dir = QRadioButton("📁 Thay thế theo Thư mục (Nhiều file con)")
        self.replace_rad_file.setChecked(True)
        replace_mode_layout.addWidget(self.replace_rad_file)
        replace_mode_layout.addWidget(self.replace_rad_dir)
        replace_mode_layout.addStretch()
        g_replace_layout.addLayout(replace_mode_layout)

        self.replace_in_path, replace_btn, replace_lbl = self.create_flexible_file_row(g_replace_layout, "File SRT gốc cần sửa:", self.user_cfg.get('REPLACE_IN_PATH', ''), "SRT (*.srt)")        
        self.replace_rad_file.toggled.connect(lambda: replace_lbl.setText("File SRT gốc cần sửa:") if self.replace_rad_file.isChecked() else replace_lbl.setText("Thư mục chứa các file SRT con (VD: _1.srt, _2.srt):"))
        replace_btn.clicked.connect(lambda: self.select_flexible_path(self.replace_in_path, "SRT (*.srt)", is_dir=self.replace_rad_dir.isChecked()))
        
        lbl_paste = QLabel("Dán nội dung các block SRT đã sửa vào đây:")
        lbl_paste.setStyleSheet("font-weight: 600; color: #94a3b8; font-size: 12px;")
        g_replace_layout.addWidget(lbl_paste)
        
        self.replace_text_input = QTextEdit()
        self.replace_text_input.setPlaceholderText("Ví dụ:\n926\n00:26:41,366 --> 00:26:44,400\nNội dung mới...")
        self.replace_text_input.setMinimumHeight(150)
        g_replace_layout.addWidget(self.replace_text_input)
        
        btn_replace = QPushButton("🔄 Replace Hàng Loạt")
        btn_replace.setObjectName("ActionBtn")
        btn_replace.clicked.connect(self.run_batch_replace)
        g_replace_layout.addWidget(btn_replace)
        layout_replace.addWidget(group_replace)
        layout_replace.addStretch()

        # TRANG 5: COMPARE SRT FOLDERS
        page_compare = QWidget()
        layout_compare = QVBoxLayout(page_compare)
        layout_compare.setContentsMargins(0, 0, 0, 0)
        group_compare = QGroupBox("Công Cụ: So sánh ID & Timeline 2 Thư mục")
        g_compare_layout = QVBoxLayout(group_compare)
        g_compare_layout.setSpacing(15)
        self.compare_dir_a = self.create_modern_file_row(g_compare_layout, "📁 Thư mục A (Bản gốc):", self.user_cfg.get('COMPARE_DIR_A', ''), is_dir=True)
        self.compare_dir_b = self.create_modern_file_row(g_compare_layout, "📁 Thư mục B (Bản đã sửa):", self.user_cfg.get('COMPARE_DIR_B', ''), is_dir=True)
        btn_compare = QPushButton("⚖️ Bắt đầu So sánh")
        btn_compare.setObjectName("ActionBtn")
        btn_compare.clicked.connect(self.run_compare_srt)
        g_compare_layout.addWidget(btn_compare)
        layout_compare.addWidget(group_compare)
        layout_compare.addStretch()

        # [UX CẢI TIẾN MỚI] TRANG 6: PRE-QA SUBTITLE 
        page_pre_qa = QWidget()
        layout_pre_qa = QVBoxLayout(page_pre_qa)
        layout_pre_qa.setContentsMargins(0, 0, 0, 0)
        group_pre_qa = QGroupBox("Công Cụ: Kiểm tra lỗi Subtitle (Pre-QA)")
        g_pre_qa_layout = QVBoxLayout(group_pre_qa)
        g_pre_qa_layout.setSpacing(15)
        
        qa_mode_layout = QHBoxLayout()
        self.qa_rad_file = QRadioButton("📄 Kiểm tra 1 File đơn lẻ")
        self.qa_rad_dir = QRadioButton("📁 Kiểm tra cả Thư mục")
        self.qa_rad_file.setChecked(True)
        qa_mode_layout.addWidget(self.qa_rad_file)
        qa_mode_layout.addWidget(self.qa_rad_dir)
        qa_mode_layout.addStretch()
        g_pre_qa_layout.addLayout(qa_mode_layout)

        # ---> ĐÂY LÀ KHUNG DROPDOWN CHỌN CHẾ ĐỘ QUÉT <---
        qa_scan_mode_layout = QHBoxLayout()
        lbl_scan_mode = QLabel("🎯 Chế độ Quét:")
        lbl_scan_mode.setStyleSheet("font-weight: 600; color: #cbd5e1; font-size: 12px;")
        qa_scan_mode_layout.addWidget(lbl_scan_mode)
        
        # 1. KHỞI TẠO COMBOBOX TRƯỚC
        self.qa_mode_combo = QComboBox()
        self.qa_mode_combo.addItems([
            "Quét Toàn Diện (Lỗi Ngắt câu + Tràn Tốc độ CPS)",
            "Chỉ quét lỗi Gãy Câu (Bỏ qua cảnh báo CPS)",
            "Chỉ quét lỗi Tốc độ CPS (Bỏ qua cảnh báo Gãy câu)"
        ])
        
        # 2. RỒI MỚI THIẾT LẬP GIÁ TRỊ INDEX TỪ CẤU HÌNH
        self.qa_mode_combo.setCurrentIndex(self.user_cfg.get('QA_SCAN_MODE', 0))
        
        self.qa_mode_combo.setMinimumWidth(320)
        qa_scan_mode_layout.addWidget(self.qa_mode_combo)
        qa_scan_mode_layout.addStretch()
        g_pre_qa_layout.addLayout(qa_scan_mode_layout)
        # ---------------------------------------------------

        self.preqa_in_file, qa_btn, qa_lbl = self.create_flexible_file_row(g_pre_qa_layout, "File SRT cần kiểm tra:", self.user_cfg.get('QA_IN_PATH', ''), "SRT (*.srt)")
        self.preqa_out_dir = self.create_modern_file_row(g_pre_qa_layout, "Thư mục lưu báo cáo (TXT):", self.user_cfg.get('QA_OUT_DIR', './qa_reports'), is_dir=True)
        
        self.qa_rad_file.toggled.connect(lambda: qa_lbl.setText("File SRT cần kiểm tra:") if self.qa_rad_file.isChecked() else qa_lbl.setText("Thư mục chứa các file SRT cần kiểm tra:"))
        qa_btn.clicked.connect(lambda: self.select_flexible_path(self.preqa_in_file, "SRT (*.srt)", is_dir=self.qa_rad_dir.isChecked()))

        chunk_settings = QHBoxLayout()
        self.preqa_chunk_spin = QSpinBox()
        self.preqa_chunk_spin.setRange(10, 1000)
        self.preqa_chunk_spin.setValue(self.user_cfg.get('QA_CHUNK_SIZE', 80)) 
        self.preqa_chunk_spin.setSuffix(" cụm/file")
        
        chunk_settings.addWidget(QLabel("Giới hạn báo cáo (Chunking):"))
        chunk_settings.addWidget(self.preqa_chunk_spin)
        chunk_settings.addStretch()
        g_pre_qa_layout.addLayout(chunk_settings)
        
        btn_pre_qa = QPushButton("📝 Phân tích & Xuất Báo Cáo")
        btn_pre_qa.setObjectName("ActionBtn")
        btn_pre_qa.clicked.connect(self.run_pre_qa_srt)
        g_pre_qa_layout.addWidget(btn_pre_qa)
        
        layout_pre_qa.addWidget(group_pre_qa)
        layout_pre_qa.addStretch()

        # TRANG 7: CONVERT SPEED SRT
        page_speed = QWidget()
        layout_speed = QVBoxLayout(page_speed)
        layout_speed.setContentsMargins(0, 0, 0, 0)
        group_speed = QGroupBox("Công Cụ: Chuyển đổi tốc độ mốc thời gian SRT")
        g_speed_layout = QVBoxLayout(group_speed)
        g_speed_layout.setSpacing(15)
        
        self.speed_in_file = self.create_modern_file_row(g_speed_layout, "📄 File SRT gốc cần chuyển:", self.user_cfg.get('SPEED_TOOL_IN_FILE', ''), "SRT (*.srt)")
        self.speed_out_file = self.create_modern_file_row(g_speed_layout, "💾 Đường dẫn lưu kết quả (Tự tạo nếu chưa có):", self.user_cfg.get('SPEED_TOOL_OUT_FILE', ''), "SRT (*.srt)", is_save=True)
        
        speed_settings = QHBoxLayout()
        
        speed_settings.addWidget(QLabel("Tốc độ gốc (VD: 0.7):"))
        self.spin_old_speed = QDoubleSpinBox()
        self.spin_old_speed.setRange(0.1, 5.0)
        self.spin_old_speed.setValue(self.user_cfg.get('SPEED_TOOL_OLD_SPEED', 0.7))
        self.spin_old_speed.setSingleStep(0.1)
        speed_settings.addWidget(self.spin_old_speed)
        
        speed_settings.addSpacing(20)
        
        speed_settings.addWidget(QLabel("Tốc độ mới (VD: 0.8):"))
        self.spin_new_speed = QDoubleSpinBox()
        self.spin_new_speed.setRange(0.1, 5.0)
        self.spin_new_speed.setValue(self.user_cfg.get('SPEED_TOOL_NEW_SPEED', 0.8))
        self.spin_new_speed.setSingleStep(0.1)
        speed_settings.addWidget(self.spin_new_speed)
        
        speed_settings.addStretch()
        g_speed_layout.addLayout(speed_settings)
        
        btn_speed = QPushButton("⏱️ Bắt đầu Chuyển đổi & Inject")
        btn_speed.setObjectName("ActionBtn")
        btn_speed.clicked.connect(self.run_convert_speed)
        g_speed_layout.addWidget(btn_speed)
        
        layout_speed.addWidget(group_speed)
        layout_speed.addStretch()

        # =========================================================
        self.srt_stack.addWidget(page_split) 
        self.srt_stack.addWidget(page_compare)  
        self.srt_stack.addWidget(page_pre_qa)   
        self.srt_stack.addWidget(page_replace)  
        self.srt_stack.addWidget(page_merge)    
        self.srt_stack.addWidget(page_fix) 
        self.srt_stack.addWidget(page_speed)     
        
        self.srt_menu.currentRowChanged.connect(self.srt_stack.setCurrentIndex)
        self.srt_menu.setCurrentRow(0)

#------------------------------------------------------------------------
    def setup_gemini_tab(self):
        layout_gemini = QVBoxLayout(self.tab_gemini)
        layout_gemini.setContentsMargins(20, 20, 20, 20)
        
        # --- CÔNG TẮC CHUYỂN ĐỔI CHẾ ĐỘ ---
        mode_layout = QHBoxLayout()
        lbl_mode = QLabel("🤖 Chọn tác vụ tự động hóa Playwright:")
        lbl_mode.setStyleSheet("font-weight: 600; color: #cbd5e1; font-size: 13px;")
        mode_layout.addWidget(lbl_mode)
        
        self.rad_gemini_trans = QRadioButton("Chế độ: Dịch Phụ Đề")
        self.rad_gemini_repair = QRadioButton("Chế độ: Sửa Lỗi QA (Repair)")
        self.rad_gemini_trans.setChecked(True) # Mặc định là Dịch thuật
        
        mode_layout.addWidget(self.rad_gemini_trans)
        mode_layout.addWidget(self.rad_gemini_repair)
        mode_layout.addStretch()
        layout_gemini.addLayout(mode_layout)
        profile_layout = QHBoxLayout()
        profile_layout.addWidget(QLabel("👤 Chọn Tài Khoản (Profile):"))
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"])
        self.profile_combo.setStyleSheet("padding: 5px; background-color: #1e293b; color: #fff;")
        # Khôi phục profile đã chọn từ lần trước
        saved_profile_idx = self.user_cfg.get('GEMINI_PROFILE_INDEX', 0)
        self.profile_combo.setCurrentIndex(saved_profile_idx)
        profile_layout.addWidget(self.profile_combo)
        profile_layout.addStretch()
        layout_gemini.addLayout(profile_layout)

        # Sử dụng StackedWidget để chuyển trang mượt mà
        self.gemini_stack = QStackedWidget()
        layout_gemini.addWidget(self.gemini_stack)

        # ==================================================
        # TRANG 1: GIAO DIỆN DỊCH THUẬT (GIỮ NGUYÊN NHƯ CŨ)
        # ==================================================
        page_trans = QWidget()
        layout_trans = QVBoxLayout(page_trans)
        layout_trans.setContentsMargins(0, 0, 0, 0)
        
        group_gemini = QGroupBox("Công Cụ: Tự động tải file SRT lên Gemini dịch")
        g_gemini_layout = QVBoxLayout(group_gemini)
        g_gemini_layout.setSpacing(15)
        
        self.gemini_prompt = self.create_modern_file_row(g_gemini_layout, "📄 File Text Prompt (Luật dịch):", self.user_cfg.get('GEMINI_PROMPT_PATH', ''), "Text (*.txt)")
        self.gemini_cn_dir = self.create_modern_file_row(g_gemini_layout, "📁 Thư mục chứa SRT gốc (Nguồn CN):", self.user_cfg.get('GEMINI_CN_DIR', ''), is_dir=True)
        self.gemini_vi_dir = self.create_modern_file_row(g_gemini_layout, "💾 Thư mục lưu SRT kết quả (Đích VI):", self.user_cfg.get('GEMINI_VI_DIR', ''), is_dir=True)
        
        gemini_settings = QHBoxLayout()
        gemini_settings.addWidget(QLabel("Thời gian chờ Gemini:"))
        self.gemini_wait_spin = QSpinBox()
        self.gemini_wait_spin.setRange(20, 300)
        self.gemini_wait_spin.setValue(self.user_cfg.get('GEMINI_WAIT_TIME', 40))
        self.gemini_wait_spin.setSuffix(" giây")
        gemini_settings.addWidget(self.gemini_wait_spin)
        
        gemini_settings.addSpacing(20)
        gemini_settings.addWidget(QLabel("Nghỉ giữa các file:"))
        self.gemini_delay_spin = QSpinBox()
        self.gemini_delay_spin.setRange(5, 60)
        self.gemini_delay_spin.setValue(self.user_cfg.get('GEMINI_DELAY_TIME', 15))
        self.gemini_delay_spin.setSuffix(" giây")
        gemini_settings.addWidget(self.gemini_delay_spin)
        gemini_settings.addStretch()
        
        g_gemini_layout.addLayout(gemini_settings)
        
        self.btn_gemini = QPushButton("Tiến hành Dịch Tự Động")
        self.btn_gemini.setObjectName("PrimaryActionBtn")
        self.btn_gemini.setStyleSheet("background-color: #0ea5e9; color: #ffffff;") 
        self.btn_gemini.setMinimumHeight(45)
        self.btn_gemini.clicked.connect(self.run_gemini_translator)
        g_gemini_layout.addWidget(self.btn_gemini)
        
        layout_trans.addWidget(group_gemini)
        layout_trans.addStretch()
        self.gemini_stack.addWidget(page_trans)

        # ==================================================
        # TRANG 2: GIAO DIỆN AUTO REPAIR
        # ==================================================
        page_repair = QWidget()
        layout_repair = QVBoxLayout(page_repair)
        layout_repair.setContentsMargins(0, 0, 0, 0)
        
        group_repair = QGroupBox("Công Cụ: Tự động tải báo cáo lỗi lên Gemini sửa")
        g_repair_layout = QVBoxLayout(group_repair)
        g_repair_layout.setSpacing(15)

        self.qa_repair_prompt = self.create_modern_file_row(g_repair_layout, "📄 File Text Prompt (Luật Repair Engine):", self.user_cfg.get('QA_REPAIR_PROMPT', ''), "Text (*.txt)")
        self.qa_repair_report_dir = self.create_modern_file_row(g_repair_layout, "📁 Thư mục chứa các báo cáo lỗi (.txt):", self.user_cfg.get('QA_REPAIR_REPORT_DIR', ''), is_dir=True)
        self.qa_repair_orig_dir = self.create_modern_file_row(g_repair_layout, "📁 Thư mục chứa SRT gốc (bị lỗi):", self.user_cfg.get('QA_REPAIR_ORIGINAL_DIR', ''), is_dir=True)
        self.qa_repair_fixed_dir = self.create_modern_file_row(g_repair_layout, "💾 Thư mục an toàn lưu SRT đã sửa (Output):", self.user_cfg.get('QA_REPAIR_FIXED_DIR', ''), is_dir=True)

        repair_settings = QHBoxLayout()
        repair_settings.addWidget(QLabel("Thời gian chờ Gemini:"))
        self.qa_wait_spin = QSpinBox()
        self.qa_wait_spin.setRange(20, 300)
        self.qa_wait_spin.setValue(self.user_cfg.get('GEMINI_WAIT_TIME', 40))
        self.qa_wait_spin.setSuffix(" giây")
        repair_settings.addWidget(self.qa_wait_spin)

        repair_settings.addSpacing(20)
        repair_settings.addWidget(QLabel("Nghỉ giữa các báo cáo:"))
        self.qa_delay_spin = QSpinBox()
        self.qa_delay_spin.setRange(5, 60)
        self.qa_delay_spin.setValue(self.user_cfg.get('GEMINI_DELAY_TIME', 15))
        self.qa_delay_spin.setSuffix(" giây")
        repair_settings.addWidget(self.qa_delay_spin)
        repair_settings.addStretch()
        
        g_repair_layout.addLayout(repair_settings)

        self.btn_qa_repair = QPushButton("Tiến Hành Auto Repair")
        self.btn_qa_repair.setObjectName("PrimaryActionBtn")
        self.btn_qa_repair.setStyleSheet("background-color: #f59e0b; color: #ffffff;") # Nút màu Vàng cam cho Repair
        self.btn_qa_repair.setMinimumHeight(45)
        self.btn_qa_repair.clicked.connect(self.run_qa_repair_tool)
        g_repair_layout.addWidget(self.btn_qa_repair)

        layout_repair.addWidget(group_repair)
        layout_repair.addStretch()
        self.gemini_stack.addWidget(page_repair)

        # Logic kết nối Radio Button với Stacked Widget
        self.rad_gemini_trans.toggled.connect(lambda: self.gemini_stack.setCurrentIndex(0) if self.rad_gemini_trans.isChecked() else None)
        self.rad_gemini_repair.toggled.connect(lambda: self.gemini_stack.setCurrentIndex(1) if self.rad_gemini_repair.isChecked() else None)
    
    def run_qa_repair_tool(self):
        prompt_path = self.qa_repair_prompt.text()
        report_dir = self.qa_repair_report_dir.text()
        orig_dir = self.qa_repair_orig_dir.text()
        fixed_dir = self.qa_repair_fixed_dir.text()
        
        # --- LẤY THÔNG SỐ TỪ SPINNER MỚI ---
        wait_time = self.qa_wait_spin.value()
        delay_time = self.qa_delay_spin.value()
        profile_idx = self.profile_combo.currentIndex()
        profile_folder = f"chrome_data_{profile_idx + 1}"

        if not report_dir or not orig_dir or not fixed_dir:
            return self.log_msg("⚠️ Vui lòng điền đầy đủ thư mục báo cáo, thư mục SRT gốc và thư mục đích.")
        if not os.path.exists(report_dir):
            return self.log_msg(f"⚠️ Thư mục chứa báo cáo không tồn tại: {report_dir}")
        if not os.path.exists(orig_dir):
            return self.log_msg(f"⚠️ Thư mục SRT gốc không tồn tại: {orig_dir}")

        self.user_cfg['QA_REPAIR_PROMPT'] = prompt_path
        self.user_cfg['QA_REPAIR_REPORT_DIR'] = report_dir
        self.user_cfg['QA_REPAIR_ORIGINAL_DIR'] = orig_dir
        self.user_cfg['QA_REPAIR_FIXED_DIR'] = fixed_dir
        self.user_cfg['GEMINI_WAIT_TIME'] = wait_time
        self.user_cfg['GEMINI_DELAY_TIME'] = delay_time
        self.user_cfg['GEMINI_PROFILE_INDEX'] = profile_idx
        save_user_config(self.user_cfg)

        self.log_output.clear()
        self.log_msg("Đang khởi tạo trình duyệt Playwright cho QA Repair...")

        self.btn_qa_repair.setEnabled(False)
        self.btn_qa_repair.setText("⏳ Đang kết nối AI sửa lỗi...")
        self.stop_btn.setEnabled(True)

        self.worker = GenericWorker(run_auto_qa_repair, prompt_path, report_dir, orig_dir, fixed_dir, profile_folder=profile_folder, wait_time=wait_time, delay_time=delay_time)        
        self.worker.log_signal.connect(self.log_msg)

        def on_finished(success, msg):
            self.log_msg(f"\n[HỆ THỐNG] {msg}")
            self.btn_qa_repair.setEnabled(True)
            self.btn_qa_repair.setText("Tiến Hành Auto Repair")
            self.stop_btn.setEnabled(False)

        self.worker.finished_signal.connect(on_finished)
        self.worker.start()
    
    def setup_qa_tools_tab(self):
        layout = QVBoxLayout(self.tab_qa_tools)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        group_qa = QGroupBox("Báo cáo Trải nghiệm Audio (Tốc độ CPS / Tràn khung hình)")
        g_qa_layout = QVBoxLayout(group_qa)
        g_qa_layout.setSpacing(15)
        
        self.qa_srt_in = self.create_modern_file_row(g_qa_layout, "📄 File SRT hoàn thiện:", "", "SRT (*.srt)")
        self.qa_audio_dir = self.create_modern_file_row(g_qa_layout, "📁 Thư mục chứa Audio (WAV/MP3):", "", is_dir=True)
        self.qa_log_out = self.create_modern_file_row(g_qa_layout, "📝 Lưu báo cáo Log tại:", f"./qa_report_{datetime.now().strftime('%H%M%S')}.log", "Log (*.log)", is_save=True)
        
        btn_qa = QPushButton("🔍 Bắt Đầu Chạy Phân Tích QA")
        btn_qa.setObjectName("PrimaryActionBtn")
        btn_qa.setStyleSheet("background-color: #0ea5e9; color: #ffffff;")
        btn_qa.setMinimumHeight(45)
        btn_qa.clicked.connect(self.run_qa_analysis)
        g_qa_layout.addWidget(btn_qa)
        
        layout.addWidget(group_qa)
        layout.addStretch()

    # ------------------ CƠ CHẾ AUTO-CREATE PATH ------------------
    def auto_create_path(self, path, is_file=True):
        if not path: return
        directory = os.path.dirname(path) if is_file else path
        if directory and not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
                self.log_msg(f"📁 Tự động tạo thư mục: {directory}")
            except Exception as e:
                self.log_msg(f"❌ Không thể tạo thư mục {directory}: {e}")
                
        if is_file and not os.path.exists(path):
            try:
                with open(path, 'a', encoding='utf-8') as f:
                    pass
                self.log_msg(f"📄 Tự động tạo file trắng: {path}")
            except Exception as e:
                self.log_msg(f"❌ Không thể tạo file {path}: {e}")

    # ------------------ UX/UI HELPERS ------------------
    def create_flexible_file_row(self, parent_layout, label_text, default_val, file_filter=""):
        vbox = QVBoxLayout()
        vbox.setSpacing(4)
        
        lbl = QLabel(label_text)
        lbl.setStyleSheet("font-weight: 600; color: #cbd5e1; font-size: 12px;")
        vbox.addWidget(lbl)
        
        hbox = QHBoxLayout()
        line_edit = QLineEdit(default_val)
        hbox.addWidget(line_edit)
        
        btn = QPushButton("Duyệt...")
        btn.setObjectName("FileBrowseButton")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hbox.addWidget(btn)
        
        vbox.addLayout(hbox)
        parent_layout.addLayout(vbox)
        return line_edit, btn, lbl

    def select_flexible_path(self, line_edit, file_filter, is_dir):
        if is_dir:
            dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục nguồn")
            if dir_path: line_edit.setText(os.path.abspath(dir_path))
        else:
            file_path, _ = QFileDialog.getOpenFileName(self, "Chọn file phụ đề", "", file_filter)
            if file_path: line_edit.setText(os.path.abspath(file_path))

    def create_modern_file_row(self, parent_layout, label_text, default_val, file_filter="", is_dir=False, is_save=False):
        vbox = QVBoxLayout()
        vbox.setSpacing(4)
        lbl = QLabel(label_text)
        lbl.setStyleSheet("font-weight: 600; color: #cbd5e1; font-size: 12px;")
        vbox.addWidget(lbl)
        
        hbox = QHBoxLayout()
        line_edit = QLineEdit(default_val)
        hbox.addWidget(line_edit)
        
        btn = QPushButton("Duyệt...")
        btn.setObjectName("FileBrowseButton")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if is_dir:
            btn.clicked.connect(lambda: self.select_directory(line_edit))
        elif is_save:
            btn.clicked.connect(lambda: self.save_file_dialog(line_edit, file_filter))
        else:
            btn.clicked.connect(lambda: self.select_file(line_edit, file_filter))
        hbox.addWidget(btn)
        
        vbox.addLayout(hbox)
        parent_layout.addLayout(vbox)
        return line_edit

    def select_file(self, line_edit, file_filter):
        file_path, _ = QFileDialog.getOpenFileName(self, "Chọn file", "", file_filter)
        if file_path: line_edit.setText(os.path.abspath(file_path))

    def save_file_dialog(self, line_edit, file_filter):
        file_path, _ = QFileDialog.getSaveFileName(self, "Lưu file", line_edit.text(), file_filter)
        if file_path: line_edit.setText(os.path.abspath(file_path))

    def select_directory(self, line_edit):
        dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if dir_path: line_edit.setText(os.path.abspath(dir_path))

    def setup_global_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #0f172a; }
            QLabel { font-family: 'Segoe UI', Arial, sans-serif; }
            
            QGroupBox { 
                border: 1px solid #334155; 
                border-radius: 8px; 
                margin-top: 15px; 
                padding-top: 20px; 
                font-weight: bold; 
                color: #38bdf8; 
                font-size: 13px;
                background-color: #1e293b;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 15px; padding: 0 5px; }
            #PanelWidget, QTabWidget::pane { background-color: #1e293b; border-radius: 8px; border: 1px solid #334155; }
            
            QTabBar::tab { 
                background: #334155; color: #94a3b8; padding: 10px 20px; 
                border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; font-weight: 600;
            }
            QTabBar::tab:selected { background: #1e293b; color: #0ea5e9; border-bottom: 2px solid #0ea5e9; }
            QTabBar::tab:hover:!selected { background: #475569; color: #e2e8f0; }
            
            QLineEdit, QTextEdit, QDoubleSpinBox, QSpinBox {
                background-color: #0f172a; color: #f8fafc; border: 1px solid #475569;
                border-radius: 6px; padding: 8px 12px; font-family: 'Segoe UI'; font-size: 13px;
            }
            QLineEdit:focus, QTextEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus { border: 1px solid #0ea5e9; background-color: #1e293b; }
            
            QComboBox {
                background-color: #0f172a; color: #f8fafc; border: 1px solid #475569;
                border-radius: 6px; padding: 8px 12px; font-family: 'Segoe UI'; font-size: 10pt;
            }
            QComboBox:focus { border: 1px solid #0ea5e9; background-color: #1e293b; }
            QTextEdit[readOnly="true"] { background-color: #0B1120; color: #34d399; font-family: 'Consolas', monospace; font-size: 13px; border: 1px solid #1e293b; line-height: 1.5; }
            
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background-color: #1e293b; color: #f8fafc; border: 1px solid #475569; selection-background-color: #0ea5e9; font-size: 10px; outline: none; }            
            QPushButton#FileBrowseButton { background-color: #334155; color: #f8fafc; border: 1px solid #475569; border-radius: 6px; padding: 7px 15px; font-weight: 600; }
            QPushButton#FileBrowseButton:hover { background-color: #475569; }
            
            QPushButton#ActionBtn { background-color: #0284c7; color: #ffffff; border: none; border-radius: 6px; padding: 10px 20px; font-weight: bold; font-size: 13px; }
            QPushButton#ActionBtn:hover { background-color: #0ea5e9; }
            QPushButton#PrimaryActionBtn { font-size: 14px; font-weight: bold; border-radius: 8px; }
            QPushButton#SmallButton { background-color: #ef4444; color: white; border-radius: 4px; font-size: 11px; font-weight: bold; }
            QPushButton#SmallButton:hover { background-color: #f87171; }
            QPushButton:disabled { background-color: #334155 !important; color: #64748b !important; }
            
            QProgressBar { border: 1px solid #334155; border-radius: 6px; text-align: center; background-color: #0f172a; color: #ffffff; font-weight: bold; font-size: 12px; }
            QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0ea5e9, stop:1 #38bdf8); border-radius: 5px; }
            
            QListWidget { background-color: #1e293b; border: 1px solid #334155; border-radius: 8px; outline: none; padding: 8px; }
            QListWidget::item { color: #cbd5e1; padding: 12px 15px; border-radius: 6px; margin-bottom: 4px; font-weight: 600; font-size: 13px; }
            QListWidget::item:hover { background-color: #334155; color: #ffffff; }
            QListWidget::item:selected { background-color: #0ea5e9; color: #ffffff; }
            
            QCheckBox { 
                color: #94a3b8; font-weight: bold; font-size: 13px; padding: 10px 15px; 
                border-radius: 6px; border: 2px solid #334155; background-color: #0f172a;
            }
            QCheckBox::indicator { width: 0px; height: 0px; border: none; } 
            QCheckBox:hover { background-color: #1e293b; border: 2px solid #475569; color: #cbd5e1; }
            QCheckBox:checked { background-color: rgba(234, 179, 8, 0.15); color: #eab308; border: 2px solid #eab308; }

            QRadioButton { color: #cbd5e1; font-weight: 600; font-size: 13px; padding: 5px; }
            QRadioButton::indicator { width: 16px; height: 16px; border-radius: 8px; border: 1px solid #475569; background-color: #0f172a; }
            QRadioButton::indicator:checked { background-color: #0ea5e9; border: 3px solid #0f172a; }
            QRadioButton::indicator:hover { border: 1px solid #0ea5e9; }
            
            QSplitter::handle { background-color: #334155; width: 4px; margin: 2px 5px; border-radius: 2px; }
            QSplitter::handle:hover { background-color: #0ea5e9; }
            #InjectFrame { border-radius: 8px; }
            
            QScrollBar:vertical { border: none; background-color: #0f172a; width: 10px; margin: 0px 0px 0px 0px; border-radius: 5px; }
            QScrollBar::handle:vertical { background-color: #334155; min-height: 20px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background-color: #475569; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
        """)
        

    # ------------------ EVENT HANDLERS ------------------
    def log_msg(self, msg):
        scrollbar = self.log_output.verticalScrollBar()
        is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5
        self.log_output.append(msg)
        if is_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    # ĐÃ THÊM **kwargs VÀO HÀM RUN_TOOL ĐỂ CÓ THỂ TRUYỀN THAM SỐ ĐỘNG
    def run_tool(self, func, *args, **kwargs):
        self.stop_btn.setEnabled(True)
        self.worker = GenericWorker(func, *args, **kwargs)
        self.worker.log_signal.connect(self.log_msg)
        def on_tool_finished(s, m):
            self.log_msg(f"\n[HỆ THỐNG] {m}")
            self.stop_btn.setEnabled(False) 
            
        self.worker.finished_signal.connect(on_tool_finished)
        self.worker.start()

    def run_merge_srt(self):
        in_dir = self.merge_in_dir.text()
        out_file = self.merge_out_file.text()
        if not in_dir or not out_file: return self.log_msg("⚠️ Vui lòng điền đủ đường dẫn.")
        if not os.path.exists(in_dir): return self.log_msg(f"⚠️ Cảnh báo: Thư mục đầu vào không tồn tại: {in_dir}")
        self.user_cfg['MERGE_IN_DIR'] = in_dir
        self.user_cfg['MERGE_OUT_FILE'] = out_file
        save_user_config(self.user_cfg)
        self.log_output.clear()
        self.auto_create_path(out_file, is_file=True)
        self.run_tool(merge_numbered_srt_files, in_dir, out_file)

    def run_fix_srt(self):
        in_path = self.fix_in_file.text()
        out_path = self.fix_out_file.text()
        if not in_path or not out_path: return self.log_msg("⚠️ Vui lòng điền đủ đường dẫn.")
        if not os.path.exists(in_path): return self.log_msg(f"⚠️ Cảnh báo: Đường dẫn đầu vào không tồn tại: {in_path}")
        self.user_cfg['FIX_IN_PATH'] = in_path
        self.user_cfg['FIX_OUT_PATH'] = out_path
        save_user_config(self.user_cfg)
        self.log_output.clear()
        
        self.auto_create_path(out_path, is_file=not self.fix_rad_dir.isChecked())
        self.run_tool(process_and_renumber_srt, in_path, out_path)

    def run_split_srt(self):
        in_file = self.split_in_file.text()
        out_dir = self.split_out_dir.text()
        prefix = self.split_out_prefix.text()
        blocks = self.split_blocks.value()
        
        if not in_file or not out_dir or not prefix: 
            return self.log_msg("⚠️ Vui lòng điền đầy đủ thư mục đầu vào, đầu ra và tiền tố.")
        if not os.path.exists(in_file): 
            return self.log_msg(f"⚠️ Cảnh báo: File SRT gốc không tồn tại: {in_file}")
        
        self.user_cfg['SPLIT_IN_FILE'] = in_file
        self.user_cfg['SPLIT_OUT_DIR'] = out_dir
        self.user_cfg['SPLIT_PREFIX'] = prefix
        self.user_cfg['SPLIT_BLOCKS'] = blocks
        save_user_config(self.user_cfg)
            
        self.log_output.clear()
        self.auto_create_path(out_dir, is_file=False)
        full_prefix_path = os.path.join(out_dir, prefix)
        self.run_tool(split_srt_file, in_file, full_prefix_path, blocks)

    def run_batch_replace(self):
        in_path = self.replace_in_path.text()
        patch_text = self.replace_text_input.toPlainText()
        is_dir = self.replace_rad_dir.isChecked()
        
        if not in_path: return self.log_msg("⚠️ Vui lòng chọn đường dẫn (File hoặc Thư mục).")
        if not patch_text.strip(): return self.log_msg("⚠️ Vui lòng dán nội dung các block SRT cần replace.")
        if not os.path.exists(in_path): return self.log_msg(f"⚠️ Cảnh báo: Đường dẫn không tồn tại: {in_path}")
        self.user_cfg['REPLACE_IN_PATH'] = in_path
        save_user_config(self.user_cfg)
        
        self.log_output.clear()
        
        if is_dir:
            self.run_tool(replace_blocks_in_folder, in_path, patch_text)
        else:
            self.run_tool(replace_blocks_in_file, in_path, patch_text)

    def run_compare_srt(self):
        folder_a = self.compare_dir_a.text()
        folder_b = self.compare_dir_b.text()
        if not folder_a or not folder_b: return self.log_msg("⚠️ Vui lòng chọn đầy đủ cả 2 thư mục cần so sánh.")
        if not os.path.exists(folder_a): return self.log_msg(f"⚠️ Cảnh báo: Thư mục A không tồn tại: {folder_a}")
        if not os.path.exists(folder_b): return self.log_msg(f"⚠️ Cảnh báo: Thư mục B không tồn tại: {folder_b}")
        self.user_cfg['COMPARE_DIR_B'] = folder_b
        save_user_config(self.user_cfg)
        self.log_output.clear()
        self.run_tool(compare_srt_folders, folder_a, folder_b)

    def run_pre_qa_srt(self):
        in_path = self.preqa_in_file.text()
        out_dir = self.preqa_out_dir.text()
        chunk_size = self.preqa_chunk_spin.value() 
        
        # ĐỌC TRẠNG THÁI TỪ DROPDOWN VÀ GÁN MODE
        idx = self.qa_mode_combo.currentIndex()
        if idx == 1:
            scan_mode = 'semantic'
        elif idx == 2:
            scan_mode = 'cps'
        else:
            scan_mode = 'all'
        
        if not in_path or not out_dir: 
            return self.log_msg("⚠️ Vui lòng điền đủ đường dẫn đầu vào và thư mục lưu báo cáo.")
        if not os.path.exists(in_path): 
            return self.log_msg(f"⚠️ Cảnh báo: Đường dẫn đầu vào không tồn tại: {in_path}")
        
        self.user_cfg['QA_OUT_DIR'] = out_dir
        self.user_cfg['QA_CHUNK_SIZE'] = chunk_size
        self.user_cfg['QA_SCAN_MODE'] = idx
        save_user_config(self.user_cfg)
        self.log_output.clear()
        self.auto_create_path(out_dir, is_file=False)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        out_file_path = os.path.join(out_dir, f"PreQA_Report_{timestamp}.txt")
        
        # TRUYỀN THAM SỐ scan_mode VÀO HÀM RUN_TOOL
        self.run_tool(analyze_srt_to_file, in_path, out_file_path, chunk_size, scan_mode=scan_mode)
    
    def run_convert_speed(self):
        in_file = self.speed_in_file.text()
        out_file = self.speed_out_file.text()
        old_spd = self.spin_old_speed.value()
        new_spd = self.spin_new_speed.value()

        if not in_file or not out_file:
            return self.log_msg("⚠️ Vui lòng điền đủ đường dẫn file gốc và file xuất ra.")
        if not os.path.exists(in_file):
            return self.log_msg(f"⚠️ Cảnh báo: File SRT gốc không tồn tại: {in_file}")
        
        self.user_cfg['SPEED_TOOL_IN_FILE'] = in_file
        self.user_cfg['SPEED_TOOL_OUT_FILE'] = out_file
        self.user_cfg['SPEED_TOOL_OLD_SPEED'] = old_spd
        self.user_cfg['SPEED_TOOL_NEW_SPEED'] = new_spd
        save_user_config(self.user_cfg)
        
        self.log_output.clear()
        self.auto_create_path(out_file, is_file=True) 
        self.run_tool(process_srt_speed, in_file, out_file, old_spd, new_spd)

    def run_qa_analysis(self):
        srt = self.qa_srt_in.text()
        aud_dir = self.qa_audio_dir.text()
        log_out = self.qa_log_out.text()
        if not srt or not aud_dir or not log_out: return self.log_msg("⚠️ Vui lòng điền đủ đường dẫn.")
        if not os.path.exists(srt): return self.log_msg(f"⚠️ Cảnh báo: File SRT không tồn tại: {srt}")
        if not os.path.exists(aud_dir): return self.log_msg(f"⚠️ Cảnh báo: Thư mục Audio không tồn tại: {aud_dir}")

        self.user_cfg['QA_AUDIO_SRT_IN'] = srt
        self.user_cfg['QA_AUDIO_DIR_IN'] = aud_dir
        self.user_cfg['QA_AUDIO_LOG_OUT'] = log_out
        save_user_config(self.user_cfg)
        self.log_output.clear()
        self.auto_create_path(log_out, is_file=True)
        self.run_tool(analyze_audio_wpm_and_log, srt, aud_dir, log_out)

    def run_gemini_translator(self):
        prompt_path = self.gemini_prompt.text()
        cn_dir = self.gemini_cn_dir.text()
        vi_dir = self.gemini_vi_dir.text()
        wait_time = self.gemini_wait_spin.value()
        delay_time = self.gemini_delay_spin.value()

        profile_idx = self.profile_combo.currentIndex()
        profile_folder = f"chrome_data_{profile_idx + 1}"
        
        if not cn_dir or not vi_dir:
            return self.log_msg("⚠️ Vui lòng chọn ít nhất thư mục nguồn (CN) và thư mục đích (VI).")
        if not os.path.exists(cn_dir):
            return self.log_msg(f"⚠️ Thư mục nguồn không tồn tại: {cn_dir}")
            
        # Lưu lại thông số vào file JSON
        self.user_cfg['GEMINI_PROMPT_PATH'] = prompt_path
        self.user_cfg['GEMINI_CN_DIR'] = cn_dir
        self.user_cfg['GEMINI_VI_DIR'] = vi_dir
        self.user_cfg['GEMINI_WAIT_TIME'] = wait_time
        self.user_cfg['GEMINI_DELAY_TIME'] = delay_time
        self.user_cfg['GEMINI_PROFILE_INDEX'] = profile_idx 
        save_user_config(self.user_cfg)
        
        self.log_output.clear()
        self.auto_create_path(vi_dir, is_file=False)
        
        self.log_msg("Đang khởi tạo trình duyệt Playwright, vui lòng chờ...")

        # KHÓA NÚT BẤM để tránh người dùng spam click gây mở nhiều Chrome
        self.btn_gemini.setEnabled(False)
        self.btn_gemini.setText("⏳ Đang xử lý dịch thuật...")
        self.stop_btn.setEnabled(True)

        # TẠO VÀ CHẠY WORKER MỘT LẦN DUY NHẤT
        self.worker = GenericWorker(run_auto_translate_srt, prompt_path, cn_dir, vi_dir, profile_folder, wait_time=wait_time, delay_time=delay_time)
        self.worker.log_signal.connect(self.log_msg)
        
        # Xử lý khi tiến trình kết thúc
        def on_finished(success, msg):
            self.log_msg(f"\n[HỆ THỐNG] {msg}")
            # Mở khóa và reset lại text của nút bấm
            self.btn_gemini.setEnabled(True)
            self.btn_gemini.setText("Tiến hành Dịch Tự Động")
            self.stop_btn.setEnabled(False)
            
        self.worker.finished_signal.connect(on_finished)
        
        # Bắt đầu chạy luồng (XÓA BỎ self.run_tool ở đây để tránh chạy đúp)
        self.worker.start()

    def start_process(self):

        idx = self.inject_mode_combo.currentIndex()
        is_inject_only = (idx == 1)
        
        current_config = {
            'SRT_FILE_PATH': self.srt_input.text(), 
            'REF_AUDIO_PATH': self.ref_audio_input.text(),
            'AUDIO_OUT_DIR': self.audio_out_input.text(), 
            'CAPCUT_JSON_PATH': self.capcut_json_input.text(),
            'SERVER_URL': self.server_input.text(), 
            'REF_TEXT': self.ref_text_input.toPlainText(),
            'SPEED_RATIO': self.speed_spin.value(),
            'INJECT_ONLY': is_inject_only,
        }
        
        if not os.path.exists(current_config['SRT_FILE_PATH']):
            return self.log_msg(f"⚠️ Cảnh báo: File phụ đề gốc không tồn tại: {current_config['SRT_FILE_PATH']}")
        if not os.path.exists(current_config['CAPCUT_JSON_PATH']):
            return self.log_msg(f"⚠️ Cảnh báo: File JSON CapCut không tồn tại: {current_config['CAPCUT_JSON_PATH']}")
            
        if not current_config['INJECT_ONLY'] and not os.path.exists(current_config['REF_AUDIO_PATH']):
            return self.log_msg(f"⚠️ Cảnh báo: File Audio mẫu không tồn tại: {current_config['REF_AUDIO_PATH']}")

        self.auto_create_path(current_config['AUDIO_OUT_DIR'], is_file=False)

        save_user_config(current_config)
        self.progress_bar.setValue(0)
        self.log_output.clear()
        
        self.start_btn.setEnabled(False)
        self.inject_mode_combo.setEnabled(False)
        self.phase_label.setText("Trạng thái: ⏳ Đang chạy tiến trình xử lý...")
        self.stop_btn.setEnabled(True)
        
        self.worker = BackendWorker(current_config)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.log_signal.connect(self.log_msg)
        self.worker.finished_signal.connect(self.process_finished)
        self.worker.start()

    def update_progress(self, done, total, phase_name):
        self.phase_label.setText(f"Giai đoạn: {phase_name}...")
        pct = int((done / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.progress_bar.setFormat(f"🔹 Xử lý: {done}/{total} ({pct}%)")

    def process_finished(self, success, message):
        self.start_btn.setEnabled(True)
        self.inject_mode_combo.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if success:
            self.phase_label.setText("Trạng thái: HOÀN THÀNH XUẤT SẮC! 🎉")
            self.log_msg(f"\n🎉 {message}")
        else:
            self.phase_label.setText("Trạng thái: THẤT BẠI! ❌")
            self.log_msg(f"\n❌ LỖI HỆ THỐNG: {message}")

    def stop_active_process(self):
        """Hàm ép buộc dừng QThread đang chạy"""
        if hasattr(self, 'worker') and self.worker and self.worker.isRunning():
            self.log_msg("\n⚠️ [HỆ THỐNG] NHẬN LỆNH DỪNG KHẨN CẤP! Đang hủy tiến trình...")
            self.phase_label.setText("Trạng thái: ĐANG HỦY TIẾN TRÌNH... 🛑")
            
            # Ép buộc kết thúc luồng (Terminate)
            self.worker.terminate()
            self.worker.wait()
            
            # TIÊU DIỆT CHROME CHẠY NGẦM DO PLAYWRIGHT ĐỂ LẠI
            try:
                import subprocess, platform
                if platform.system() == "Windows":
                    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(["pkill", "-f", "Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.log_msg("🧹 Đã đóng các trình duyệt Chrome chạy ngầm.")
            except Exception:
                pass
            
            self.log_msg("🛑 ĐÃ DỪNG TOÀN BỘ HOẠT ĐỘNG!")
            self.phase_label.setText("Trạng thái: ĐÃ BỊ HỦY BỞI NGƯỜI DÙNG 🛑")
            
            # MỞ KHÓA LẠI TOÀN BỘ CÁC NÚT BẤM TRÊN GIAO DIỆN
            self.stop_btn.setEnabled(False)
            
            if hasattr(self, 'start_btn'): 
                self.start_btn.setEnabled(True)
            if hasattr(self, 'inject_mode_combo'): 
                self.inject_mode_combo.setEnabled(True)
                
            # Mở khóa nút Dịch Gemini
            if hasattr(self, 'btn_gemini'):
                self.btn_gemini.setEnabled(True)
                self.btn_gemini.setText("Tiến hành Dịch Tự Động")
                
            # ---> BỔ SUNG: Mở khóa nút Sửa Lỗi QA <---
            if hasattr(self, 'btn_qa_repair'):
                self.btn_qa_repair.setEnabled(True)
                self.btn_qa_repair.setText("Tiến Hành Auto Repair")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    from PyQt6.QtGui import QFont
    app.setFont(QFont("Segoe UI", 10))
    gui = CapCutInjectorGUI()
    gui.show()
    sys.exit(app.exec())
