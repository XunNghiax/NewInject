import sys
import os
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QTextEdit,
    QFrame, QGroupBox, QSplitter, QMessageBox, QToolButton,
    QComboBox, QCheckBox, QFileDialog
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMutex, QWaitCondition, QTimer
from PyQt6.QtGui import QTextCursor, QKeySequence, QShortcut

# Thêm thư mục src vào sys.path để import modul
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

try:
    from bilibili_downloader import BilibiliDownloader
except ImportError:
    BilibiliDownloader = None

try:
    from subtitle_generator import SubtitleGenerator
except ImportError:
    SubtitleGenerator = None

try:
    from gemini_translate import run_auto_translate_srt
except ImportError:
    run_auto_translate_srt = None


# ==============================================================================
# WORKER THREAD ĐỒNG BỘ: HỖ TRỢ TẢI REAL-TIME BILIBILI & GIẢ LẬP
# ==============================================================================
class ProcessWorker(QThread):
    log_signal = pyqtSignal(str, str)         # (log_msg, log_level)
    progress_signal = pyqtSignal(int, str)    # (percentage, status_text)
    finished_signal = pyqtSignal(bool, str)   # (success, final_message)

    def __init__(self, link: str, output_dir: str = "./downloads", auto_gen_srt: bool = False, auto_translate_srt: bool = False, local_media_path: str = None, srt_translate_path: str = None):
        super().__init__()
        self.link = link
        self.output_dir = output_dir
        self.auto_gen_srt = auto_gen_srt
        self.auto_translate_srt = auto_translate_srt
        self.local_media_path = local_media_path
        self.srt_translate_path = srt_translate_path
        self._is_paused = False
        self._is_stopped = False
        self.downloader = None
        self.srt_generator = None
        self.mutex = QMutex()
        self.pause_condition = QWaitCondition()

    def pause(self):
        """Tạm dừng tiến trình"""
        self.mutex.lock()
        self._is_paused = True
        self.mutex.unlock()
        self.log_signal.emit("⏸️ Đã nhận lệnh TẠM DỪNG tiến trình.", "warning")
        self.progress_signal.emit(-1, "Đã tạm dừng ⏸️")

    def resume(self):
        """Tiếp tục tiến trình"""
        self.mutex.lock()
        self._is_paused = False
        self.pause_condition.wakeAll()
        self.mutex.unlock()
        self.log_signal.emit("▶️ Đã nhận lệnh TIẾP TỤC tiến trình.", "info")

    def stop(self):
        """Dừng hoàn toàn tiến trình"""
        self.mutex.lock()
        self._is_stopped = True
        if self.downloader:
            self.downloader.cancel()
        if self.srt_generator:
            self.srt_generator.cancel()
        if self._is_paused:
            self._is_paused = False
            self.pause_condition.wakeAll()
        self.mutex.unlock()
        self.log_signal.emit("🛑 Đã nhận lệnh DỪNG TOÀN BỘ tiến trình!", "error")

    def emit_log(self, msg: str, level: str = "info"):
        self.log_signal.emit(msg, level)

    def emit_progress(self, pct: int, status: str):
        self.progress_signal.emit(pct, status)

    def run(self):
        try:
            # ── CHẾ ĐỘ 1: DỊCH FILE PHỤ ĐỀ SRT CHỌN TỪ BÊN NGOÀI ──
            if self.srt_translate_path and os.path.exists(self.srt_translate_path):
                self.log_signal.emit(f"🌐 Khởi chạy tiến trình Dịch Phụ Đề SRT sang Tiếng Việt cho: {self.srt_translate_path}", "info")
                if not run_auto_translate_srt:
                    self.finished_signal.emit(False, "Modul dịch thuật 'gemini_translate' chưa sẵn sàng!")
                    return

                # Chuẩn bị thư mục chứa file nguồn và file đích
                src_dir = os.path.dirname(os.path.abspath(self.srt_translate_path))
                file_name = os.path.basename(self.srt_translate_path)
                out_dir = self.output_dir

                prompt_file = "./prompts/promptTranslates.md"
                if not os.path.exists(prompt_file):
                    prompt_file = "./prompts/translate.txt"

                self.progress_signal.emit(10, "Đang khởi động Gemini AI Biên Dịch Phim...")
                run_auto_translate_srt(
                    prompt_file=prompt_file,
                    cn_folder=src_dir,
                    vi_folder=out_dir,
                    wait_time=300,
                    log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl)
                )
                self.progress_signal.emit(100, "Hoàn tất dịch 100%! 🎉")
                self.finished_signal.emit(True, f"🎉 Đã dịch xong phụ đề SRT sang Tiếng Việt!")
                return

            # ── CHẾ ĐỘ 2: XỬ LÝ NHẬN DIỆN GIỌNG NÓI TỪ FILE LOCAL ──
            if self.local_media_path and os.path.exists(self.local_media_path):
                self.log_signal.emit(f"🎙️ Khởi tạo tiến trình tạo phụ đề cho file: {self.local_media_path}", "info")
                if not SubtitleGenerator:
                    self.finished_signal.emit(False, "Chưa cài đặt thư viện 'faster-whisper'!")
                    return

                self.srt_generator = SubtitleGenerator(
                    output_dir=self.output_dir,
                    log_callback=self.emit_log,
                    progress_callback=self.emit_progress
                )
                res = self.srt_generator.generate_srt(self.local_media_path, model_size="base")
                if res.get("success"):
                    srt_file = res.get("srt_path")
                    # Nếu có bật tự động dịch sau khi tạo phụ đề
                    if self.auto_translate_srt and srt_file and os.path.exists(srt_file) and run_auto_translate_srt:
                        self.log_signal.emit("🌐 Khởi chạy Gemini AI dịch phụ đề vừa tạo sang Tiếng Việt...", "info")
                        prompt_file = "./prompts/promptTranslates.md"
                        if not os.path.exists(prompt_file):
                            prompt_file = "./prompts/translate.txt"
                        src_dir = os.path.dirname(os.path.abspath(srt_file))
                        run_auto_translate_srt(
                            prompt_file=prompt_file,
                            cn_folder=src_dir,
                            vi_folder=self.output_dir,
                            wait_time=300,
                            log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl)
                        )
                        self.finished_signal.emit(True, f"🎉 Đã tạo & dịch xong phụ đề SRT sang Tiếng Việt cho file local!")
                    else:
                        self.finished_signal.emit(True, f"🎉 Đã xuất file phụ đề SRT thành công: {os.path.basename(srt_file)}")
                else:
                    self.finished_signal.emit(False, res.get("error", "Lỗi tạo phụ đề"))
                return

            # ── CHẾ ĐỘ 3: TẢI VIDEO BILIBILI (+ TỰ ĐỘNG TẠO SRT + DỊCH SANG TIẾNG VIỆT NẾU BẬT) ──
            is_bilibili = BilibiliDownloader and BilibiliDownloader.is_valid_bilibili_url(self.link)

            if is_bilibili:
                self.log_signal.emit("📌 Phát hiện liên kết Bilibili! Khởi động modul BilibiliDownloader...", "info")
                self.downloader = BilibiliDownloader(
                    output_dir=self.output_dir,
                    log_callback=self.emit_log,
                    progress_callback=self.emit_progress
                )
                res = self.downloader.download(self.link)

                if res.get("success"):
                    video_file = res.get("file_path")
                    # 1. Tạo SRT nếu bật
                    if self.auto_gen_srt and video_file and os.path.exists(video_file) and SubtitleGenerator:
                        self.log_signal.emit("🎙️ Kích hoạt tính năng Tự Động Tạo Phụ Đề SRT bằng Faster-Whisper AI...", "info")
                        self.srt_generator = SubtitleGenerator(
                            output_dir=self.output_dir,
                            log_callback=self.emit_log,
                            progress_callback=self.emit_progress
                        )
                        srt_res = self.srt_generator.generate_srt(video_file, model_size="base")
                        srt_file = srt_res.get("srt_path") if srt_res.get("success") else None

                        # 2. Dịch SRT sang Tiếng Việt nếu bật
                        if self.auto_translate_srt and srt_file and os.path.exists(srt_file) and run_auto_translate_srt:
                            self.log_signal.emit("🌐 Kích hoạt Gemini AI dịch phụ đề vừa tạo sang Tiếng Việt...", "info")
                            prompt_file = "./prompts/promptTranslates.md"
                            if not os.path.exists(prompt_file):
                                prompt_file = "./prompts/translate.txt"
                            src_dir = os.path.dirname(os.path.abspath(srt_file))
                            run_auto_translate_srt(
                                prompt_file=prompt_file,
                                cn_folder=src_dir,
                                vi_folder=self.output_dir,
                                wait_time=300,
                                log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl)
                            )
                            self.finished_signal.emit(True, f"🎉 Đã Tải Video ➔ Tạo Phụ Đề ➔ Dịch Tiếng Việt thành công cho: {res.get('title', '')}")
                        else:
                            self.finished_signal.emit(True, f"🎉 Đã tải video & tạo phụ đề SRT thành công: {res.get('title', '')}")
                    else:
                        self.finished_signal.emit(True, f"Đã tải xong video: {res.get('title', '')}")
                else:
                    self.finished_signal.emit(False, res.get("error", "Lỗi tải video"))
            else:
                self.log_signal.emit(f"🚀 Khởi tạo tiến trình xử lý cho link: {self.link}", "info")
                total_steps = 100
                for step in range(1, total_steps + 1):
                    self.mutex.lock()
                    while self._is_paused:
                        self.pause_condition.wait(self.mutex)
                    if self._is_stopped:
                        self.mutex.unlock()
                        self.finished_signal.emit(False, "Tiến trình đã bị người dùng hủy bỏ!")
                        return
                    self.mutex.unlock()

                    time.sleep(0.06)
                    status_msg = f"Đang xử lý dữ liệu... [{step}/{total_steps}]"
                    self.progress_signal.emit(step, status_msg)

                    if step % 20 == 0 or step == 1:
                        self.log_signal.emit(f"✓ Hoàn thành {step}%: {status_msg}", "info")

                self.finished_signal.emit(True, "🎉 Tiến trình đã hoàn thành 100%!")

        except Exception as e:
            self.log_signal.emit(f"❌ Lỗi ngoài dự kiến: {str(e)}", "error")
            self.finished_signal.emit(False, f"Lỗi: {str(e)}")


