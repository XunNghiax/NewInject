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

try:
    from qa_srt_before import analyze_srt_to_file
except ImportError:
    analyze_srt_to_file = None

try:
    from auto_qa_repair import run_auto_qa_repair
except ImportError:
    run_auto_qa_repair = None

try:
    from srt_utils import process_and_renumber_srt, merge_numbered_srt_files, process_srt_speed, split_srt_file
except ImportError:
    process_and_renumber_srt = None
    merge_numbered_srt_files = None
    process_srt_speed = None
    split_srt_file = None


# ==============================================================================
# WORKER THREAD ĐỒNG BỘ: HỖ TRỢ TẢI REAL-TIME BILIBILI & GIẢ LẬP
# ==============================================================================
class ProcessWorker(QThread):
    log_signal = pyqtSignal(str, str)         # (log_msg, log_level)
    progress_signal = pyqtSignal(int, str)    # (percentage, status_text)
    finished_signal = pyqtSignal(bool, str)   # (success, final_message)

    def __init__(self, link: str, output_dir: str = "./downloads", auto_gen_srt: bool = False, auto_translate_srt: bool = False, local_media_path: str = None, srt_translate_path: str = None, qa_scan_path: str = None, qa_repair_mode: bool = False):
        super().__init__()
        self.link = link
        self.output_dir = output_dir
        self.auto_gen_srt = auto_gen_srt
        self.auto_translate_srt = auto_translate_srt
        self.local_media_path = local_media_path
        self.srt_translate_path = srt_translate_path
        self.qa_scan_path = qa_scan_path
        self.qa_repair_mode = qa_repair_mode
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

    def check_pause(self):
        """Kiểm tra và tạm dừng thread ngay lập tức nếu có lệnh pause"""
        self.mutex.lock()
        while self._is_paused:
            self.pause_condition.wait(self.mutex)
        if self._is_stopped:
            self.mutex.unlock()
            raise Exception("Tiến trình đã bị người dùng hủy bỏ!")
        self.mutex.unlock()

    def run(self):
        try:
            root_downloads = os.path.abspath(self.output_dir)
            os.makedirs(root_downloads, exist_ok=True)

            self.emit_progress(5, "⚡ Khởi động Dự Án Tự Động...")
            self.emit_log("==================================================", "info")
            self.emit_log("🚀 BẮT ĐẦU QUY TRÌNH XỬ LÝ DỰ ÁN TỰ ĐỘNG (END-TO-END)", "info")
            self.emit_log("==================================================", "info")

            # ── BƯỚC 1: KIỂM TRA THƯ MỤC & TẢI VIDEO ──
            video_file = None
            for f in os.listdir(root_downloads):
                if f.lower().endswith(('.mp4', '.mkv', '.flv', '.webm')) and not f.endswith('.part'):
                    video_file = os.path.join(root_downloads, f)
                    break

            if not video_file and self.link and BilibiliDownloader and BilibiliDownloader.is_valid_bilibili_url(self.link):
                self.emit_log(f"📥 Thư mục trống. Khởi động tải Video từ Bilibili: {self.link}...", "info")
                self.emit_progress(10, "Đang tải Video Bilibili...")
                self.downloader = BilibiliDownloader(
                    output_dir=root_downloads,
                    log_callback=self.emit_log,
                    progress_callback=self.emit_progress
                )
                res = self.downloader.download(self.link)
                if res.get("success"):
                    video_file = res.get("file_path")
                else:
                    self.finished_signal.emit(False, res.get("error", "Lỗi tải video Bilibili"))
                    return

            if video_file:
                raw_title = os.path.splitext(os.path.basename(video_file))[0]
                self.emit_log(f"📹 Tệp Video dự án: {os.path.basename(video_file)}", "info")
            else:
                raw_title = "DuAn_Auto"

            cn_folder = os.path.join(root_downloads, f"temp_split_cn_{raw_title}")
            vi_folder = os.path.join(root_downloads, f"temp_split_vi_{raw_title}")

            # ── BƯỚC 2: KIỂM TRA THƯ MỤC SRT TRUNG QUỐC (temp_split_cn_) ──
            has_cn_splits = os.path.exists(cn_folder) and any(f.endswith('.srt') for f in os.listdir(cn_folder))

            if not has_cn_splits:
                self.emit_log(f"🎙️ Chưa có thư mục SRT Trung Quốc. Tiến hành tạo & xử lý phụ đề gốc...", "info")
                raw_srt = None
                for f in os.listdir(root_downloads):
                    if f.lower().endswith('.srt') and not f.endswith('_vi.srt') and not f.endswith('_08.srt') and not f.endswith('_speed08.srt'):
                        raw_srt = os.path.join(root_downloads, f)
                        break

                if not raw_srt and video_file and SubtitleGenerator:
                    self.emit_progress(25, "Đang nhận diện giọng nói tạo phụ đề SRT...")
                    self.srt_generator = SubtitleGenerator(
                        output_dir=root_downloads,
                        log_callback=self.emit_log,
                        progress_callback=self.emit_progress
                    )
                    srt_res = self.srt_generator.generate_srt(video_file, model_size="base")
                    if srt_res.get("success"):
                        raw_srt = srt_res.get("srt_path")

                if not raw_srt:
                    self.finished_signal.emit(False, "❌ Không thể tạo hoặc tìm thấy file phụ đề SRT gốc!")
                    return

                # 2b: Chuyển đổi tốc độ từ 1.0 sang 0.8 (CHỈ 1 LẦN DUY NHẤT từ file SRT gốc)
                srt_08 = os.path.join(root_downloads, f"{raw_title}_speed08.srt")
                if not os.path.exists(srt_08):
                    self.emit_log("⚡ Đang tự động chuyển đổi tốc độ phụ đề SRT gốc từ 1.0x sang 0.8x (Chỉ thực hiện 1 lần duy nhất)...", "info")
                    if process_srt_speed:
                        process_srt_speed(raw_srt, srt_08, 1.0, 0.8, log_callback=lambda msg: self.emit_log(msg, "info"))
                    else:
                        srt_08 = raw_srt

                # 2c: Lấy tệp SRT 0.8x vừa tạo chia nhỏ thành 100 block/file lưu vào thư mục cn
                self.emit_log(f"📁 Đang chia nhỏ tệp SRT 0.8x thành 100 block/file lưu vào thư mục Trung Quốc: {cn_folder}...", "info")
                os.makedirs(cn_folder, exist_ok=True)
                if split_srt_file:
                    prefix_path = os.path.join(cn_folder, "part")
                    split_srt_file(srt_08, output_prefix=prefix_path, blocks_per_file=100, log_callback=lambda msg: self.emit_log(msg, "info"))
            else:
                self.emit_log(f"✅ Đã tìm thấy thư mục phụ đề Trung Quốc: {cn_folder}", "info")

            # ── BƯỚC 3: DỊCH THUẬT CN -> VI (temp_split_vi_) ──
            self.emit_progress(40, "Đang kiểm tra & chạy Gemini AI dịch Tiếng Việt...")
            os.makedirs(vi_folder, exist_ok=True)

            cn_splits = [f for f in os.listdir(cn_folder) if f.endswith('.srt')] if os.path.exists(cn_folder) else []
            vi_splits = [f for f in os.listdir(vi_folder) if f.endswith('.srt')] if os.path.exists(vi_folder) else []

            def check_split_translated(cf_name):
                raw_bname = os.path.splitext(cf_name)[0]
                cand1 = os.path.join(vi_folder, cf_name)
                cand2 = os.path.join(vi_folder, f"{raw_bname}_vi.srt")
                return (os.path.exists(cand1) and os.path.getsize(cand1) > 50) or (os.path.exists(cand2) and os.path.getsize(cand2) > 50)

            is_all_vi_translated = (
                len(cn_splits) > 0 and
                len(vi_splits) >= len(cn_splits) and
                all(check_split_translated(cf) for cf in cn_splits)
            )

            if is_all_vi_translated:
                self.emit_log(f"⏩ [CHECKPOINT] Đã tìm thấy đầy đủ {len(vi_splits)} phân đoạn Tiếng Việt hoàn chỉnh tại: {vi_folder}. BỎ QUA BƯỚC DỊCH THUẬT, CHUYỂN SANG BƯỚC TỐI ƯU QA!", "success")
            else:
                self.emit_log(f"🌐 Đang dịch các phân đoạn từ '{cn_folder}' sang Tiếng Việt lưu tại '{vi_folder}'...", "info")
                prompt_trans = "./user_data/prompts/promptTranslates.md"
                if not os.path.exists(prompt_trans):
                    prompt_trans = "./prompts/promptTranslates.md"

                if run_auto_translate_srt:
                    run_auto_translate_srt(
                        prompt_file=prompt_trans,
                        cn_folder=cn_folder,
                        vi_folder=vi_folder,
                        target_speed=1.0,
                        wait_time=300,
                        log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl),
                        check_pause_callback=self.check_pause
                    )

            # ── BƯỚC 4: VÁ LỖI QA & KIỂM TRA LẠI (QA REPAIR & RE-CHECK) ──
            self.emit_progress(70, "Đang chạy vòng lặp kiểm tra & vá lỗi QA...")
            report_dir = os.path.join(root_downloads, "report")
            os.makedirs(report_dir, exist_ok=True)

            prompt_repair = "./user_data/prompts/promptRepair.md"
            if not os.path.exists(prompt_repair):
                prompt_repair = "./prompts/promptRepair.md"

            MAX_PASSES = 5
            pass_num = 1

            while pass_num <= MAX_PASSES:
                self.check_pause()
                self.emit_log(f"\n🔄 ===== BẮT ĐẦU VÒNG VÁ LỖI QA THỨ {pass_num}/{MAX_PASSES} =====", "info")
                
                if run_auto_qa_repair:
                    run_auto_qa_repair(
                        prompt_file=prompt_repair,
                        report_folder=report_dir,
                        original_srt_folder=vi_folder,
                        fixed_srt_folder=vi_folder,
                        profile_folder="chrome_data_1",
                        wait_time=300,
                        log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl),
                    )

                if process_and_renumber_srt:
                    process_and_renumber_srt(vi_folder, vi_folder, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))

                recheck_out = os.path.join(report_dir, f"Report_ReCheck_Pass_{pass_num}.txt")
                total_err, total_crit, total_warn = 0, 0, 0

                if analyze_srt_to_file:
                    res = analyze_srt_to_file(
                        in_path=vi_folder,
                        out_path=recheck_out,
                        errors_per_file=80,
                        log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl),
                        scan_mode='all'
                    )
                    if res:
                        total_err, total_crit, total_warn = res

                if total_crit == 0:
                    self.emit_log(f"🎉 THÀNH CÔNG RỰC RỠ! Không còn lỗi CRITICAL nào sau {pass_num} vòng vá!", "success")
                    break
                else:
                    if pass_num < MAX_PASSES:
                        self.emit_log(f"⚠️ Vẫn còn {total_crit} lỗi CRITICAL. Tiếp tục vòng vá {pass_num + 1}/{MAX_PASSES}...", "warning")
                        pass_num += 1
                    else:
                        self.emit_log(f"🛑 Đã đạt {MAX_PASSES} vòng vá tối đa. Còn {total_crit} lỗi CRITICAL.", "warning")
                        break

            # ── BƯỚC 5: GỘP FILE THÀNH PHẨM (MERGE FINAL SRT) ──
            self.emit_progress(95, "Đang gộp toàn bộ phân đoạn thành 1 file phụ đề duy nhất...")
            final_srt_path = os.path.join(root_downloads, f"{raw_title}_vi.srt")

            if merge_numbered_srt_files:
                merge_numbered_srt_files(vi_folder, final_srt_path, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))

            self.emit_progress(100, "HOÀN TẤT TOÀN BỘ DỰ ÁN! 🎉")
            self.finished_signal.emit(True, f"🎉 ĐÃ HOÀN TẤT TOÀN BỘ DỰ ÁN TỰ ĐỘNG!\n📁 File phụ đề thành phẩm lưu tại:\n{final_srt_path}")

        except Exception as e:
            self.emit_log(f"❌ Lỗi tiến trình tự động: {e}", "error")
            self.finished_signal.emit(False, str(e))

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
        
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_timer_display)

        self.init_ui()
        self.setup_shortcuts()
        self.load_user_config()

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
        self.txt_link.setToolTip("Nhập hoặc dán link Bilibili (Nhấn nút BẮT ĐẦU DỰ ÁN TỰ ĐỘNG để chạy)")

        self.btn_paste_link = QToolButton()
        self.btn_paste_link.setText("📋 Dán Link")
        self.btn_paste_link.setObjectName("ToolBtnAccent")
        self.btn_paste_link.setToolTip("Dán nhanh đường dẫn video Bilibili từ Clipboard")
        self.btn_paste_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste_link.clicked.connect(self.paste_link_only)

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

        # Dòng Tùy Chọn Thư Mục Lưu
        opt_layout = QHBoxLayout()
        opt_layout.setSpacing(12)

        lbl_dir = QLabel("📁 Thư mục lưu:")
        lbl_dir.setStyleSheet("color: #cbd5e1; font-weight: 600; font-size: 12px;")

        self.txt_output_dir = QLineEdit("./downloads")
        self.txt_output_dir.setObjectName("DirInput")
        self.txt_output_dir.setToolTip("Thư mục lưu trữ dự án video & phụ đề thành phẩm")
        self.txt_output_dir.editingFinished.connect(self.save_user_config)

        self.btn_browse_dir = QToolButton()
        self.btn_browse_dir.setText("📂 Chọn...")
        self.btn_browse_dir.setObjectName("ToolBtn")
        self.btn_browse_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_dir.clicked.connect(self.browse_output_directory)

        opt_layout.addWidget(lbl_dir)
        opt_layout.addWidget(self.txt_output_dir, stretch=1)
        opt_layout.addWidget(self.btn_browse_dir)
        input_layout.addLayout(opt_layout)

        # Dòng Button duy nhất điều khiển dự án tự động
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self.btn_run = QPushButton("🚀 BẮT ĐẦU DỰ ÁN TỰ ĐỘNG")
        self.btn_run.setObjectName("BtnRun")
        self.btn_run.setToolTip("Khởi động toàn bộ dự án: Tải Video -> Tạo SRT -> Đổi Tốc Độ 0.8 -> Chia Block -> Dịch Tiếng Việt -> Vá Lỗi QA -> Gộp Thành Phẩm (Ctrl+Enter)")
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.clicked.connect(self.on_run_clicked)

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

        btn_layout.addWidget(self.btn_run, stretch=2)
        btn_layout.addWidget(self.btn_pause, stretch=1)
        btn_layout.addWidget(self.btn_stop, stretch=1)

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
        # Tự động tạo cấu trúc thư mục cá nhân người dùng
        os.makedirs("./user_data/cookies", exist_ok=True)
        os.makedirs("./user_data/chrome_profiles", exist_ok=True)
        os.makedirs("./user_data/config", exist_ok=True)
        os.makedirs("./user_data/prompts", exist_ok=True)

        possible_files = [
            "./user_data/cookies/cookies.txt",
            "./user_data/cookies.txt",
            "./cookie.txt", 
            "./cookies.txt", 
            "./downloads/cookie.txt", 
            "./downloads/cookies.txt"
        ]
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
                            
                            # Lưu vào thư mục cá nhân người dùng user_data/cookies/
                            with open("./user_data/cookies/cookies.txt", "w", encoding="utf-8") as out:
                                out.write(netscape_content)
                            with open("./cookies.txt", "w", encoding="utf-8") as out:
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

    def load_user_config(self):
        """Đọc cấu hình từ user_config.json để tự động hiển thị link và thư mục lưu gần nhất"""
        config_path = "./user_data/config/user_config.json"
        if os.path.exists(config_path):
            try:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                last_url = cfg.get("last_url", "")
                last_dir = cfg.get("last_dir", "./downloads")
                if last_url:
                    self.txt_link.setText(last_url)
                if last_dir:
                    self.txt_output_dir.setText(last_dir)
                self.append_log(f"⚙️ Đã nạp cấu hình đã lưu: {last_dir}", "info")
            except Exception as e:
                pass

    def save_user_config(self):
        """Lưu đường dẫn link và thư mục dự án vào user_config.json"""
        os.makedirs("./user_data/config", exist_ok=True)
        config_path = "./user_data/config/user_config.json"
        try:
            import json
            current_url = self.txt_link.text().strip()
            current_dir = self.txt_output_dir.text().strip() or "./downloads"

            cfg = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}

            history_urls = cfg.get("history_urls", [])
            history_dirs = cfg.get("history_dirs", [])

            if current_url and current_url not in history_urls:
                history_urls.insert(0, current_url)
            if current_dir and current_dir not in history_dirs:
                history_dirs.insert(0, current_dir)

            cfg["last_url"] = current_url
            cfg["last_dir"] = current_dir
            cfg["history_urls"] = history_urls[:20]
            cfg["history_dirs"] = history_dirs[:20]

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def browse_output_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video tải về")
        if dir_path:
            self.txt_output_dir.setText(dir_path)
            self.append_log(f"📁 Đã đổi thư mục lưu: {dir_path}", "info")
            self.save_user_config()

    def paste_link_only(self):
        """Chỉ dán link vào ô nhập liệu mà KHÔNG tự động chạy tiến trình"""
        clipboard = QApplication.clipboard()
        raw_text = clipboard.text().strip()
        if not raw_text:
            return

        clean_url = BilibiliDownloader.extract_bilibili_url(raw_text) if BilibiliDownloader else None
        target_url = clean_url if clean_url else raw_text
        self.txt_link.setText(target_url)
        self.append_log(f"📋 Đã dán link Bilibili: {target_url}", "info")
        self.save_user_config()

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

        # Lưu cấu hình sử dụng gần nhất
        self.save_user_config()

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
        self.worker = ProcessWorker(target_url, output_dir=out_dir)
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

        self.worker = ProcessWorker(link="", output_dir=out_dir, local_media_path=file_path)
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

    def on_qa_scan_clicked(self):
        """Xử lý sự kiện bấm nút QUÉT LỖI QA: Hỗ trợ chọn 1 Tệp lẻ hoặc Cả Thư Mục"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Lựa Chọn Phạm Vi Quét QA")
        msg_box.setText("Bạn muốn quét phân tích lỗi QA cho 1 Tệp đơn lẻ hay Cả Thư Mục?")
        btn_file = msg_box.addButton("📄 Chọn 1 Tệp SRT", QMessageBox.ButtonRole.ActionRole)
        btn_folder = msg_box.addButton("📁 Chọn Cả Thư Mục SRT", QMessageBox.ButtonRole.ActionRole)
        btn_cancel = msg_box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        target_path = None
        if msg_box.clickedButton() == btn_file:
            target_path, _ = QFileDialog.getOpenFileName(
                self,
                "Chọn Tệp SRT Tiếng Việt để Quét Lỗi QA",
                self.txt_output_dir.text().strip() or "./downloads",
                "Subtitle Files (*.srt);;All Files (*)"
            )
        elif msg_box.clickedButton() == btn_folder:
            target_path = QFileDialog.getExistingDirectory(
                self,
                "Chọn Thư Mục Chứa Các Tệp SRT Tiếng Việt để Quét Hàng Loạt",
                self.txt_output_dir.text().strip() or "./downloads"
            )

        if not target_path:
            return

        out_dir = self.txt_output_dir.text().strip() or "./downloads"
        self.progress_bar.setValue(0)

        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

        self.update_kpi_value("kpi_percent", "0%", "#10b981")
        self.update_kpi_value("kpi_status", "Quét QA 🔍", "#6366f1")
        self.update_kpi_value("kpi_elapsed", "00:00:00", "#38bdf8")
        
        self.lbl_system_badge.setText("🔍 QUÉT QA")
        self.lbl_system_badge.setStyleSheet("background-color: #0284c7; color: white;")

        self.start_timestamp = time.time()
        self.timer.start()

        self.worker = ProcessWorker(link="", output_dir=out_dir, qa_scan_path=target_path)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_process_finished)
        self.worker.start()

    def on_qa_repair_clicked(self):
        """Xử lý sự kiện bấm nút VÁ LỖI QA (AI)"""
        out_dir = self.txt_output_dir.text().strip() or "./downloads"
        self.progress_bar.setValue(0)

        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

        self.update_kpi_value("kpi_percent", "0%", "#10b981")
        self.update_kpi_value("kpi_status", "Vá QA 🩺", "#6366f1")
        self.update_kpi_value("kpi_elapsed", "00:00:00", "#38bdf8")
        
        self.lbl_system_badge.setText("🩺 VÁ LỖI QA")
        self.lbl_system_badge.setStyleSheet("background-color: #9333ea; color: white;")

        self.start_timestamp = time.time()
        self.timer.start()

        self.worker = ProcessWorker(link="", output_dir=out_dir, qa_repair_mode=True)
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