# ==============================================================================
# GIAO DIỆN CHÍNH CAO CẤP (GUI V2 PRO - AUTO DOWNLOAD BILIBILI)
# ==============================================================================
class MainWindowV2(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.is_paused = False
        self.start_timestamp = None
        self.logs_history = []
        
        # Debounce Timer tự động phát hiện dán Bilibili URL để tự tải
        self.auto_start_timer = QTimer(self)
        self.auto_start_timer.setSingleShot(True)
        self.auto_start_timer.setInterval(400)
        self.auto_start_timer.timeout.connect(self.check_and_auto_start)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_timer_display)

        self.init_ui()
        self.setup_shortcuts()

    def init_ui(self):
        self.setWindowTitle("Trình Tải Video Bilibili Tốc Độ Cao - GUI V2 PRO 🚀")
        self.resize(1080, 740)
        self.setMinimumSize(880, 580)

        self.setup_stylesheet()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)

        # ----------------------------------------------------------------------
        # 1. HEADER BANNER MODERN
        # ----------------------------------------------------------------------
        header_frame = QFrame()
        header_frame.setObjectName("HeaderFrame")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(20, 14, 20, 14)

        header_text_layout = QVBoxLayout()
        header_text_layout.setSpacing(4)

        title_label = QLabel("⚡ TRÌNH TẢI VIDEO BILIBILI CHUYÊN NGHIỆP (GUI V2 PRO)")
        title_label.setObjectName("MainTitle")
        
        subtitle_label = QLabel("Tự động tải video chất lượng cao nhất (1080p/4K MP4) | Chống bị chặn 403 & Rate Limit")
        subtitle_label.setObjectName("SubTitle")

        header_text_layout.addWidget(title_label)
        header_text_layout.addWidget(subtitle_label)
        header_layout.addLayout(header_text_layout, stretch=1)

        self.lbl_cookie_badge = QLabel("🍪 COOKIE: CHƯA CÓ")
        self.lbl_cookie_badge.setObjectName("SystemBadge")
        self.lbl_cookie_badge.setStyleSheet("background-color: #334155; color: #94a3b8;")

        self.lbl_system_badge = QLabel("🟢 SẴN SÀNG")
        self.lbl_system_badge.setObjectName("SystemBadge")
        
        header_layout.addWidget(self.lbl_cookie_badge)
        header_layout.addWidget(self.lbl_system_badge)

        main_layout.addWidget(header_frame)

        # ----------------------------------------------------------------------
        # 2. CHỌN KHU VỰC VỚI QSPLITTER (CHO PHÉP KÉO THAY ĐỔI KÍCH THƯỚC)
        # ----------------------------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(14)

        # --- Card 1: Cấu hình & Ô Nhập Link ---
        input_group = QGroupBox("📌 Nhập Link Video Bilibili (Tự Động Bắt Đầu Tải Khi Dán Link)")
        input_group.setObjectName("InputGroup")
        input_layout = QVBoxLayout(input_group)
        input_layout.setContentsMargins(18, 18, 18, 18)
        input_layout.setSpacing(14)

        # Dòng nhập Link
        link_box_layout = QHBoxLayout()
        link_box_layout.setSpacing(10)

        lbl_link = QLabel("🔗 Link Bilibili:")
        lbl_link.setObjectName("InputLabel")

        self.txt_link = QLineEdit()
        self.txt_link.setObjectName("LinkInput")
        self.txt_link.setPlaceholderText("Dán liên kết Bilibili (https://www.bilibili.com/video/BV...) vào đây...")
        self.txt_link.setToolTip("Khi dán link Bilibili hợp lệ, hệ thống sẽ TỰ ĐỘNG khởi chạy tiến trình tải ngay!")
        self.txt_link.textChanged.connect(self.on_link_text_changed)

        self.btn_paste_link = QToolButton()
        self.btn_paste_link.setText("📋 Dán & Tải Ngay")
        self.btn_paste_link.setObjectName("ToolBtnAccent")
        self.btn_paste_link.setToolTip("Dán nhanh nội dung từ Clipboard và TỰ ĐỘNG TẢI NGAY")
        self.btn_paste_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste_link.clicked.connect(self.paste_and_auto_run)

        self.btn_clear_link = QToolButton()
        self.btn_clear_link.setText("❌ Xóa")
        self.btn_clear_link.setObjectName("ToolBtn")
        self.btn_clear_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_link.clicked.connect(lambda: self.txt_link.clear())

        link_box_layout.addWidget(lbl_link)
        link_box_layout.addWidget(self.txt_link, stretch=1)
        link_box_layout.addWidget(self.btn_paste_link)
        link_box_layout.addWidget(self.btn_clear_link)
        input_layout.addLayout(link_box_layout)

        # Dòng Tùy Chọn Thư Mục Lưu & Autostart
        opt_layout = QHBoxLayout()
        opt_layout.setSpacing(12)

        lbl_dir = QLabel("📁 Thư mục lưu:")
        lbl_dir.setStyleSheet("color: #cbd5e1; font-weight: 600; font-size: 12px;")

        self.txt_output_dir = QLineEdit("./downloads")
        self.txt_output_dir.setObjectName("DirInput")
        self.txt_output_dir.setReadOnly(True)

        self.btn_browse_dir = QToolButton()
        self.btn_browse_dir.setText("📂 Chọn...")
        self.btn_browse_dir.setObjectName("ToolBtn")
        self.btn_browse_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_dir.clicked.connect(self.browse_output_directory)

        self.chk_auto_download = QCheckBox("⚡ Tự động tải ngay khi dán Link")
        self.chk_auto_download.setObjectName("AutoScrollCheck")
        self.chk_auto_download.setChecked(True)
        self.chk_auto_download.setToolTip("Khi bật tùy chọn này, dán đường dẫn Bilibili sẽ tự động bắt đầu tải mà không cần nhấn nút")

        self.chk_auto_srt = QCheckBox("🎙️ Tự động tạo phụ đề .srt sau khi tải")
        self.chk_auto_srt.setObjectName("AutoScrollCheck")
        self.chk_auto_srt.setChecked(False)
        self.chk_auto_srt.setToolTip("Tự động dùng AI Faster-Whisper chuyển giọng nói trong video vừa tải thành tệp phụ đề .srt chuẩn")

        self.chk_auto_translate = QCheckBox("🌐 Tự động Dịch sang Tiếng Việt (Gemini AI)")
        self.chk_auto_translate.setObjectName("AutoScrollCheck")
        self.chk_auto_translate.setChecked(False)
        self.chk_auto_translate.setToolTip("Tự động dùng Gemini AI kết hợp promptTranslates.md dịch tệp phụ đề .srt vừa tạo sang Tiếng Việt mượt mà")

        opt_layout.addWidget(lbl_dir)
        opt_layout.addWidget(self.txt_output_dir, stretch=1)
        opt_layout.addWidget(self.btn_browse_dir)
        opt_layout.addWidget(self.chk_auto_download)
        opt_layout.addWidget(self.chk_auto_srt)
        opt_layout.addWidget(self.chk_auto_translate)
        input_layout.addLayout(opt_layout)

        # Dòng Buttons điều khiển
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self.btn_run = QPushButton("🚀 TẢI VIDEO NGAY")
        self.btn_run.setObjectName("BtnRun")
        self.btn_run.setToolTip("Bắt đầu tải video Bilibili chất lượng cao nhất (Ctrl+Enter)")
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.clicked.connect(self.on_run_clicked)

        self.btn_gen_srt = QPushButton("🎙️ TẠO PHỤ ĐỀ SRT")
        self.btn_gen_srt.setObjectName("BtnPause")
        self.btn_gen_srt.setToolTip("Chọn tệp Video hoặc Audio từ máy tính để AI nhận diện giọng nói và xuất tệp .srt")
        self.btn_gen_srt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gen_srt.clicked.connect(self.on_select_file_for_srt)

        self.btn_translate_srt = QPushButton("🌐 DỊCH SRT SANG VIỆT")
        self.btn_translate_srt.setObjectName("BtnPause")
        self.btn_translate_srt.setToolTip("Chọn tệp phụ đề .srt Tiếng Trung/Anh từ máy tính để Gemini AI dịch sang Tiếng Việt qua promptTranslates.md")
        self.btn_translate_srt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_translate_srt.clicked.connect(self.on_select_srt_for_translation)

        self.btn_pause = QPushButton("⏸️ TẠM DỪNG")
        self.btn_pause.setObjectName("BtnPause")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.on_pause_clicked)

        self.btn_stop = QPushButton("🛑 DỪNG LẠI")
        self.btn_stop.setObjectName("BtnStop")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop_clicked)

        btn_layout.addWidget(self.btn_run)
        btn_layout.addWidget(self.btn_gen_srt)
        btn_layout.addWidget(self.btn_translate_srt)
        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addStretch()

        input_layout.addLayout(btn_layout)
        top_layout.addWidget(input_group)

        # --- Card 2: Bảng Điều Khiển KPI & Tiến Độ ---
        progress_group = QGroupBox("📊 Tiến Độ Tải & Chỉ Số Thực Thi")
        progress_group.setObjectName("ProgressGroup")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setContentsMargins(18, 16, 18, 16)
        progress_layout.setSpacing(12)

        kpi_layout = QHBoxLayout()
        kpi_layout.setSpacing(12)

        self.card_status = self.create_kpi_card("TRẠNG THÁI", "Đang chờ lệnh", "#94a3b8", "kpi_status")
        self.card_elapsed = self.create_kpi_card("THỜI GIAN ĐÃ CHẠY", "00:00:00", "#38bdf8", "kpi_elapsed")
        self.card_eta = self.create_kpi_card("DỰ KIẾN CÒN LẠI", "--:--", "#a78bfa", "kpi_eta")
        self.card_percent = self.create_kpi_card("TIẾN ĐỘ TẢI", "0%", "#10b981", "kpi_percent")

        kpi_layout.addWidget(self.card_status, stretch=2)
        kpi_layout.addWidget(self.card_elapsed, stretch=1)
        kpi_layout.addWidget(self.card_eta, stretch=1)
        kpi_layout.addWidget(self.card_percent, stretch=1)

        progress_layout.addLayout(kpi_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("CustomProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)

        progress_layout.addWidget(self.progress_bar)
        top_layout.addWidget(progress_group)

        splitter.addWidget(top_panel)

        # === PANEL DƯỚI: CONSOLE LOG ===
        console_group = QGroupBox("🖥️ Monitor Nhật Ký Tiến Trình (Console Log)")
        console_group.setObjectName("ConsoleGroup")
        console_layout = QVBoxLayout(console_group)
        console_layout.setContentsMargins(16, 14, 16, 14)
        console_layout.setSpacing(10)

        console_toolbar = QHBoxLayout()
        console_toolbar.setSpacing(10)

        lbl_filter = QLabel("🔍 Bộ lọc:")
        lbl_filter.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 12px;")

        self.cbo_log_level = QComboBox()
        self.cbo_log_level.setObjectName("LogFilterCombo")
        self.cbo_log_level.addItems(["Tất cả log", "ℹ️ Info", "✅ Success", "⚠️ Warning", "❌ Error"])
        self.cbo_log_level.currentIndexChanged.connect(self.filter_logs)

        self.txt_log_search = QLineEdit()
        self.txt_log_search.setObjectName("LogSearchInput")
        self.txt_log_search.setPlaceholderText("Tìm từ khóa trong log...")
        self.txt_log_search.textChanged.connect(self.filter_logs)

        self.chk_autoscroll = QCheckBox("📌 Cuộn tự động")
        self.chk_autoscroll.setObjectName("AutoScrollCheck")
        self.chk_autoscroll.setChecked(True)

        self.btn_copy_log = QPushButton("📋 Sao chép")
        self.btn_copy_log.setObjectName("BtnSmall")
        self.btn_copy_log.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy_log.clicked.connect(self.copy_logs)

        self.btn_clear_log = QPushButton("🗑️ Xóa Log")
        self.btn_clear_log.setObjectName("BtnSmall")
        self.btn_clear_log.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_log.clicked.connect(self.clear_console)

        console_toolbar.addWidget(lbl_filter)
        console_toolbar.addWidget(self.cbo_log_level)
        console_toolbar.addWidget(self.txt_log_search, stretch=1)
        console_toolbar.addWidget(self.chk_autoscroll)
        console_toolbar.addWidget(self.btn_copy_log)
        console_toolbar.addWidget(self.btn_clear_log)

        self.txt_console = QTextEdit()
        self.txt_console.setObjectName("ConsoleMonitor")
        self.txt_console.setReadOnly(True)

        console_layout.addLayout(console_toolbar)
        console_layout.addWidget(self.txt_console, stretch=1)

        splitter.addWidget(console_group)
        splitter.setSizes([340, 360])

        main_layout.addWidget(splitter, stretch=1)

        self.append_log("Chào mừng bạn tới GUI V2 PRO! Dán đường dẫn Bilibili để TỰ ĐỘNG TẢI NGAY.", "info")
        self.update_cookie_badge_status()

    # ==========================================================================
    # KHỦNG TRỢ GIÚP UI & AUTO DETECT ON PASTE
    # ==========================================================================
    def create_kpi_card(self, title: str, initial_value: str, color_hex: str, object_name: str) -> QFrame:
        card = QFrame()
        card.setObjectName("KpiCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        lbl_t = QLabel(title)
        lbl_t.setObjectName("KpiTitle")

        lbl_v = QLabel(initial_value)
        lbl_v.setObjectName(object_name)
        lbl_v.setStyleSheet(f"color: {color_hex}; font-size: 15px; font-weight: bold;")

        layout.addWidget(lbl_t)
        layout.addWidget(lbl_v)
        return card

    def update_kpi_value(self, object_name: str, value_text: str, color_hex: str = None):
        lbl = self.findChild(QLabel, object_name)
        if lbl:
            lbl.setText(value_text)
            if color_hex:
                lbl.setStyleSheet(f"color: {color_hex}; font-size: 15px; font-weight: bold;")

    def setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.on_run_clicked)
        QShortcut(QKeySequence("Ctrl+P"), self).activated.connect(self.on_pause_clicked)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self.clear_console)

    def update_cookie_badge_status(self):
        possible_files = ["./cookie.txt", "./cookies.txt", "./downloads/cookie.txt", "./downloads/cookies.txt"]
        has_cookie = False
        for p in possible_files:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().strip()
                        if content.startswith("[") and content.endswith("]"):
                            import json
                            data = json.loads(content)
                            netscape_content = "# Netscape HTTP Cookie File\n\n"
                            for item in data:
                                name = item.get("name", "")
                                value = item.get("value", "")
                                exp = int(item.get("expirationDate", 2147483647))
                                netscape_content += f".bilibili.com\tTRUE\t/\tFALSE\t{exp}\t{name}\t{value}\n"
                            with open("./cookies.txt", "w", encoding="utf-8") as out:
                                out.write(netscape_content)
                            with open("./downloads/cookies.txt", "w", encoding="utf-8") as out:
                                out.write(netscape_content)
                            has_cookie = True
                            break
                        elif "SESSDATA" in content or "bili_jct" in content:
                            has_cookie = True
                            break
                except Exception:
                    pass

        if has_cookie:
            self.lbl_cookie_badge.setText("🍪 COOKIE VIP: ĐÃ NẠP")
            self.lbl_cookie_badge.setStyleSheet("background-color: #059669; color: white; font-weight: bold;")
        else:
            self.lbl_cookie_badge.setText("🍪 COOKIE: CHƯA CÓ")
            self.lbl_cookie_badge.setStyleSheet("background-color: #334155; color: #94a3b8; font-weight: bold;")

    def browse_output_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video tải về")
        if dir_path:
            self.txt_output_dir.setText(dir_path)
            self.append_log(f"📁 Đã đổi thư mục lưu: {dir_path}", "info")

    def on_link_text_changed(self):
        """Kích hoạt timer kiểm tra khi text thay đổi"""
        if self.chk_auto_download.isChecked():
            self.auto_start_timer.start()

    def check_and_auto_start(self):
        """Nếu phát hiện link Bilibili hợp lệ và worker đang rảnh -> TỰ ĐỘNG CHẠY"""
        raw_text = self.txt_link.text().strip()
        if not raw_text:
            return

        clean_url = BilibiliDownloader.extract_bilibili_url(raw_text) if BilibiliDownloader else None
        is_running = self.worker and self.worker.isRunning()
        
        if not is_running and clean_url:
            # Tự chuẩn hóa về URL sạch
            if raw_text != clean_url:
                self.txt_link.setText(clean_url)
            self.append_log(f"⚡ Tự động phát hiện liên kết Bilibili hợp lệ: {clean_url}! Khởi động tải ngay...", "info")
            self.on_run_clicked()

    def paste_and_auto_run(self):
        clipboard = QApplication.clipboard()
        raw_text = clipboard.text().strip()
        if not raw_text:
            return

        clean_url = BilibiliDownloader.extract_bilibili_url(raw_text) if BilibiliDownloader else None
        if clean_url:
            self.txt_link.setText(clean_url)
            self.append_log(f"📋 Đã trích xuất & dán URL Bilibili: {clean_url}", "info")
            QTimer.singleShot(100, self.on_run_clicked)
        else:
            self.append_log("⚠️ Nội dung trong Bộ nhớ tạm (Clipboard) không chứa liên kết Bilibili hợp lệ!", "warning")
            QMessageBox.warning(
                self, "Không Tìm Thấy Link Hợp Lệ",
                "⚠️ Nội dung vừa dán từ Clipboard không chứa liên kết Bilibili hợp lệ!\n\nVui lòng copy một đường dẫn video Bilibili (ví dụ: https://www.bilibili.com/video/BV...)."
            )

    def update_timer_display(self):
        if self.start_timestamp:
            elapsed_sec = int(time.time() - self.start_timestamp)
            hours, remainder = divmod(elapsed_sec, 3600)
            minutes, seconds = divmod(remainder, 60)
            time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            self.update_kpi_value("kpi_elapsed", time_str, "#38bdf8")

    # ==========================================================================
    # CÁC HÀM XỬ LÝ SỰ KIỆN NÚT BẤM (SLOTS)
    # ==========================================================================
    def on_run_clicked(self):
        raw_input = self.txt_link.text().strip()
        if not raw_input:
            QMessageBox.warning(
                self, "Cảnh Báo Dữ Liệu", 
                "⚠️ Vui lòng nhập hoặc dán đường dẫn Link Bilibili trước khi tải!"
            )
            self.txt_link.setFocus()
            return

        clean_url = BilibiliDownloader.extract_bilibili_url(raw_input) if BilibiliDownloader else None
        if not clean_url and not BilibiliDownloader.is_valid_bilibili_url(raw_input):
            QMessageBox.warning(
                self, "Cảnh Báo Dữ Liệu", 
                "⚠️ Ô nhập liệu không chứa đường dẫn Link Bilibili hợp lệ!"
            )
            self.txt_link.setFocus()
            return

        target_url = clean_url if clean_url else raw_input

        # Đang chạy thì không bấm trùng
        if self.worker and self.worker.isRunning():
            return

        out_dir = self.txt_output_dir.text().strip() or "./downloads"

        # Cập nhật UI State
        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.txt_link.setEnabled(False)
        self.btn_paste_link.setEnabled(False)
        self.btn_clear_link.setEnabled(False)

        self.btn_pause.setText("⏸️ TẠM DỪNG")
        self.btn_pause.setProperty("class", "")
        self.style().unpolish(self.btn_pause)
        self.style().polish(self.btn_pause)
        self.is_paused = False

        self.progress_bar.setValue(0)
        self.update_kpi_value("kpi_percent", "0%", "#10b981")
        self.update_kpi_value("kpi_status", "Đang kết nối ⚡", "#6366f1")
        self.update_kpi_value("kpi_elapsed", "00:00:00", "#38bdf8")
        self.update_kpi_value("kpi_eta", "Tự động...", "#a78bfa")
        
        self.lbl_system_badge.setText("⚡ ĐANG TẢI")
        self.lbl_system_badge.setStyleSheet("background-color: #0284c7; color: white;")

        self.start_timestamp = time.time()
        self.timer.start()

        # Khởi chạy Worker Thread
        auto_srt = self.chk_auto_srt.isChecked()
        auto_trans = self.chk_auto_translate.isChecked()
        self.worker = ProcessWorker(target_url, output_dir=out_dir, auto_gen_srt=auto_srt, auto_translate_srt=auto_trans)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_process_finished)
        self.worker.start()

    def on_select_file_for_srt(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn file Video hoặc Audio để Tạo Phụ Đề SRT",
            "",
            "Tệp Media (*.mp4 *.mkv *.m4v *.mov *.avi *.mp3 *.wav *.m4a);;Tất cả tệp (*.*)"
        )
        if not file_path:
            return

        out_dir = self.txt_output_dir.text().strip() or "./downloads"

        # Cập nhật UI State
        self.btn_run.setEnabled(False)
        self.btn_gen_srt.setEnabled(False)
        self.btn_translate_srt.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

        self.progress_bar.setValue(0)
        self.update_kpi_value("kpi_percent", "0%", "#10b981")
        self.update_kpi_value("kpi_status", "Đang phân tích 🎙️", "#6366f1")
        self.update_kpi_value("kpi_elapsed", "00:00:00", "#38bdf8")
        
        self.lbl_system_badge.setText("🎙️ TẠO SRT")
        self.lbl_system_badge.setStyleSheet("background-color: #7c3aed; color: white;")

        self.start_timestamp = time.time()
        self.timer.start()

        auto_trans = self.chk_auto_translate.isChecked()
        self.worker = ProcessWorker(link="", output_dir=out_dir, auto_translate_srt=auto_trans, local_media_path=file_path)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_process_finished)
        self.worker.start()

    def on_select_srt_for_translation(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn tệp Phụ Đề SRT Tiếng Trung/Anh để Dịch Sang Tiếng Việt",
            "",
            "Tệp Phụ Đề (*.srt);;Tất cả tệp (*.*)"
        )
        if not file_path:
            return

        out_dir = self.txt_output_dir.text().strip() or "./downloads"

        # Cập nhật UI State
        self.btn_run.setEnabled(False)
        self.btn_gen_srt.setEnabled(False)
        self.btn_translate_srt.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

        self.progress_bar.setValue(0)
        self.update_kpi_value("kpi_percent", "0%", "#10b981")
        self.update_kpi_value("kpi_status", "Đang dịch AI 🌐", "#6366f1")
        self.update_kpi_value("kpi_elapsed", "00:00:00", "#38bdf8")
        
        self.lbl_system_badge.setText("🌐 DỊCH AI")
        self.lbl_system_badge.setStyleSheet("background-color: #059669; color: white;")

        self.start_timestamp = time.time()
        self.timer.start()

        self.worker = ProcessWorker(link="", output_dir=out_dir, srt_translate_path=file_path)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_process_finished)
        self.worker.start()

    def on_pause_clicked(self):
        if not self.worker or not self.worker.isRunning():
            return

        if not self.is_paused:
            self.worker.pause()
            self.is_paused = True
            self.timer.stop()
            self.btn_pause.setText("▶️ TIẾP TỤC")
            self.btn_pause.setProperty("class", "resume")
            self.style().unpolish(self.btn_pause)
            self.style().polish(self.btn_pause)
            
            self.update_kpi_value("kpi_status", "ĐÃ TẠM DỪNG ⏸️", "#f59e0b")
            self.lbl_system_badge.setText("⏸️ TẠM DỪNG")
            self.lbl_system_badge.setStyleSheet("background-color: #d97706; color: white;")
        else:
            self.worker.resume()
            self.is_paused = False
            self.timer.start()
            self.btn_pause.setText("⏸️ TẠM DỪNG")
            self.btn_pause.setProperty("class", "")
            self.style().unpolish(self.btn_pause)
            self.style().polish(self.btn_pause)
            
            self.update_kpi_value("kpi_status", "Đang tải ⚡", "#6366f1")
            self.lbl_system_badge.setText("⚡ ĐANG TẢI")
            self.lbl_system_badge.setStyleSheet("background-color: #0284c7; color: white;")

    def on_stop_clicked(self):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "Xác Nhận Dừng Tải", 
                "🛑 Bạn có chắc chắn muốn DỪNG TOÀN BỘ tiến trình tải video đang chạy?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.btn_stop.setEnabled(False)
                self.btn_pause.setEnabled(False)
                self.worker.stop()

    def on_process_finished(self, success: bool, message: str):
        self.timer.stop()
        self.btn_run.setEnabled(True)
        self.btn_gen_srt.setEnabled(True)
        self.btn_translate_srt.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.txt_link.setEnabled(True)
        self.btn_paste_link.setEnabled(True)
        self.btn_clear_link.setEnabled(True)
        
        self.btn_pause.setText("⏸️ TẠM DỪNG")
        self.is_paused = False

        if success:
            self.update_kpi_value("kpi_status", "HOÀN THÀNH 🎉", "#10b981")
            self.update_kpi_value("kpi_percent", "100%", "#10b981")
            self.lbl_system_badge.setText("✅ HOÀN THÀNH")
            self.lbl_system_badge.setStyleSheet("background-color: #059669; color: white;")
            self.append_log(f"SUCCESS: {message}", "success")
        else:
            self.update_kpi_value("kpi_status", "ĐÃ HỦY / LỖI 🛑", "#ef4444")
            self.lbl_system_badge.setText("🛑 ĐÃ DỪNG")
            self.lbl_system_badge.setStyleSheet("background-color: #dc2626; color: white;")
            self.append_log(f"STOPPED: {message}", "error")

    def update_progress(self, percentage: int, status_text: str):
        if percentage >= 0:
            self.progress_bar.setValue(percentage)
            self.update_kpi_value("kpi_percent", f"{percentage}%", "#10b981")
            self.update_kpi_value("kpi_status", status_text, "#38bdf8")

    # ==========================================================================
    # CÁC XỬ LÝ LOG CONSOLE
    # ==========================================================================
    def append_log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs_history.append((timestamp, message, level))
        self.render_single_log(timestamp, message, level)

    def render_single_log(self, timestamp: str, message: str, level: str):
        colors = {
            "info": "#94a3b8",      
            "success": "#10b981",   
            "warning": "#f59e0b",   
            "error": "#ef4444"      
        }
        color = colors.get(level, "#94a3b8")
        formatted_html = f'<span style="color: #475569; font-size: 11px;">[{timestamp}]</span> <span style="color: {color}; font-weight: 500;">{message}</span>'
        self.txt_console.append(formatted_html)

        if self.chk_autoscroll.isChecked():
            cursor = self.txt_console.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.txt_console.setTextCursor(cursor)

    def filter_logs(self):
        filter_level_idx = self.cbo_log_level.currentIndex()
        search_kw = self.txt_log_search.text().strip().lower()

        level_map = {1: "info", 2: "success", 3: "warning", 4: "error"}
        target_level = level_map.get(filter_level_idx, None)

        self.txt_console.clear()
        for ts, msg, lvl in self.logs_history:
            if target_level and lvl != target_level:
                continue
            if search_kw and search_kw not in msg.lower():
                continue
            self.render_single_log(ts, msg, lvl)

    def copy_logs(self):
        text = self.txt_console.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.append_log("📋 Đã sao chép toàn bộ nội dung nhật ký log vào Clipboard.", "info")

    def clear_console(self):
        self.logs_history.clear()
        self.txt_console.clear()
        self.append_log("Đã xóa toàn bộ nhật ký hệ thống.", "info")

    # ==========================================================================
    # DESIGN SYSTEM STYLESHEET (DARK GLASSMORPHISM MODERN THEME)
    # ==========================================================================
    def setup_stylesheet(self):
        qss = """
        QMainWindow {
            background-color: #090d16;
            font-family: 'Segoe UI', -apple-system, Roboto, sans-serif;
        }

        QSplitter::handle {
            background-color: #1e293b;
            height: 6px;
            margin: 2px 0;
            border-radius: 3px;
        }
        QSplitter::handle:hover {
            background-color: #38bdf8;
        }

        #HeaderFrame {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1e293b, stop:1 #0f172a);
            border: 1px solid #334155;
            border-radius: 12px;
        }
        #MainTitle {
            color: #f8fafc;
            font-size: 17px;
            font-weight: 800;
        }
        #SubTitle {
            color: #94a3b8;
            font-size: 12px;
        }
        #SystemBadge {
            background-color: #059669;
            color: white;
            font-size: 11px;
            font-weight: bold;
            padding: 6px 14px;
            border-radius: 14px;
        }

        QGroupBox {
            color: #38bdf8;
            font-size: 13px;
            font-weight: 700;
            background-color: #131c2e;
            border: 1px solid #1e293b;
            border-radius: 12px;
            margin-top: 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 8px;
            color: #38bdf8;
        }

        #InputLabel {
            color: #e2e8f0;
            font-size: 13px;
            font-weight: 600;
        }
        #LinkInput, #DirInput {
            background-color: #090d16;
            color: #f8fafc;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 9px 12px;
            font-size: 13px;
        }
        #LinkInput:focus {
            border: 1px solid #38bdf8;
            background-color: #0f172a;
        }

        #ToolBtn {
            background-color: #1e293b;
            color: #cbd5e1;
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 12px;
            font-weight: 600;
        }
        #ToolBtn:hover {
            background-color: #334155;
            color: white;
        }

        #ToolBtnAccent {
            background-color: #0284c7;
            color: white;
            border: 1px solid #38bdf8;
            border-radius: 6px;
            padding: 6px 12px;
            font-size: 12px;
            font-weight: 700;
        }
        #ToolBtnAccent:hover {
            background-color: #0369a1;
        }

        QPushButton {
            font-size: 13px;
            font-weight: 700;
            border-radius: 8px;
            padding: 10px 22px;
            border: none;
            color: white;
        }

        #BtnRun {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #059669, stop:1 #10b981);
        }
        #BtnRun:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #34d399);
        }
        #BtnRun:disabled {
            background-color: #1e293b;
            color: #475569;
        }

        #BtnPause {
            background-color: #d97706;
        }
        #BtnPause:hover {
            background-color: #f59e0b;
        }
        #BtnPause[class="resume"] {
            background-color: #2563eb;
        }
        #BtnPause[class="resume"]:hover {
            background-color: #3b82f6;
        }
        #BtnPause:disabled {
            background-color: #1e293b;
            color: #475569;
        }

        #BtnStop {
            background-color: #dc2626;
        }
        #BtnStop:hover {
            background-color: #ef4444;
        }
        #BtnStop:disabled {
            background-color: #1e293b;
            color: #475569;
        }

        #BtnSmall {
            background-color: #1e293b;
            color: #cbd5e1;
            font-size: 11px;
            font-weight: 600;
            padding: 6px 12px;
            border-radius: 6px;
            border: 1px solid #334155;
        }
        #BtnSmall:hover {
            background-color: #334155;
            color: white;
        }

        #KpiCard {
            background-color: #090d16;
            border: 1px solid #1e293b;
            border-radius: 8px;
        }
        #KpiTitle {
            color: #64748b;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }

        #CustomProgressBar {
            background-color: #090d16;
            border: 1px solid #1e293b;
            border-radius: 8px;
            height: 14px;
            text-align: center;
        }
        #CustomProgressBar::chunk {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6366f1, stop:0.5 #38bdf8, stop:1 #10b981);
            border-radius: 6px;
        }

        #LogFilterCombo, #LogSearchInput {
            background-color: #090d16;
            color: #e2e8f0;
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 12px;
        }
        #AutoScrollCheck {
            color: #94a3b8;
            font-size: 12px;
        }

        #ConsoleMonitor {
            background-color: #050811;
            color: #94a3b8;
            font-family: 'Consolas', 'Fira Code', 'Courier New', monospace;
            font-size: 12px;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 10px;
        }
        """
        self.setStyleSheet(qss)


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
def main():
    app = QApplication(sys.argv)
    window = MainWindowV2()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
