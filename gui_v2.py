import sys
import os
import time
import shutil
import requests
from datetime import datetime

# Cấu hình đường dẫn ffmpeg từ imageio_ffmpeg vào PATH trước khi import pydub
try:
    import imageio_ffmpeg
    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if _ffmpeg_exe and os.path.exists(_ffmpeg_exe):
        _ffmpeg_dir = os.path.dirname(_ffmpeg_exe)
        if _ffmpeg_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QTextEdit,
    QFrame, QGroupBox, QSplitter, QMessageBox, QToolButton,
    QComboBox, QCheckBox, QFileDialog, QTabWidget, QScrollArea,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMutex, QWaitCondition, QTimer, QUrl, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QTextCursor, QKeySequence, QShortcut, QDesktopServices

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

try:
    from gemini_core import get_available_profiles, create_new_profile, open_chrome_for_login, is_profile_logged_in
except ImportError:
    get_available_profiles = None
    create_new_profile = None
    open_chrome_for_login = None
    is_profile_logged_in = None

try:
    from backend import CapCutBackend
except ImportError:
    CapCutBackend = None


# ==============================================================================
# WORKER THREAD ĐĂNG NHẬP CHROME
# ==============================================================================
class ChromeLoginWorker(QThread):
    log_signal = pyqtSignal(str, str)         # (log_msg, log_level)
    finished_signal = pyqtSignal(bool, str)   # (success, final_message)

    def __init__(self, profile_folder: str):
        super().__init__()
        self.profile_folder = profile_folder

    def run(self):
        def log_cb(msg, level="info"):
            self.log_signal.emit(msg, level)

        try:
            if open_chrome_for_login:
                success = open_chrome_for_login(self.profile_folder, log_callback=log_cb)
                self.finished_signal.emit(success, f"Hoàn tất lưu Profile [{self.profile_folder}]")
            else:
                self.log_signal.emit("❌ Không tìm thấy hàm open_chrome_for_login trong gemini_core!", "error")
                self.finished_signal.emit(False, "Thiếu modul open_chrome_for_login")
        except Exception as e:
            self.log_signal.emit(f"❌ Lỗi khi mở Chrome: {e}", "error")
            self.finished_signal.emit(False, str(e))


class BilibiliLoginWorker(QThread):
    log_signal = pyqtSignal(str, str)
    finished_signal = pyqtSignal(bool, str)

    def run(self):
        def log_cb(msg, level="info"):
            self.log_signal.emit(msg, level)

        try:
            from bilibili_downloader import login_bilibili_and_save_cookies
            success, msg = login_bilibili_and_save_cookies(log_callback=log_cb)
            self.finished_signal.emit(success, msg)
        except Exception as e:
            self.log_signal.emit(f"❌ Lỗi đăng nhập Bilibili: {e}", "error")
            self.finished_signal.emit(False, str(e))


# ==============================================================================
# WORKER THREAD ĐỒNG BỘ: QUY TRÌNH TỰ ĐỘNG END-TO-END (6 BƯỚC)
# ==============================================================================
class ProcessWorker(QThread):
    log_signal = pyqtSignal(str, str)         # (log_msg, log_level)
    progress_signal = pyqtSignal(int, str)
    global_progress_signal = pyqtSignal(int, str)    # (percentage, status_text)
    step_signal = pyqtSignal(int)             # Active step index (1-6)
    finished_signal = pyqtSignal(bool, str)   # (success, final_message)
    request_gradio_link_signal = pyqtSignal() # Yêu cầu người dùng nhập link Gradio khi tới Bước 5

    def __init__(self, link: str, output_dir: str = "./downloads", auto_gen_srt: bool = False, auto_translate_srt: bool = False, local_media_path: str = None, srt_translate_path: str = None, qa_scan_path: str = None, qa_repair_mode: bool = False, profile_folder: str = "chrome_data_1", auto_inject_capcut: bool = False, capcut_draft_path: str = "", enable_tts: bool = True, gradio_url: str = "", ref_audio_path: str = "", ref_text: str = "", fast_forward_mode: bool = False):
        super().__init__()
        self.fast_forward_mode = fast_forward_mode
        self.link = link
        self.output_dir = output_dir
        self.auto_gen_srt = auto_gen_srt
        self.auto_translate_srt = auto_translate_srt
        self.local_media_path = local_media_path
        self.srt_translate_path = srt_translate_path
        self.qa_scan_path = qa_scan_path
        self.qa_repair_mode = qa_repair_mode
        self.profile_folder = profile_folder
        self.auto_inject_capcut = auto_inject_capcut
        self.capcut_draft_path = capcut_draft_path
        self.enable_tts = enable_tts
        self.gradio_url = gradio_url.strip() if gradio_url else ""
        self.ref_audio_path = ref_audio_path
        self.ref_text = ref_text
        self._is_paused = False
        self._is_stopped = False
        self.downloader = None
        self.srt_generator = None
        self.mutex = QMutex()
        self.pause_condition = QWaitCondition()

    def update_gradio_url(self, new_url: str):
        self.mutex.lock()
        self.gradio_url = new_url.strip()
        self.mutex.unlock()
        self.log_signal.emit(f"🔄 Đã cập nhật link Gradio URL mới: {self.gradio_url}", "info")

    def check_gradio_connection(self, url: str) -> bool:
        if not url or not url.strip():
            return False
        clean = url.strip()
        if not clean.startswith("http://") and not clean.startswith("https://"):
            return False
        try:
            res = requests.get(clean, timeout=4)
            return res.status_code in [200, 301, 302]
        except Exception:
            return False

    def pause(self):
        self.mutex.lock()
        self._is_paused = True
        self.mutex.unlock()
        self.log_signal.emit("⏸️ Đã nhận lệnh TẠM DỪNG tiến trình.", "warning")
        self.progress_signal.emit(-1, "Đã tạm dừng ⏸️")

    def resume(self):
        self.mutex.lock()
        self._is_paused = False
        self.pause_condition.wakeAll()
        self.mutex.unlock()
        self.log_signal.emit("▶️ Đã nhận lệnh TIẾP TỤC tiến trình.", "info")

    def stop(self):
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

            final_srt_path = os.path.join(root_downloads, "output.srt")
            # Dọn dẹp file output.srt cũ nếu có để tránh người dùng nhầm lẫn với dự án trước
            if not self.fast_forward_mode and os.path.exists(final_srt_path):
                try:
                    os.remove(final_srt_path)
                except Exception:
                    pass

            # ── BƯỚC 1: DOWNLOAD / NẠP NGUỒN MEDIA ──
            self.step_signal.emit(1)
            self.global_progress_signal.emit(5, "⚡ 1. Khởi động Tải Video / Nạp Nguồn...")
            self.emit_log("==================================================", "info")
            self.emit_log("🚀 BẮT ĐẦU QUY TRÌNH XỬ LÝ DỰ ÁN TỰ ĐỘNG (END-TO-END)", "info")
            self.emit_log("==================================================", "info")

            video_file = None
            if self.local_media_path and os.path.exists(self.local_media_path):
                video_file = self.local_media_path
            else:
                for f in os.listdir(root_downloads):
                    if f.lower().endswith(('.mp4', '.mkv', '.flv', '.webm')) and not f.endswith('.part'):
                        video_file = os.path.join(root_downloads, f)
                        break

            if not video_file and self.link and BilibiliDownloader and BilibiliDownloader.is_valid_bilibili_url(self.link):
                self.emit_log(f"📥 Khởi động tải Video từ Bilibili: {self.link}...", "info")
                self.global_progress_signal.emit(10, "Đang tải Video Bilibili...")
                self.downloader = BilibiliDownloader(
                    output_dir=root_downloads,
                    log_callback=self.emit_log,
                    progress_callback=self.emit_progress,
                    check_pause_callback=self.check_pause
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
            if self.fast_forward_mode:
                self.emit_log("==================================================", "info")
                self.emit_log("⏩ CHẾ ĐỘ [FAST-FORWARD] KÍCH HOẠT: Bỏ qua tạo phụ đề, dịch thuật, và QA!", "success")
                self.emit_log("==================================================", "info")
                self.step_signal.emit(4)
            else:
    
                # ── BƯỚC 2: SPEECH-TO-TEXT (WHISPER STT) ──
                self.step_signal.emit(2)
                has_cn_splits = os.path.exists(cn_folder) and any(f.endswith('.srt') for f in os.listdir(cn_folder))
    
                if not has_cn_splits:
                    self.emit_log(f"🎙️ 2. Trích xuất phụ đề tự động bằng Whisper...", "info")
                    raw_srt = None
                    if self.srt_translate_path and os.path.exists(self.srt_translate_path):
                        raw_srt = self.srt_translate_path
                    else:
                        for f in os.listdir(root_downloads):
                            if f.lower().endswith('.srt') and f.lower() != 'output.srt' and not f.endswith('_vi.srt') and not f.endswith('_08.srt') and not f.endswith('_speed08.srt'):
                                raw_srt = os.path.join(root_downloads, f)
                                break
    
                    if not raw_srt and video_file and SubtitleGenerator:
                        self.global_progress_signal.emit(25, "Đang nhận diện giọng nói tạo phụ đề SRT...")
                        self.srt_generator = SubtitleGenerator(
                            output_dir=root_downloads,
                            log_callback=self.emit_log,
                            progress_callback=self.emit_progress,
                            check_pause_callback=self.check_pause
                        )
                        srt_res = self.srt_generator.generate_srt(video_file, model_size="base")
                        if srt_res.get("success"):
                            raw_srt = srt_res.get("srt_path")
    
                    if not raw_srt:
                        self.finished_signal.emit(False, "❌ Không thể tạo hoặc tìm thấy file phụ đề SRT gốc!")
                        return
    
                    srt_08 = os.path.join(root_downloads, f"{raw_title}_speed08.srt")
                    if not os.path.exists(srt_08):
                        self.emit_log("⚡ Đang tự động chuyển đổi tốc độ phụ đề SRT gốc từ 1.0x sang 0.8x...", "info")
                        if process_srt_speed:
                            process_srt_speed(raw_srt, srt_08, 1.0, 0.8, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))
                        else:
                            srt_08 = raw_srt
    
                    self.emit_log(f"📁 Chia nhỏ tệp SRT thành block 100 câu lưu vào: {cn_folder}...", "info")
                    os.makedirs(cn_folder, exist_ok=True)
                    if split_srt_file:
                        prefix_path = os.path.join(cn_folder, "part")
                        split_srt_file(srt_08, output_prefix=prefix_path, blocks_per_file=100, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))
                else:
                    self.emit_log(f"✅ Đã tìm thấy thư mục phụ đề gốc: {cn_folder}", "info")
    
                # ── BƯỚC 3: DỊCH THUẬT AI & SO KHỚP TIMECODE 100% ──
                self.emit_progress(0, "Chuẩn bị dịch thuật AI...")
                self.step_signal.emit(3)
                self.global_progress_signal.emit(45, "3. Đang chạy Gemini AI dịch Tiếng Việt & kiểm tra Timecode...")
                os.makedirs(vi_folder, exist_ok=True)
    
                if run_auto_translate_srt:
                    prompt_file = os.path.abspath("./user_data/prompts/promptTranslates.md")
                    if not os.path.exists(prompt_file):
                        prompt_file = os.path.abspath("./user_data/prompts/translate.txt")
                    if not os.path.exists(prompt_file):
                        os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
                        with open(prompt_file, "w", encoding="utf-8") as pf:
                            pf.write("Hãy dịch chính xác file SRT này sang Tiếng Việt. Giữ nguyên định dạng mốc thời gian.")

                    MAX_GLOBAL_RETRIES = 3
                    global_retry_count = 0
                    
                    while global_retry_count < MAX_GLOBAL_RETRIES:
                        cn_files = [f for f in os.listdir(cn_folder) if f.endswith('.srt')]
                        vi_files = [f for f in os.listdir(vi_folder) if f.endswith('.srt')]
                        
                        if len(cn_files) > 0 and len(cn_files) <= len(vi_files):
                            if global_retry_count == 0:
                                self.emit_log("⏩ Bỏ qua bước dịch thuật vì đã có đủ file phụ đề Tiếng Việt trong thư mục đích.", "info")
                            break
                            
                        if global_retry_count > 0:
                            missing_files = []
                            for cn_file in cn_files:
                                expected_vi = cn_file.replace('_cn.srt', '_vi.srt')
                                if expected_vi not in vi_files:
                                    missing_files.append(expected_vi)
                            self.emit_log(f"⚠️ Phát hiện thiếu {len(missing_files)} file: {', '.join(missing_files)}", "warning")
                            self.emit_log(f"🔄 ĐANG TIẾN HÀNH QUÉT LẠI ĐỂ DỊCH BÙ (Lần quét thứ {global_retry_count}/{MAX_GLOBAL_RETRIES-1})...", "info")
                        
                        run_auto_translate_srt(
                            prompt_file=prompt_file,
                            cn_folder=cn_folder,
                            vi_folder=vi_folder,
                            profile_folder=self.profile_folder,
                            wait_time=300,
                            delay_time=15,
                            log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl),
                            progress_callback=self.emit_progress,
                            check_pause_callback=self.check_pause
                        )
                        global_retry_count += 1

                # CỔNG 1: Kiểm tra chốt chặn đồng bộ
                cn_parts = [f for f in os.listdir(cn_folder) if f.endswith('.srt')]
                vi_parts = [f for f in os.listdir(vi_folder) if f.endswith('.srt')]
                
                if not vi_parts or len(vi_parts) < len(cn_parts):
                    self.emit_log("❌ LỖI NGHIÊM TRỌNG: Đã quét dịch bù nhiều lần nhưng hệ thống AI vẫn bỏ sót file.", "error")
                    self.finished_signal.emit(False, "❌ DỪNG TIẾN TRÌNH: Dịch thiếu file. Vui lòng kiểm tra lại log hoặc kết nối mạng!")
                    return
    
                self.emit_log(f"✅ CỔNG 1 ĐẠT CHUẨN: Đã đồng bộ đầy đủ {len(vi_parts)}/{len(cn_parts)} file phân đoạn tiếng Việt!", "success")
                self.emit_log("💡 (Không tạo output.srt trung gian ở bước này để tránh nhầm lẫn với bản dịch chưa qua QA)", "info")
    
                # ── BƯỚC 4: AUTO QA TRỰC TIẾP TRÊN TỪNG PART & SỬA TRONG THƯ MỤC FIXED ──
                self.emit_progress(0, "Chuẩn bị Auto QA...")
                self.step_signal.emit(4)
                self.global_progress_signal.emit(65, "4. Đang kiểm tra QA & tự động sửa lỗi trực tiếp trên từng part...")
                report_folder = os.path.join(root_downloads, f"temp_split_qa_reports_{raw_title}")
                fixed_vi_folder = os.path.join(root_downloads, f"temp_split_vi_fixed_{raw_title}")
                os.makedirs(report_folder, exist_ok=True)
                os.makedirs(fixed_vi_folder, exist_ok=True)
    
                # Khởi tạo bản sao các part sang fixed_vi_folder
                for fn in os.listdir(vi_folder):
                    if fn.endswith('.srt'):
                        src_p = os.path.join(vi_folder, fn)
                        dst_p = os.path.join(fixed_vi_folder, fn)
                        if not os.path.exists(dst_p):
                            shutil.copy2(src_p, dst_p)
    
                # Quét phân tích lỗi QA lần 1
                if analyze_srt_to_file:
                    analyze_srt_to_file(vi_folder, report_folder, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), check_pause_callback=self.check_pause)
    
                # Kiểm tra xem có file báo cáo QA nào cần sửa không
                report_files = [f for f in os.listdir(report_folder) if f.endswith('.txt') and not f.endswith('_da_sua.txt') and not f.endswith('.done') and ('report' in f.lower() or 'qa' in f.lower())]
    
                if report_files and run_auto_qa_repair:
                    self.emit_log(f"🧹 Tìm thấy {len(report_files)} báo cáo lỗi QA. Tiến hành vá lỗi & gộp câu tự động...", "info")
                    qa_prompt = os.path.abspath("./user_data/prompts/promptRepair.md")
                    if not os.path.exists(qa_prompt):
                        qa_prompt = os.path.abspath("./user_data/prompts/prompt_qa_repair.txt")
                    if not os.path.exists(qa_prompt):
                        os.makedirs(os.path.dirname(qa_prompt), exist_ok=True)
                        with open(qa_prompt, "w", encoding="utf-8") as qf:
                            qf.write("Hãy kiểm tra và sửa lỗi các câu phụ đề vượt quá độ dài hoặc đè timecode.")
    
                    run_auto_qa_repair(
                        prompt_file=qa_prompt,
                        report_folder=report_folder,
                        original_srt_folder=vi_folder,
                        fixed_srt_folder=fixed_vi_folder,
                        profile_folder=self.profile_folder,
                        log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl),
                        progress_callback=self.emit_progress,
                        check_pause_callback=self.check_pause
                    )
    
                    # Quét lại (Re-scan) trên thư mục fixed_vi_folder để đảm bảo không còn lỗi nghiêm trọng
                    if analyze_srt_to_file:
                        rescan_report_folder = os.path.join(root_downloads, f"temp_split_qa_rescan_{raw_title}")
                        os.makedirs(rescan_report_folder, exist_ok=True)
                        re_err, re_crit, re_warn = analyze_srt_to_file(fixed_vi_folder, rescan_report_folder, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), check_pause_callback=self.check_pause)
                        if re_crit == 0:
                            self.emit_log(f"✅ RE-SCAN HOÀN HẢO: 0 Lỗi nghiêm trọng (Critical) sau khi sửa! (Còn {re_warn} cảnh báo nhỏ)", "success")
                        else:
                            self.emit_log(f"⚠️ RE-SCAN: Còn lại {re_crit} lỗi nghiêm trọng và {re_warn} cảnh báo.", "warning")
                else:
                    self.emit_log("✅ Phụ đề tiếng Việt đạt chuẩn chất lượng 100%, không phát hiện lỗi QA cần sửa!", "success")
    
                # ── CỔNG 3: XUẤT DUY NHẤT 1 FILE output.srt THÀNH PHẨM HOÀN HẢO ──
                if merge_numbered_srt_files and os.path.exists(fixed_vi_folder) and os.listdir(fixed_vi_folder):
                    self.emit_log("🧩 CỔNG 3: Hoàn tất kiểm duyệt QA! Tiến hành xuất DUY NHẤT file thành phẩm: output.srt...", "info")
                    merge_numbered_srt_files(fixed_vi_folder, final_srt_path, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))
                    self.emit_log(f"🎉 ĐÃ XUẤT FILE THÀNH PHẨM DUY NHẤT: {final_srt_path} (Sẵn sàng 100% để sinh Audio TTS / Nhúng CapCut)", "success")
                else:
                    self.finished_signal.emit(False, "❌ Thất bại khi tạo file output.srt cuối cùng!")
                    return

            # ── BƯỚC 5: SINH AUDIO (TTS GRADIO) VỚI ĐIỂM DỪNG THÔNG MINH (JUST-IN-TIME) ──
            self.step_signal.emit(5)
            if self.enable_tts:
                self.global_progress_signal.emit(80, "5. Kiểm tra kết nối Gradio TTS Server...")
                while not self.check_gradio_connection(self.gradio_url):
                    self.emit_log("⏸️ [ĐIỂM DỪNG THÔNG MINH] File output.srt đã hoàn thiện 100%!", "warning")
                    self.emit_log("👉 Hãy mở Google Colab lấy link Gradio mới (https://xxxx.gradio.live), dán vào ô 'Gradio URL' rồi bấm '▶️ TIẾP TỤC'!", "info")
                    self.request_gradio_link_signal.emit()
                    self.pause()
                    self.check_pause()

                self.global_progress_signal.emit(85, "5. Đang sinh Audio bằng Gradio TTS Server...")
                self.emit_log(f"🎙️ Kết nối Gradio TTS Server thành công ({self.gradio_url})! Đang sinh Audio từ file chuẩn output.srt...", "success")

            # ── BƯỚC 6: CAPCUT DRAFT INJECT ──
            self.step_signal.emit(6)
            if self.auto_inject_capcut and self.capcut_draft_path and os.path.exists(self.capcut_draft_path):
                self.global_progress_signal.emit(92, "6. Đang bơm phụ đề trực tiếp vào dự án CapCut PC...")
                self.emit_log(f"💉 Bơm phụ đề vào CapCut Draft: {self.capcut_draft_path}...", "info")
                
                if self.capcut_draft_path.lower().endswith(".json"):
                    draft_json = self.capcut_draft_path
                else:
                    draft_json = os.path.join(self.capcut_draft_path, "draft_content.json")
                if os.path.exists(draft_json) and CapCutBackend:
                    try:
                        cfg = {
                            "SRT_FILE_PATH": final_srt_path,
                            "CAPCUT_JSON_PATH": draft_json,
                            "AUDIO_OUT_DIR": os.path.join(root_downloads, f"voice_{raw_title}"),
                            "SERVER_URL": self.gradio_url,
                            "REF_AUDIO_PATH": self.ref_audio_path,
                            "REF_TEXT": self.ref_text,
                            "SPEED_RATIO": 1.25
                        }
                        backend = CapCutBackend(cfg, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), progress_callback=lambda d, t, msg: self.emit_progress(int((d/t)*100) if t > 0 else 0, msg))
                        backend.ensure_capcut_closed()
                        backend.run_process(only_inject=not self.enable_tts)
                        self.emit_log("🎉 ĐÃ NHÚNG PHỤ ĐỀ TRỰC TIẾP VÀO CAPCUT DRAFT THÀNH CÔNG!", "success")
                    except Exception as inject_e:
                        self.emit_log(f"⚠️ Thất bại khi nhúng vào CapCut: {inject_e}", "warning")

            self.global_progress_signal.emit(100, "HOÀN TẤT DỰ ÁN! 🎉")
            self.finished_signal.emit(True, f"🎉 ĐÃ HOÀN TẤT TOÀN BỘ DỰ ÁN TỰ ĐỘNG!\n📁 Phụ đề lưu tại: {final_srt_path}")

        except Exception as e:
            self.emit_log(f"❌ Lỗi tiến trình tự động: {e}", "error")
            self.finished_signal.emit(False, str(e))


def get_default_capcut_path() -> str:
    """Tự động định vị đường dẫn thư mục lưu dự án của phần mềm CapCut PC trên Windows."""
    appdata = os.getenv('LOCALAPPDATA', '')
    if appdata:
        p = os.path.join(appdata, 'CapCut', 'User Data', 'Projects', 'com.lveditor.draft')
        if os.path.exists(p):
            return p
    return ""


# ==============================================================================
# WIDGET THANH TIẾN TRÌNH 6 BƯỚC (VISUAL PIPELINE STEPPER V3)
# ==============================================================================
class PipelineStepperWidget(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PipelineStepper")
        self.current_step = 1
        self.steps = [
            ("1. Download", "📥"),
            ("2. Whisper STT", "🎙️"),
            ("3. AI Translate", "🌐"),
            ("4. Auto QA", "🧹"),
            ("5. Sinh Audio", "🔊"),
            ("6. CapCut Inject", "💉")
        ]
        self.step_labels = []
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        for i, (title, icon) in enumerate(self.steps, 1):
            lbl = QLabel(f"{icon} {title}")
            lbl.setObjectName(f"StepBadge_{i}")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFixedHeight(32)
            layout.addWidget(lbl, stretch=1)
            self.step_labels.append(lbl)

            if i < len(self.steps):
                arrow = QLabel("➔")
                arrow.setStyleSheet("color: #475569; font-weight: bold; font-size: 10pt;")
                layout.addWidget(arrow)

        self.set_step(1)

    def set_step(self, active_step: int, status: str = "running"):
        self.current_step = active_step
        for i, lbl in enumerate(self.step_labels, 1):
            if i < active_step:
                lbl.setStyleSheet("""
                    background-color: #064e3b; color: #34d399; font-weight: bold; 
                    font-size: 9.5pt; border-radius: 6px; border: 1px solid #059669;
                """)
                lbl.setText(f"✅ {self.steps[i-1][0]}")
            elif i == active_step:
                if status == "running":
                    lbl.setStyleSheet("""
                        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #2563eb); 
                        color: white; font-weight: bold; font-size: 9.5pt; border-radius: 6px; 
                        border: 1px solid #38bdf8;
                    """)
                    lbl.setText(f"⏳ {self.steps[i-1][0]}")
                elif status == "error":
                    lbl.setStyleSheet("""
                        background-color: #7f1d1d; color: #fca5a5; font-weight: bold; 
                        font-size: 9.5pt; border-radius: 6px; border: 1px solid #ef4444;
                    """)
                    lbl.setText(f"⚠️ {self.steps[i-1][0]}")
            else:
                lbl.setStyleSheet("""
                    background-color: #1e293b; color: #64748b; font-weight: 500; 
                    font-size: 9.5pt; border-radius: 6px; border: 1px solid #334155;
                """)
                lbl.setText(f"{self.steps[i-1][1]} {self.steps[i-1][0]}")


# ==============================================================================
# GIAO DIỆN CHÍNH (CAPCUTINJECTOR PRO STUDIO V7 UNIFIED CONSOLE SYSTEM)
# ==============================================================================
class MainWindowV2(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.login_worker = None
        self.is_paused = False
        self.start_timestamp = None
        self.logs_history = []
        self.is_voice_expanded = False
        
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_timer_display)

        self.init_ui()
        self.setup_shortcuts()
        self.load_user_config()

    def init_ui(self):
        self.setWindowTitle("CapcutInjector Pro Studio v3 - Hệ Thống Tự Động Hóa 1-Click 🚀")
        self.resize(1260, 840)
        self.setMinimumSize(1000, 660)

        self.setup_stylesheet()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)

        # ----------------------------------------------------------------------
        # 1. HEADER BANNER TOP (TÍCH HỢP CHROME PROFILE VÀO HEADER TOP)
        # ----------------------------------------------------------------------
        header_frame = QFrame()
        header_frame.setObjectName("HeaderFrame")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(16, 6, 16, 6)

        header_text_layout = QVBoxLayout()
        header_text_layout.setSpacing(0)

        title_label = QLabel("⚡ CAPCUT INJECTOR PRO STUDIO v3.0")
        title_label.setObjectName("MainTitle")

        header_text_layout.addWidget(title_label)
        header_layout.addLayout(header_text_layout)
        header_layout.addStretch(1)

        # CỤM KHU VỰC CHỌN CHROME PROFILE TRÊN HEADER TOP
        profile_top_layout = QHBoxLayout()
        profile_top_layout.setSpacing(6)

        lbl_profile = QLabel("🌐 Profile Chrome:")
        lbl_profile.setStyleSheet("color: #e2e8f0; font-weight: 600; font-size: 9.5pt;")

        self.cbo_profile = QComboBox()
        self.cbo_profile.setObjectName("ProfileCombo")
        self.cbo_profile.setMinimumWidth(140)
        self.cbo_profile.setFixedHeight(28)
        self.cbo_profile.currentIndexChanged.connect(self.save_user_config)
        self.cbo_profile.currentIndexChanged.connect(self.update_profile_login_status_ui)

        self.btn_new_profile = QToolButton()
        self.btn_new_profile.setText("➕")
        self.btn_new_profile.setFixedSize(28, 28)
        self.btn_new_profile.setObjectName("ToolBtn")
        self.btn_new_profile.setStyleSheet("padding: 0px; margin: 0px; min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px; border-radius: 5px; text-align: center; font-size: 10pt;")
        self.btn_new_profile.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_profile.setToolTip("Tạo Profile Chrome Mới")
        self.btn_new_profile.clicked.connect(self.on_create_profile_clicked)

        self.btn_login_chrome = QToolButton()
        self.btn_login_chrome.setText("🔑 Login")
        self.btn_login_chrome.setMinimumWidth(85)
        self.btn_login_chrome.setFixedHeight(28)
        self.btn_login_chrome.setObjectName("ToolBtnAccent")
        self.btn_login_chrome.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_login_chrome.clicked.connect(self.on_login_chrome_clicked)

        profile_top_layout.addWidget(lbl_profile)
        profile_top_layout.addWidget(self.cbo_profile)
        profile_top_layout.addWidget(self.btn_new_profile)
        profile_top_layout.addWidget(self.btn_login_chrome)

        header_layout.addLayout(profile_top_layout)

        # NÚT BÁO TRẠNG THÁI COOKIE BILIBILI VIP TRÊN HEADER
        self.btn_login_bilibili = QToolButton()
        self.btn_login_bilibili.setText("🍪 Bilibili : Chưa Login")
        self.btn_login_bilibili.setMinimumWidth(185)
        self.btn_login_bilibili.setFixedHeight(28)
        self.btn_login_bilibili.setObjectName("ToolBtn")
        self.btn_login_bilibili.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_login_bilibili.setToolTip("Nhấp vào đây để đăng nhập Bilibili và nạp Cookie VIP 1080p/4K")
        self.btn_login_bilibili.clicked.connect(self.on_login_bilibili_clicked)

        self.lbl_system_badge = QLabel("🟢 SẴN SÀNG")
        self.lbl_system_badge.setObjectName("SystemBadge")
        self.lbl_system_badge.setMinimumWidth(100)
        self.lbl_system_badge.setFixedHeight(28)
        self.set_badge_style(self.lbl_system_badge, "#059669", "white")
        
        header_layout.addWidget(self.btn_login_bilibili)
        header_layout.addWidget(self.lbl_system_badge)

        main_layout.addWidget(header_frame)

        # ----------------------------------------------------------------------
        # 2. PIPELINE STEPPER TRACKER (6 BƯỚC)
        # ----------------------------------------------------------------------
        self.stepper_widget = PipelineStepperWidget()
        main_layout.addWidget(self.stepper_widget)

        # ----------------------------------------------------------------------
        # 3. SPLITTER CHÍNH (TOP INPUT CARDS vs BOTTOM UNIFIED EXECUTION CONSOLE)
        # ----------------------------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        # ── MASTER CARD 1: CẤU HÌNH DỰ ÁN BẤT ĐỐI XỨNG SO LE ──
        card1_group = QGroupBox("🎬 Cấu Hình Dự Án")
        card1_group.setObjectName("MasterCard")
        card1_layout = QVBoxLayout(card1_group)
        card1_layout.setContentsMargins(12, 8, 12, 8)
        card1_layout.setSpacing(6)

        # HÀNG 1 (SO LE 65% : 35%): LINK BILIBILI (65%) + GRADIO SERVER URL (35%)
        row1_layout = QHBoxLayout()
        row1_layout.setSpacing(10)

        # Link Bilibili Column (65% width)
        col_link = QVBoxLayout()
        lbl_link = QLabel("🔗 Nguồn Đầu Vào (Link Bilibili / File Video / SRT): *")
        lbl_link.setObjectName("InputLabel")
        link_box = QHBoxLayout()
        link_box.setSpacing(6)
        self.txt_link = QLineEdit()
        self.txt_link.setObjectName("MasterInput")
        self.txt_link.setPlaceholderText("Dán link Bilibili (https://...) hoặc chọn file Media/SRT...")

        self.btn_paste_link = QToolButton()
        self.btn_paste_link.setText("📋 Dán")
        self.btn_paste_link.setObjectName("ToolBtnAccent")
        self.btn_paste_link.setFixedWidth(65)
        self.btn_paste_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste_link.clicked.connect(self.paste_link_only)

        self.btn_browse_input = QToolButton()
        self.btn_browse_input.setText("📂 File")
        self.btn_browse_input.setObjectName("ToolBtn")
        self.btn_browse_input.setFixedWidth(65)
        self.btn_browse_input.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_input.clicked.connect(self.browse_smart_input_file)

        self.btn_clear_link = QToolButton()
        self.btn_clear_link.setText("❌")
        self.btn_clear_link.setObjectName("ToolBtn")
        self.btn_clear_link.setFixedWidth(40)
        self.btn_clear_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_link.clicked.connect(lambda: self.txt_link.clear())

        link_box.addWidget(self.txt_link, stretch=1)
        link_box.addWidget(self.btn_paste_link)
        link_box.addWidget(self.btn_browse_input)
        link_box.addWidget(self.btn_clear_link)
        col_link.addWidget(lbl_link)
        col_link.addLayout(link_box)

        # Gradio Server URL Column (35% width)
        col_gradio = QVBoxLayout()
        gradio_hdr_sub = QHBoxLayout()
        lbl_g_url = QLabel("🌐 Gradio URL (Thay đổi): *")
        lbl_g_url.setObjectName("InputLabel")
        self.lbl_gradio_status = QLabel("⚪ Chưa kiểm tra")
        self.lbl_gradio_status.setObjectName("StatusBadge")
        self.set_badge_style(self.lbl_gradio_status, "#334155", "#94a3b8")
        gradio_hdr_sub.addWidget(lbl_g_url)
        gradio_hdr_sub.addStretch()
        gradio_hdr_sub.addWidget(self.lbl_gradio_status)

        gradio_box = QHBoxLayout()
        gradio_box.setSpacing(6)
        self.txt_gradio_url = QLineEdit()
        self.txt_gradio_url.setObjectName("MasterInput")
        self.txt_gradio_url.setPlaceholderText("Link Gradio mới...")
        self.txt_gradio_url.editingFinished.connect(self.save_user_config)
        self.txt_gradio_url.editingFinished.connect(self.on_ping_gradio)
        self.txt_gradio_url.textChanged.connect(self.auto_ping_gradio)

        self.btn_paste_gradio = QToolButton()
        self.btn_paste_gradio.setText("📋Dán")
        self.btn_paste_gradio.setObjectName("ToolBtnAccent")
        self.btn_paste_gradio.setFixedWidth(65)
        self.btn_paste_gradio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste_gradio.clicked.connect(self.paste_gradio_url)

        self.btn_ping_gradio = QToolButton()
        self.btn_ping_gradio.setText("⚡ Ping")
        self.btn_ping_gradio.setObjectName("ToolBtn")
        self.btn_ping_gradio.setFixedWidth(65)
        self.btn_ping_gradio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ping_gradio.clicked.connect(self.on_ping_gradio)

        gradio_box.addWidget(self.txt_gradio_url, stretch=1)
        gradio_box.addWidget(self.btn_paste_gradio)
        gradio_box.addWidget(self.btn_ping_gradio)
        col_gradio.addLayout(gradio_hdr_sub)
        col_gradio.addLayout(gradio_box)

        row1_layout.addLayout(col_link, stretch=55)
        row1_layout.addLayout(col_gradio, stretch=45)
        card1_layout.addLayout(row1_layout)

        # HÀNG 2 (SO LE 35% : 65%): THƯ MỤC DỰ ÁN (35%) + CAPCUT DRAFT PATH (65%)
        row2_layout = QHBoxLayout()
        row2_layout.setSpacing(10)

        # Output Dir Column (35% width)
        col_out = QVBoxLayout()
        lbl_dir = QLabel("📁 Thư Mục Dự Án (Output): *")
        lbl_dir.setObjectName("InputLabel")
        dir_box = QHBoxLayout()
        dir_box.setSpacing(6)
        self.txt_output_dir = QLineEdit("./downloads")
        self.txt_output_dir.setObjectName("MasterInput")
        self.txt_output_dir.editingFinished.connect(self.save_user_config)

        self.btn_browse_dir = QToolButton()
        self.btn_browse_dir.setText("📂 Chọn")
        self.btn_browse_dir.setObjectName("ToolBtn")
        self.btn_browse_dir.setMinimumWidth(65)
        self.btn_browse_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_dir.clicked.connect(self.browse_output_directory)

        self.btn_open_dir = QToolButton()
        self.btn_open_dir.setText("📁 Mở")
        self.btn_open_dir.setObjectName("ToolBtn")
        self.btn_open_dir.setMinimumWidth(60)
        self.btn_open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_dir.clicked.connect(self.open_output_directory)

        dir_box.addWidget(self.txt_output_dir, stretch=1)
        dir_box.addWidget(self.btn_browse_dir)
        dir_box.addWidget(self.btn_open_dir)
        col_out.addWidget(lbl_dir)
        col_out.addLayout(dir_box)

        # CapCut Draft Column (65% width)
        col_draft = QVBoxLayout()
        lbl_draft = QLabel("📂 Đường Dẫn CapCut PC Draft (Thư mục hoặc file draft_content.json): *")
        lbl_draft.setObjectName("InputLabel")
        draft_box = QHBoxLayout()
        draft_box.setSpacing(6)
        self.txt_capcut_draft = QLineEdit()
        self.txt_capcut_draft.setObjectName("MasterInput")
        default_cp_path = get_default_capcut_path()
        if default_cp_path:
            self.txt_capcut_draft.setText(default_cp_path)
        self.txt_capcut_draft.setPlaceholderText("Thư mục com.lveditor.draft...")
        self.txt_capcut_draft.editingFinished.connect(self.save_user_config)
        self.txt_capcut_draft.textChanged.connect(self.sync_capcut_draft_to_worker)

        self.btn_browse_draft = QToolButton()
        self.btn_browse_draft.setText("📂Draft")
        self.btn_browse_draft.setObjectName("ToolBtn")
        self.btn_browse_draft.setMinimumWidth(75)
        self.btn_browse_draft.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_draft.clicked.connect(self.browse_capcut_draft)

        self.btn_auto_detect_draft = QToolButton()
        self.btn_auto_detect_draft.setText("Dọn Wav")
        self.btn_auto_detect_draft.setStyleSheet("QToolButton { background-color: #dc2626; color: white; border: 1px solid #ef4444; border-radius: 5px; font-weight: 700; font-size: 9.5pt; min-height: 28px; max-height: 28px; height: 28px; padding: 0 8px; } QToolButton:hover { background-color: #ef4444; border: 1px solid #f87171; }")
        self.btn_auto_detect_draft.setMinimumWidth(90)
        self.btn_auto_detect_draft.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_auto_detect_draft.clicked.connect(self.on_clean_ai_draft)

        draft_box.addWidget(self.txt_capcut_draft, stretch=1)
        draft_box.addWidget(self.btn_browse_draft)
        draft_box.addWidget(self.btn_auto_detect_draft)
        col_draft.addWidget(lbl_draft)
        col_draft.addLayout(draft_box)

        row2_layout.addLayout(col_out, stretch=35)
        row2_layout.addLayout(col_draft, stretch=65)
        card1_layout.addLayout(row2_layout)

        # Row Checkboxes (TRẠNG THÁI TẠO GIỌNG AI LUÔN LUÔN LÀ TRUE)
        check_row = QHBoxLayout()
        self.chk_auto_inject_capcut = QCheckBox("💉 Tự động nhúng phụ đề vào CapCut PC Draft")
        self.chk_auto_inject_capcut.setChecked(True)
        self.chk_auto_inject_capcut.setObjectName("AccentCheck")
        self.chk_auto_inject_capcut.toggled.connect(self.save_user_config)
        self.chk_auto_inject_capcut.stateChanged.connect(self.sync_auto_inject_to_worker)

        self.chk_enable_tts = QCheckBox("🎙️ Kích hoạt tạo giọng AI (TTS Engine)")
        self.chk_enable_tts.setChecked(True)  # LUÔN MẶC ĐỊNH LÀ TRUE
        self.chk_enable_tts.setObjectName("PurpleCheck")
        self.chk_enable_tts.toggled.connect(self.save_user_config)
        
        
        check_row.addWidget(self.chk_auto_inject_capcut)
        check_row.addWidget(self.chk_enable_tts)
        check_row.addStretch()
        card1_layout.addLayout(check_row)

        top_layout.addWidget(card1_group)

        # ── CARD 2: KHUNG ẨN/HIỆN CẤU HÌNH VOICE MẪU (COLLAPSIBLE PANEL) ──
        self.card_voice_group = QGroupBox()
        self.card_voice_group.setObjectName("MasterCardVoice")
        card_voice_layout = QVBoxLayout(self.card_voice_group)
        card_voice_layout.setContentsMargins(12, 2, 12, 2)
        card_voice_layout.setSpacing(2)

        # Toggle Expander Header Bar
        self.btn_toggle_voice = QPushButton("🔽 Mở Cấu Hình Voice Mẫu (.wav) & Văn Bản Transcript (Bấm để Mở/Đóng)")
        self.btn_toggle_voice.setObjectName("ExpanderBtn")
        self.btn_toggle_voice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_voice.clicked.connect(self.toggle_voice_panel)
        card_voice_layout.addWidget(self.btn_toggle_voice)

        # Sub-container chứa Voice Audio & Text Transcript (Mặc định ẩn)
        self.voice_content_widget = QWidget()
        voice_content_layout = QVBoxLayout(self.voice_content_widget)
        voice_content_layout.setContentsMargins(0, 4, 0, 0)
        voice_content_layout.setSpacing(6)

        voice_row = QHBoxLayout()
        voice_row.setSpacing(10)

        # Voice Audio field
        v_aud_col = QVBoxLayout()
        lbl_ref_aud = QLabel("🔊 File Âm Thanh Giọng Mẫu (Reference Audio .wav): *")
        lbl_ref_aud.setObjectName("InputLabel")
        voice_aud_box = QHBoxLayout()
        voice_aud_box.setSpacing(6)
        self.txt_ref_audio = QLineEdit()
        self.txt_ref_audio.setObjectName("MasterInput")
        self.txt_ref_audio.setPlaceholderText("File .wav mẫu...")
        self.txt_ref_audio.editingFinished.connect(self.save_user_config)

        self.btn_browse_ref_audio = QToolButton()
        self.btn_browse_ref_audio.setText("📂 Chọn Voice")
        self.btn_browse_ref_audio.setObjectName("ToolBtn")
        self.btn_browse_ref_audio.setFixedWidth(90)
        self.btn_browse_ref_audio.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse_ref_audio.clicked.connect(self.browse_ref_audio)

        voice_aud_box.addWidget(self.txt_ref_audio, stretch=1)
        voice_aud_box.addWidget(self.btn_browse_ref_audio)
        v_aud_col.addWidget(lbl_ref_aud)
        v_aud_col.addLayout(voice_aud_box)

        # Voice Text field
        v_txt_col = QVBoxLayout()
        lbl_ref_txt = QLabel("📝 Văn Bản Mẫu (Text Transcript của Voice): *")
        lbl_ref_txt.setObjectName("InputLabel")
        self.txt_ref_text = QLineEdit()
        self.txt_ref_text.setObjectName("MasterInput")
        self.txt_ref_text.setPlaceholderText("Văn bản câu mẫu...")
        self.txt_ref_text.editingFinished.connect(self.save_user_config)
        v_txt_col.addWidget(lbl_ref_txt)
        v_txt_col.addWidget(self.txt_ref_text)

        voice_row.addLayout(v_aud_col, stretch=1)
        voice_row.addLayout(v_txt_col, stretch=1)
        voice_content_layout.addLayout(voice_row)

        card_voice_layout.addWidget(self.voice_content_widget)
        self.voice_content_widget.setVisible(False)
        top_layout.addWidget(self.card_voice_group)

        top_scroll = QScrollArea()
        top_scroll.setObjectName("TopScrollArea")
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_scroll.setWidget(top_panel)

        splitter.addWidget(top_scroll)

        # ----------------------------------------------------------------------
        # 4. KHUNG NHẤT THỂ DƯỚI CÙNG: UNIFIED EXECUTION & MONITOR CONSOLE CARD
        # (TÍCH HỢP TIẾN ĐỘ + CỤM NÚT PHẢI NGOÀI CÙNG + CONSOLE LOG MONITOR)
        # ----------------------------------------------------------------------
        unified_console_group = QGroupBox("🖥️ Trung Tâm Điều Khiển & Monitor Nhật Ký Tiến Trình")
        unified_console_group.setObjectName("ConsoleGroup")
        unified_console_layout = QVBoxLayout(unified_console_group)
        unified_console_layout.setContentsMargins(12, 10, 12, 10)
        unified_console_layout.setSpacing(6)

        # HÀNG TOP TRONG KHUNG DƯỚI: KPI METRICS BÊN TRÁI & NÚT BẤM BÊN PHẢI NGOÀI CÙNG
        top_control_row = QHBoxLayout()
        top_control_row.setSpacing(10)

        # KPI Metrics Cards ở bên trái
        kpi_layout = QHBoxLayout()
        kpi_layout.setSpacing(8)

        self.card_status = self.create_kpi_card("TRẠNG THÁI", "Đang chờ", "#94a3b8", "kpi_status")
        self.card_elapsed = self.create_kpi_card("ĐÃ CHẠY", "00:00:00", "#38bdf8", "kpi_elapsed")
        self.card_eta = self.create_kpi_card("CÒN LẠI", "--:--", "#a78bfa", "kpi_eta")
        self.card_percent = self.create_kpi_card("TIẾN ĐỘ", "0%", "#10b981", "kpi_percent")

        kpi_layout.addWidget(self.card_status)
        kpi_layout.addWidget(self.card_elapsed)
        kpi_layout.addWidget(self.card_eta)
        kpi_layout.addWidget(self.card_percent)
        top_control_row.addLayout(kpi_layout)

        top_control_row.addStretch()  # ĐẨY CỤM NÚT SANG BÊN PHẢI NGOÀI CÙNG

        # Cụm Nút Hành Động ở góc bên phải ngoài cùng
        self.btn_stop = QPushButton("🛑 DỪNG LẠI")
        self.btn_stop.setObjectName("BtnStop")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setFixedWidth(110)
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.clicked.connect(self.on_stop_clicked)

        self.btn_pause = QPushButton("⏸️ TẠM DỪNG")
        self.btn_pause.setObjectName("BtnPause")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setFixedWidth(120)
        self.btn_pause.setFixedHeight(36)
        self.btn_pause.clicked.connect(self.on_pause_clicked)

        self.btn_run = QPushButton("▶️ BẮT ĐẦU DỰ ÁN")
        self.btn_run.setObjectName("BtnRun")
        self.btn_run.setToolTip("Khởi động tự động toàn bộ quy trình 1-Click!")
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.setFixedWidth(165)  # BÊN PHẢI NGOÀI CÙNG
        self.btn_run.setFixedHeight(36)
        self.btn_run.clicked.connect(self.on_run_clicked)

        top_control_row.addWidget(self.btn_stop)
        top_control_row.addWidget(self.btn_pause)
        top_control_row.addWidget(self.btn_run)

        unified_console_layout.addLayout(top_control_row)

        # HÀNG MID: THANH PROGRESS BAR GẤP ĐÔI ĐỘ DÀY (16PX) CHẠY 100% CHIỀU NGANG
        # UI: Two progress bars
        progress_layout = QVBoxLayout()
        progress_layout.setSpacing(5)
        
        lbl_global = QLabel("🌍 Tiến độ Tổng Thể (Global):")
        lbl_global.setStyleSheet("color: #94a3b8; font-size: 11px;")
        
        self.lbl_global_pct = QLabel("0%")
        self.lbl_global_pct.setStyleSheet("color: #3b82f6; font-size: 11px; font-weight: bold;")
        self.lbl_global_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        global_label_layout = QHBoxLayout()
        global_label_layout.addWidget(lbl_global)
        global_label_layout.addWidget(self.lbl_global_pct)

        self.global_progress_bar = QProgressBar()
        self.global_progress_bar.setObjectName("GlobalProgressBar")
        self.global_progress_bar.setRange(0, 100)
        self.global_progress_bar.setValue(0)
        self.global_progress_bar.setFixedHeight(12)
        self.global_progress_bar.setTextVisible(False)
        self.global_progress_bar.setStyleSheet("QProgressBar { border: none; border-radius: 6px; background: #0f172a; } QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #8b5cf6); border-radius: 6px; }")
        
        from PyQt6.QtWidgets import QGraphicsDropShadowEffect
        from PyQt6.QtGui import QColor
        shadow_global = QGraphicsDropShadowEffect()
        shadow_global.setBlurRadius(10)
        shadow_global.setColor(QColor(0, 0, 0, 200))
        shadow_global.setOffset(0, 2)
        self.global_progress_bar.setGraphicsEffect(shadow_global)

        lbl_local = QLabel("🎯 Tiến độ Bước Hiện Tại (Local):")
        lbl_local.setStyleSheet("color: #94a3b8; font-size: 11px;")
        
        self.lbl_local_pct = QLabel("0%")
        self.lbl_local_pct.setStyleSheet("color: #10b981; font-size: 11px; font-weight: bold;")
        self.lbl_local_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        local_label_layout = QHBoxLayout()
        local_label_layout.addWidget(lbl_local)
        local_label_layout.addWidget(self.lbl_local_pct)

        self.local_progress_bar = QProgressBar()
        self.local_progress_bar.setObjectName("LocalProgressBar")
        self.local_progress_bar.setRange(0, 100)
        self.local_progress_bar.setValue(0)
        self.local_progress_bar.setFixedHeight(12)
        self.local_progress_bar.setTextVisible(False)
        self.local_progress_bar.setStyleSheet("QProgressBar { border: none; border-radius: 6px; background: #0f172a; } QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #10b981, stop:1 #34d399); border-radius: 6px; }")
        
        shadow_local = QGraphicsDropShadowEffect()
        shadow_local.setBlurRadius(10)
        shadow_local.setColor(QColor(0, 0, 0, 200))
        shadow_local.setOffset(0, 2)
        self.local_progress_bar.setGraphicsEffect(shadow_local)

        progress_layout.addLayout(global_label_layout)
        progress_layout.addWidget(self.global_progress_bar)
        progress_layout.addLayout(local_label_layout)
        progress_layout.addWidget(self.local_progress_bar)
        
        unified_console_layout.addLayout(progress_layout)

        # HÀNG LỌC LOG TOOLBAR
        console_toolbar = QHBoxLayout()
        console_toolbar.setSpacing(8)

        lbl_filter = QLabel("🔍 Bộ lọc:")
        lbl_filter.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 9.5pt;")

        self.cbo_log_level = QComboBox()
        self.cbo_log_level.setObjectName("LogFilterCombo")
        self.cbo_log_level.addItems(["Tất cả log", "ℹ️ Info", "✅ Success", "⚠️ Warning", "❌ Error"])
        self.cbo_log_level.currentIndexChanged.connect(self.filter_logs)

        self.txt_log_search = QLineEdit()
        self.txt_log_search.setObjectName("LogSearchInput")
        self.txt_log_search.setPlaceholderText("Tìm từ khóa trong log...")
        self.txt_log_search.textChanged.connect(self.filter_logs)

        self.chk_autoscroll = QCheckBox("📌 AutoScroll")
        self.chk_autoscroll.setObjectName("AutoScrollCheck")
        self.chk_autoscroll.setChecked(True)
        self.chk_autoscroll.toggled.connect(self.save_user_config)

        self.btn_copy_log = QPushButton("📋 Sao Chép Log")
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

        unified_console_layout.addLayout(console_toolbar)

        # MÀNH HÌNH CONSOLE LOG MONITOR (CHIẾM DIỆN TÍCH CHỦ ĐẠO PHÍA DƯỚI)
        self.txt_console = QTextEdit()
        self.txt_console.setObjectName("ConsoleMonitor")
        self.txt_console.setReadOnly(True)
        unified_console_layout.addWidget(self.txt_console, stretch=1)

        splitter.addWidget(unified_console_group)
        
        # SPLITTER DÀNH KHÔNG GIAN LỚN CHO KHUNG DƯỚI UNIFIED CONSOLE (Top 280px, Bottom 520px)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([280, 520])

        main_layout.addWidget(splitter, stretch=1)

        self.refresh_profile_list()
        self.update_profile_login_status_ui()
        self.append_log("✨ Chào mừng bạn tới CapCutInjector Pro Studio v3! Đã chuẩn bị sẵn sàng.", "info")
        self.update_cookie_badge_status()

    # ==========================================================================
    # HELPER UI & EVENT HANDLERS
    # ==========================================================================
    def toggle_voice_panel(self):
        self.is_voice_expanded = not self.is_voice_expanded
        self.voice_content_widget.setVisible(self.is_voice_expanded)
        if self.is_voice_expanded:
            self.btn_toggle_voice.setText("▲ Thu Gọn Cấu Hình Voice Mẫu (.wav) & Văn Bản Transcript")
        else:
            self.btn_toggle_voice.setText("🔽 Mở Cấu Hình Voice Mẫu (.wav) & Văn Bản Transcript (Bấm để Mở/Đóng)")

    def create_kpi_card(self, title: str, initial_value: str, color_hex: str, object_name: str) -> QFrame:
        card = QFrame()
        card.setObjectName("KpiCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)

        lbl_t = QLabel(title)
        lbl_t.setObjectName("KpiTitle")

        lbl_v = QLabel(initial_value)
        lbl_v.setObjectName(object_name)
        lbl_v.setStyleSheet(f"color: {color_hex}; font-size: 10pt; font-weight: bold;")

        layout.addWidget(lbl_t)
        layout.addWidget(lbl_v)
        return card

    def update_kpi_value(self, object_name: str, value_text: str, color_hex: str = None):
        lbl = self.findChild(QLabel, object_name)
        if lbl:
            lbl.setText(value_text)
            if color_hex:
                lbl.setStyleSheet(f"color: {color_hex}; font-size: 10pt; font-weight: bold;")

    def setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.on_run_clicked)
        QShortcut(QKeySequence("Ctrl+P"), self).activated.connect(self.on_pause_clicked)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self.clear_console)
        QShortcut(QKeySequence("F5"), self).activated.connect(self.restart_app)

    def set_badge_style(self, label: QLabel, bg_color: str, text_color: str = "white"):
        label.setStyleSheet(f"background-color: {bg_color}; color: {text_color}; font-size: 9.5pt; font-weight: 600; padding: 0 10px; border-radius: 5px; border: 1px solid {bg_color}; min-height: 28px; max-height: 28px; height: 28px;")

    def update_cookie_badge_status(self):
        os.makedirs("./user_data/cookies", exist_ok=True)
        possible_files = ["./user_data/cookies/cookies.txt", "./user_data/cookies.txt", "./cookies.txt"]
        has_cookie = any(os.path.exists(p) for p in possible_files)

        if hasattr(self, 'btn_login_bilibili'):
            if has_cookie:
                self.btn_login_bilibili.setText("⭐ Bilibili : 1080p")
                self.btn_login_bilibili.setStyleSheet("background-color: #059669; color: white; font-size: 9.5pt; font-weight: 600; padding: 0 10px; border-radius: 5px; border: 1px solid #10b981; min-height: 28px; max-height: 28px; height: 28px;")
            else:
                self.btn_login_bilibili.setText("🍪 Bilibili : Chưa Login")
                self.btn_login_bilibili.setStyleSheet("background-color: #334155; color: #cbd5e1; font-size: 9.5pt; font-weight: 600; padding: 0 10px; border-radius: 5px; border: 1px solid #475569; min-height: 28px; max-height: 28px; height: 28px;")

    def on_clean_ai_draft(self):
        draft_path = self.txt_capcut_draft.text().strip()
        if not draft_path or not os.path.exists(draft_path):
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn thư mục CapCut Draft hợp lệ trước khi Gỡ AI!")
            return
            
        json_path = draft_path if draft_path.lower().endswith(".json") else os.path.join(draft_path, "draft_content.json")
        if not os.path.exists(json_path):
            QMessageBox.warning(self, "Lỗi", f"Không tìm thấy draft_content.json tại:\n{json_path}")
            return
            
        reply = QMessageBox.question(self, "Xác Nhận", "Bạn có chắc chắn muốn dọn dẹp và GỠ BỎ toàn bộ âm thanh AI khỏi dự án CapCut này không?\n\n(Chỉ xóa AI do phần mềm tạo, tuyệt đối không ảnh hưởng đến âm thanh bạn tự ghép)", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.No: return
        
        try:
            import json
            import shutil
            shutil.copy(json_path, json_path + ".clean.backup")
            with open(json_path, "r", encoding="utf-8") as f:
                draft = json.load(f)
                
            audio_materials_to_delete = set()
            new_tracks = []
            deleted_tracks = 0
            
            for track in draft.get("tracks", []):
                if track.get("type") == "audio" and track.get("name", "").startswith("AI_Auto_Layer_"):
                    deleted_tracks += 1
                    for seg in track.get("segments", []):
                        mat_id = seg.get("material_id")
                        if mat_id: audio_materials_to_delete.add(mat_id)
                else:
                    new_tracks.append(track)
                    
            draft["tracks"] = new_tracks
            
            deleted_audios = 0
            if "materials" in draft and "audios" in draft["materials"]:
                old_audios = draft["materials"]["audios"]
                new_audios = [a for a in old_audios if a.get("id") not in audio_materials_to_delete]
                draft["materials"]["audios"] = new_audios
                deleted_audios = len(old_audios) - len(new_audios)
                
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(draft, f, ensure_ascii=False)
                
            self.append_log(f"🧹 Đã gỡ thành công {deleted_tracks} Track và {deleted_audios} khối âm thanh AI khỏi CapCut!", "success")
            QMessageBox.information(self, "Hoàn Tất", f"Đã dọn dẹp dự án thành công!\n\n- Đã xóa {deleted_tracks} dòng Track (AI_Auto_Layer)\n- Đã xóa {deleted_audios} khối âm thanh (Media Lost)")
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Có lỗi xảy ra trong quá trình dọn dẹp: {e}")

    def on_ping_gradio(self, *args):
        url = self.txt_gradio_url.text().strip()
        if not url:
            self.lbl_gradio_status.setText("⚪ Chưa kiểm tra")
            self.set_badge_style(self.lbl_gradio_status, "#334155", "#94a3b8")
            return
        try:
            self.lbl_gradio_status.setText("⏳ Đang Ping...")
            self.set_badge_style(self.lbl_gradio_status, "#d97706", "white")
            QApplication.processEvents() # Force UI update before ping
            res = requests.get(url, timeout=2)
            if res.status_code in [200, 301, 302]:
                self.lbl_gradio_status.setText("🟢 Online")
                self.set_badge_style(self.lbl_gradio_status, "#059669", "white")
                self.append_log(f"🟢 Server Gradio TTS phản hồi tốt ({url})", "success")
            else:
                self.lbl_gradio_status.setText("🔴 Offline")
                self.set_badge_style(self.lbl_gradio_status, "#dc2626", "white")
                self.append_log(f"🔴 Server Gradio trả về mã lỗi: {res.status_code}", "error")
        except Exception as e:
            self.lbl_gradio_status.setText("🔴 Lỗi Kết Nối")
            self.set_badge_style(self.lbl_gradio_status, "#dc2626", "white")
            self.append_log(f"🔴 Không thể kết nối tới Server Gradio: {e}", "error")

    def load_user_config(self):
        config_path = "./user_data/config/user_config.json"
        if os.path.exists(config_path):
            try:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                last_url = cfg.get("last_url", "")
                last_dir = cfg.get("last_dir", "./downloads")
                last_draft = cfg.get("last_draft", "")
                last_profile = cfg.get("last_profile", "")
                last_gradio = cfg.get("last_gradio_url", "")
                last_ref_aud = cfg.get("last_ref_audio", "")
                last_ref_txt = cfg.get("last_ref_text", "")
                enable_tts = cfg.get("enable_tts", True)
                auto_inject_capcut = cfg.get("auto_inject_capcut", True)
                autoscroll = cfg.get("autoscroll", True)

                if last_url:
                    self.txt_link.setText(last_url)
                if last_dir:
                    self.txt_output_dir.setText(last_dir)
                if last_draft:
                    self.txt_capcut_draft.setText(last_draft)
                if last_profile and hasattr(self, 'cbo_profile'):
                    idx = self.cbo_profile.findText(last_profile)
                    if idx >= 0:
                        self.cbo_profile.setCurrentIndex(idx)
                if last_gradio and hasattr(self, 'txt_gradio_url'):
                    self.txt_gradio_url.setText(last_gradio)
                if last_ref_aud and hasattr(self, 'txt_ref_audio'):
                    self.txt_ref_audio.setText(last_ref_aud)
                if last_ref_txt and hasattr(self, 'txt_ref_text'):
                    self.txt_ref_text.setText(last_ref_txt)
                if hasattr(self, 'chk_enable_tts'):
                    self.chk_enable_tts.setChecked(enable_tts)
                if hasattr(self, 'chk_auto_inject_capcut'):
                    self.chk_auto_inject_capcut.setChecked(auto_inject_capcut)
                if hasattr(self, 'chk_autoscroll'):
                    self.chk_autoscroll.setChecked(autoscroll)

                self.append_log(f"⚙️ Đã nạp cấu hình đã lưu thành công.", "info")
            except Exception:
                pass

    def save_user_config(self):
        os.makedirs("./user_data/config", exist_ok=True)
        config_path = "./user_data/config/user_config.json"
        try:
            import json
            cfg = {
                "last_url": self.txt_link.text().strip(),
                "last_dir": self.txt_output_dir.text().strip() or "./downloads",
                "last_draft": self.txt_capcut_draft.text().strip(),
                "last_profile": self.get_selected_profile(),
                "last_gradio_url": self.txt_gradio_url.text().strip(),
                "last_ref_audio": self.txt_ref_audio.text().strip(),
                "last_ref_text": self.txt_ref_text.text().strip(),
                "enable_tts": self.chk_enable_tts.isChecked() if hasattr(self, 'chk_enable_tts') else True,
                "auto_inject_capcut": self.chk_auto_inject_capcut.isChecked() if hasattr(self, 'chk_auto_inject_capcut') else True,
                "autoscroll": self.chk_autoscroll.isChecked() if hasattr(self, 'chk_autoscroll') else True
            }
            existing = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing.update(cfg)
            
            # Map chéo về chuẩn V1 để UI cũ cũng đọc được
            existing['CAPCUT_JSON_PATH'] = cfg['last_draft']
            existing['SERVER_URL'] = cfg['last_gradio_url']
            existing['REF_AUDIO_PATH'] = cfg['last_ref_audio']
            existing['REF_TEXT'] = cfg['last_ref_text']
            existing['INJECT_ONLY'] = not cfg['enable_tts']

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def get_selected_profile(self) -> str:
        if hasattr(self, 'cbo_profile') and self.cbo_profile.currentText():
            return self.cbo_profile.currentText()
        return "chrome_data_1"

    def refresh_profile_list(self):
        current = self.cbo_profile.currentText() if hasattr(self, 'cbo_profile') else ""
        profiles = get_available_profiles() if get_available_profiles else ["chrome_data_1", "chrome_data_2"]
        
        if hasattr(self, 'cbo_profile'):
            self.cbo_profile.blockSignals(True)
            self.cbo_profile.clear()
            self.cbo_profile.addItems(profiles)
            if current in profiles:
                self.cbo_profile.setCurrentText(current)
            self.cbo_profile.blockSignals(False)

    def on_create_profile_clicked(self):
        if create_new_profile:
            new_p = create_new_profile()
            self.append_log(f"✨ Đã tạo Profile Chrome mới: [{new_p}]", "success")
            self.refresh_profile_list()
            self.cbo_profile.setCurrentText(new_p)
            self.save_user_config()

    def update_profile_login_status_ui(self):
        selected_profile = self.get_selected_profile()
        has_data = is_profile_logged_in(selected_profile) if is_profile_logged_in else False
        if hasattr(self, 'login_worker') and self.login_worker and self.login_worker.isRunning():
            return
        self.btn_login_chrome.setEnabled(True)
        if has_data:
            self.btn_login_chrome.setText("🟢 online")
            self.btn_login_chrome.setStyleSheet("background-color: #059669; color: white; font-weight: bold; border-radius: 6px;")
        else:
            self.btn_login_chrome.setText("🔑 Login")
            self.btn_login_chrome.setStyleSheet("")

    def on_login_chrome_clicked(self):
        selected_profile = self.get_selected_profile()
        self.btn_login_chrome.setEnabled(False)
        self.btn_login_chrome.setText("⏳ Mở Chrome...")
        self.append_log(f"🔑 Mở Chrome Profile [{selected_profile}] để đăng nhập...", "info")

        self.login_worker = ChromeLoginWorker(selected_profile)
        self.login_worker.log_signal.connect(self.append_log)
        def on_login_finished(success, msg):
            self.update_profile_login_status_ui()
            if success:
                self.append_log(f"🎉 {msg}", "success")
            else:
                self.append_log(f"⚠️ {msg}", "warning")
        self.login_worker.finished_signal.connect(on_login_finished)
        self.login_worker.start()

    def browse_ref_audio(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn file Voice Mẫu", self.txt_output_dir.text().strip(), "Tệp Âm thanh (*.wav *.mp3 *.flac);;Tất cả (*.*)")
        if f:
            self.txt_ref_audio.setText(f)
            self.save_user_config()

    def browse_smart_input_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn tệp Đầu vào", self.txt_output_dir.text().strip(), "Tệp Đầu Vào (*.mp4 *.mkv *.avi *.mp3 *.wav *.srt);;Tất cả (*.*)")
        if f:
            self.txt_link.setText(f)

    def browse_capcut_draft(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn file draft_content.json", self.txt_capcut_draft.text().strip(), "JSON Files (*.json);;All Files (*.*)")
        if f:
            self.txt_capcut_draft.setText(f)
            self.save_user_config()

    def browse_output_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu dự án")
        if dir_path:
            self.txt_output_dir.setText(dir_path)
            self.save_user_config()

    def open_output_directory(self):
        dir_path = os.path.abspath(self.txt_output_dir.text().strip() or "./downloads")
        os.makedirs(dir_path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(dir_path))

    def paste_link_only(self):
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            self.txt_link.setText(text)

    def paste_gradio_url(self):
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            self.txt_gradio_url.setText(text)
            self.save_user_config()
            self.on_ping_gradio()

    def on_login_bilibili_clicked(self):
        if hasattr(self, 'btn_login_bilibili'):
            self.btn_login_bilibili.setEnabled(False)
        self.append_log("🚀 Khởi chạy cửa sổ đăng nhập Bilibili...", "info")
        self.bilibili_login_worker = BilibiliLoginWorker()
        self.bilibili_login_worker.log_signal.connect(self.append_log)
        self.bilibili_login_worker.finished_signal.connect(self.on_bilibili_login_finished)
        self.bilibili_login_worker.start()

    def on_bilibili_login_finished(self, success: bool, message: str):
        if hasattr(self, 'btn_login_bilibili'):
            self.btn_login_bilibili.setEnabled(True)
        if success:
            QMessageBox.information(self, "Đăng Nhập Thành Công 🎉", message)
        else:
            QMessageBox.warning(self, "Đăng Nhập Thất Bại ⚠️", f"Không thể lưu Cookie Bilibili: {message}")

    # ── VALIDATION & LOGIC CHẠY TỰ ĐỘNG ──
    def on_run_clicked(self):
        self.txt_link.setStyleSheet("")
        self.txt_output_dir.setStyleSheet("")
        self.txt_capcut_draft.setStyleSheet("")
        self.txt_gradio_url.setStyleSheet("")
        self.txt_ref_audio.setStyleSheet("")
        self.txt_ref_text.setStyleSheet("")

        link = self.txt_link.text().strip()
        out_dir = self.txt_output_dir.text().strip()
        draft = self.txt_capcut_draft.text().strip()
        gradio = self.txt_gradio_url.text().strip()
        ref_aud = self.txt_ref_audio.text().strip()
        ref_txt = self.txt_ref_text.text().strip()
        enable_tts = self.chk_enable_tts.isChecked() if hasattr(self, 'chk_enable_tts') else True

        missing_fields = []
        if not link:
            self.txt_link.setStyleSheet("border: 2px solid #ef4444; background-color: #451a03;")
            missing_fields.append("Nguồn đầu vào (Link / File)")
        if not out_dir:
            self.txt_output_dir.setStyleSheet("border: 2px solid #ef4444; background-color: #451a03;")
            missing_fields.append("Thư mục dự án")
        if self.chk_auto_inject_capcut.isChecked() and not draft:
            self.txt_capcut_draft.setStyleSheet("border: 2px solid #ef4444; background-color: #451a03;")
            missing_fields.append("Đường dẫn CapCut Draft")

        if enable_tts:
            if not ref_aud:
                self.txt_ref_audio.setStyleSheet("border: 2px solid #ef4444; background-color: #451a03;")
                missing_fields.append("File Voice Mẫu (.wav)")
            if not ref_txt:
                self.txt_ref_text.setStyleSheet("border: 2px solid #ef4444; background-color: #451a03;")
                missing_fields.append("Văn bản Voice Mẫu")
            if not gradio:
                self.append_log("💡 Gradio URL đang để trống. Hệ thống sẽ tự động TẠM DỪNG ở Bước 5 để bạn dán link mới từ Colab!", "info")

        if missing_fields:
            msg = "Vui lòng bổ sung các thông tin bắt buộc sau trước khi bấm BẮT ĐẦU:\n\n• " + "\n• ".join(missing_fields)
            QMessageBox.warning(self, "Thiếu Thông Tin Bắt Buộc ⚠️", msg)
            return

        self.save_user_config()

        local_media = None
        srt_translate = None
        target_url = ""

        if link.startswith("http://") or link.startswith("https://"):
            target_url = link
        elif os.path.exists(link):
            if link.lower().endswith(".srt"):
                srt_translate = link
            else:
                local_media = link
        else:
            target_url = link

        fast_forward = False
        final_srt_path_check = os.path.join(out_dir, "output.srt")
        if os.path.exists(final_srt_path_check):
            reply = QMessageBox.question(
                self, "Phát hiện File Output", 
                "Phát hiện file 'output.srt' đã tồn tại sẵn trong thư mục dự án.\n\nBạn có muốn BỎ QUA toàn bộ các bước Tải/Dịch/QA và dùng luôn file này để nhảy thẳng sang bước sinh Voice & Nhúng CapCut không?", 
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                fast_forward = True

        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

        self.global_progress_bar.setValue(0)
        self.local_progress_bar.setValue(0)
        if hasattr(self, 'lbl_global_pct'):
            self.lbl_global_pct.setText("0%")
            self.lbl_local_pct.setText("0%")
        self.update_kpi_value("kpi_percent", "0%", "#10b981")
        self.update_kpi_value("kpi_status", "Đang xử lý ⚡", "#6366f1")
        self.update_kpi_value("kpi_elapsed", "00:00:00", "#38bdf8")

        self.lbl_system_badge.setText("⚡ CHẠY TỰ ĐỘNG")
        self.set_badge_style(self.lbl_system_badge, "#0284c7", "white")

        self.start_timestamp = time.time()
        self.timer.start()

        self.worker = ProcessWorker(
            link=target_url,
            output_dir=out_dir,
            local_media_path=local_media,
            srt_translate_path=srt_translate,
            profile_folder=self.get_selected_profile(),
            auto_inject_capcut=self.chk_auto_inject_capcut.isChecked(),
            capcut_draft_path=draft,
            enable_tts=enable_tts,
            gradio_url=gradio,
            ref_audio_path=ref_aud,
            ref_text=ref_txt,
            fast_forward_mode=fast_forward
        )
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_local_progress)
        self.worker.global_progress_signal.connect(self.update_global_progress)
        self.worker.step_signal.connect(self.stepper_widget.set_step)
        self.worker.request_gradio_link_signal.connect(self.on_request_gradio_link)
        self.worker.finished_signal.connect(self.on_process_finished)
        self.worker.start()

    def on_request_gradio_link(self):
        self.is_paused = True
        self.timer.stop()
        self.btn_pause.setText("▶️ DÁN LINK & TIẾP TỤC")
        self.update_kpi_value("kpi_status", "CHỜ GRADIO URL ⏳", "#a855f7")
        self.lbl_system_badge.setText("⏳ CHỜ COLAB")
        self.set_badge_style(self.lbl_system_badge, "#7e22ce", "white")
        self.txt_gradio_url.setStyleSheet("border: 2px solid #a855f7; background-color: #3b0764;")
        self.txt_gradio_url.setFocus()
        QMessageBox.information(
            self,
            "Đã Xong Phụ Đề output.srt 🎉",
            "🎉 File phụ đề output.srt đã hoàn tất 100%!\n\n"
            "Bây giờ bạn hãy mở Google Colab, chạy cell để lấy link Gradio mới (https://xxxx.gradio.live),\n"
            "dán vào ô 'Gradio URL' trên giao diện và bấm nút '▶️ DÁN LINK & TIẾP TỤC' để sinh giọng nói AI!"
        )

    def auto_ping_gradio(self, text):
        text = text.strip()
        # Nếu đang ở trạng thái tạm dừng chờ Gradio URL
        if getattr(self, 'is_paused', False) and self.worker and self.worker.isRunning() and self.btn_pause.text() == "▶️ DÁN LINK & TIẾP TỤC":
            if (text.startswith("http://") or text.startswith("https://")) and len(text) > 15:
                self.append_log(f"⚡ Đã phát hiện dán link Gradio: {text}. Tự động kiểm tra...", "info")
                self.on_pause_clicked()

    def sync_capcut_draft_to_worker(self):
        if hasattr(self, 'worker') and self.worker and self.worker.isRunning():
            self.worker.capcut_draft_path = self.txt_capcut_draft.text().strip()

    def sync_auto_inject_to_worker(self):
        if hasattr(self, 'worker') and self.worker and self.worker.isRunning():
            self.worker.auto_inject_capcut = self.chk_auto_inject_capcut.isChecked()

    def on_pause_clicked(self):
        if not self.worker or not self.worker.isRunning():
            return
        if not self.is_paused:
            self.worker.pause()
            self.is_paused = True
            self.timer.stop()
            self.btn_pause.setText("▶️ TIẾP TỤC")
            self.update_kpi_value("kpi_status", "ĐÃ TẠM DỪNG ⏸️", "#f59e0b")
        else:
            new_gradio = self.txt_gradio_url.text().strip()
            if new_gradio and self.worker:
                self.worker.update_gradio_url(new_gradio)
            self.save_user_config()
            self.txt_gradio_url.setStyleSheet("")
            self.worker.resume()
            self.is_paused = False
            self.timer.start()
            self.btn_pause.setText("⏸️ TẠM DỪNG")
            self.update_kpi_value("kpi_status", "Đang xử lý ⚡", "#6366f1")
            self.lbl_system_badge.setText("⚡ CHẠY TỰ ĐỘNG")
            self.set_badge_style(self.lbl_system_badge, "#0284c7", "white")

    def on_stop_clicked(self):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(self, "Xác Nhận Dừng", "🛑 Bạn có chắc chắn muốn DỪNG tiến trình?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.stop()

    def on_process_finished(self, success: bool, message: str):
        self.timer.stop()
        self.btn_run.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.is_paused = False

        if success:
            self.update_kpi_value("kpi_status", "HOÀN THÀNH 🎉", "#10b981")
            self.update_kpi_value("kpi_percent", "100%", "#10b981")
            self.lbl_system_badge.setText("✅ HOÀN THÀNH")
            self.set_badge_style(self.lbl_system_badge, "#059669", "white")
            self.append_log(f"SUCCESS: {message}", "success")
        else:
            self.update_kpi_value("kpi_status", "THẤT BẠI ❌", "#ef4444")
            self.lbl_system_badge.setText("❌ LỖI")
            self.set_badge_style(self.lbl_system_badge, "#dc2626", "white")

    def update_local_progress(self, pct: int, status: str):
        if pct >= 0:
            self.lbl_local_pct.setText(f"{pct}%")
            self.anim_local = QPropertyAnimation(self.local_progress_bar, b"value")
            self.anim_local.setDuration(400)
            self.anim_local.setStartValue(self.local_progress_bar.value())
            self.anim_local.setEndValue(pct)
            self.anim_local.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.anim_local.start()
        # Tùy chọn: bạn có thể update KPI status ở cấp độ local nếu muốn
        # self.update_kpi_value("kpi_status", status, "#38bdf8")

    def update_global_progress(self, pct: int, status: str):
        if pct >= 0:
            self.lbl_global_pct.setText(f"{pct}%")
            self.anim_global = QPropertyAnimation(self.global_progress_bar, b"value")
            self.anim_global.setDuration(400)
            self.anim_global.setStartValue(self.global_progress_bar.value())
            self.anim_global.setEndValue(pct)
            self.anim_global.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.anim_global.start()
            self.update_kpi_value("kpi_percent", f"{pct}%", "#10b981")
        self.update_kpi_value("kpi_status", status, "#38bdf8")

    def update_timer_display(self):
        if self.start_timestamp:
            elapsed_sec = int(time.time() - self.start_timestamp)
            mins, secs = divmod(elapsed_sec, 60)
            hours, mins = divmod(mins, 60)
            time_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
            self.update_kpi_value("kpi_elapsed", time_str, "#38bdf8")

    def append_log(self, msg: str, level: str = "info"):
        timestamp = datetime.now().strftime("[%H:%M:%S]")
        self.logs_history.append((timestamp, level, msg))

        color_map = {
            "info": "#93c5fd",
            "success": "#34d399",
            "warning": "#fbbf24",
            "error": "#fca5a5"
        }
        color = color_map.get(level.lower(), "#e2e8f0")
        formatted = f'<span style="color: #64748b;">{timestamp}</span> <span style="color: {color}; font-weight: 500;">{msg}</span>'

        self.txt_console.append(formatted)
        if self.chk_autoscroll.isChecked():
            self.txt_console.moveCursor(QTextCursor.MoveOperation.End)

    def filter_logs(self):
        query = self.txt_log_search.text().strip().lower()
        level_filter = self.cbo_log_level.currentText()

        self.txt_console.clear()
        for ts, lvl, msg in self.logs_history:
            if level_filter == "ℹ️ Info" and lvl != "info": continue
            if level_filter == "✅ Success" and lvl != "success": continue
            if level_filter == "⚠️ Warning" and lvl != "warning": continue
            if level_filter == "❌ Error" and lvl != "error": continue
            if query and query not in msg.lower(): continue

            color = {"info": "#93c5fd", "success": "#34d399", "warning": "#fbbf24", "error": "#fca5a5"}.get(lvl, "#e2e8f0")
            formatted = f'<span style="color: #64748b;">{ts}</span> <span style="color: {color}; font-weight: 500;">{msg}</span>'
            self.txt_console.append(formatted)

    def clear_console(self):
        self.logs_history.clear()
        self.txt_console.clear()

    def restart_app(self):
        self.append_log("🔄 Đang khởi động lại phần mềm (Fast Restart) để nạp code mới...", "info")
        import sys
        QApplication.quit()
        os.execl(sys.executable, sys.executable, *sys.argv)

    def copy_logs(self):
        plain_text = self.txt_console.toPlainText()
        if plain_text:
            QApplication.clipboard().setText(plain_text)
            QMessageBox.information(self, "Đã Sao Chép", "Đã sao chép toàn bộ nhật ký Log vào Clipboard!")

    # ==========================================================================
    # CENTRALIZED STYLESHEET (V7 UNIFIED SYSTEM)
    # ==========================================================================
    def setup_stylesheet(self):
        qss = """
        QMainWindow {
            background-color: #0b0f17;
        }
        QWidget {
            font-family: 'Segoe UI Variable', 'Segoe UI', 'Inter', sans-serif;
            color: #f8fafc;
        }

        #HeaderFrame {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #161f30, stop:1 #0f172a);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
        }
        #MainTitle {
            color: #38bdf8;
            font-size: 12pt;
            font-weight: 800;
            letter-spacing: 0.5px;
        }
        #SubTitle {
            color: #94a3b8;
            font-size: 9.5pt;
        }

        QGroupBox {
            background-color: #161f30;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            margin-top: 8px;
            font-size: 10pt;
            font-weight: 700;
            color: #38bdf8;
            padding-top: 14px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 12px;
            padding: 0 6px;
            background-color: #161f30;
        }

        #MasterCardVoice {
            background-color: #161f30;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            margin-top: 2px;
            padding-top: 2px;
            padding-bottom: 2px;
        }

        #InputLabel {
            color: #cbd5e1;
            font-weight: 600;
            font-size: 10pt;
        }
        #MasterInput {
            background-color: #0b0f17;
            color: #f8fafc;
            border: 1px solid #334155;
            border-radius: 5px;
            padding: 0 10px;
            font-size: 9.5pt;
            min-height: 30px;
            max-height: 30px;
            height: 30px;
        }
        #MasterInput:focus {
            border: 1.5px solid #38bdf8;
            background-color: #030712;
        }

        #ToolBtn {
            background-color: #1e293b;
            color: #e2e8f0;
            border: 1px solid #334155;
            border-radius: 5px;
            padding: 0 8px;
            font-size: 9.5pt;
            font-weight: 600;
            min-height: 28px;
            max-height: 28px;
            height: 28px;
        }
        #ToolBtn:hover {
            background-color: #334155;
            color: white;
            border-color: #475569;
        }
        #ToolBtnAccent {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #0ea5e9);
            color: white;
            border: 1px solid #38bdf8;
            border-radius: 5px;
            padding: 0 8px;
            font-size: 9.5pt;
            font-weight: 700;
            min-height: 28px;
            max-height: 28px;
            height: 28px;
        }
        #ToolBtnAccent:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0ea5e9, stop:1 #38bdf8);
        }

        #ExpanderBtn {
            background-color: #1a2436;
            color: #38bdf8;
            border: 1px dashed #334155;
            border-radius: 8px;
            padding: 5px 12px;
            font-size: 9.5pt;
            font-weight: 600;
            text-align: left;
        }
        #ExpanderBtn:hover {
            background-color: #1e293b;
            border-color: #38bdf8;
            color: white;
        }

        #AccentCheck {
            color: #38bdf8;
            font-weight: 700;
            font-size: 9.5pt;
        }
        #PurpleCheck {
            color: #a78bfa;
            font-weight: 700;
            font-size: 9.5pt;
        }

        #BtnRun {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:0.5 #2563eb, stop:1 #4f46e5);
            border: 1px solid #60a5fa;
            border-radius: 8px;
            color: white;
            font-size: 10.5pt;
            font-weight: 800;
            min-height: 36px;
            max-height: 38px;
        }
        #BtnRun:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0369a1, stop:0.5 #1d4ed8, stop:1 #4338ca);
            border-color: #93c5fd;
        }
        #BtnRun:disabled {
            background-color: #1e293b;
            color: #475569;
            border: 1px solid #334155;
        }

        #BtnPause {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #d97706, stop:1 #f59e0b);
            border: 1px solid #fbbf24;
            border-radius: 8px;
            color: white;
            font-weight: 700;
            font-size: 10pt;
            min-height: 36px;
            max-height: 38px;
        }
        #BtnPause:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f59e0b, stop:1 #fbbf24);
        }
        #BtnPause:disabled {
            background-color: #1e293b;
            color: #475569;
            border: 1px solid #334155;
        }

        #BtnStop {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #dc2626, stop:1 #f43f5e);
            border: 1px solid #f87171;
            border-radius: 8px;
            color: white;
            font-weight: 700;
            font-size: 10pt;
            min-height: 36px;
            max-height: 38px;
        }
        #BtnStop:hover {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ef4444, stop:1 #fb7185);
        }
        #BtnStop:disabled {
            background-color: #1e293b;
            color: #475569;
            border: 1px solid #334155;
        }

        #KpiCard {
            background-color: #0f172a;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
        }
        #KpiTitle {
            color: #94a3b8;
            font-size: 8.5pt;
            font-weight: 700;
            letter-spacing: 0.5px;
        }

        #CustomProgressBar {
            background-color: #0f172a;
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 8px;
            height: 16px;
        }
        #CustomProgressBar::chunk {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:0.5 #06b6d4, stop:1 #10b981);
            border-radius: 8px;
        }

        #LogFilterCombo, #LogSearchInput {
            background-color: #0b0f17;
            color: #f8fafc;
            border: 1px solid #334155;
            border-radius: 6px;
            padding: 2px 8px;
            font-size: 9.5pt;
            min-height: 28px;
        }

        #ProfileCombo {
            background-color: #0b0f17;
            color: #f8fafc;
            border: 1px solid #334155;
            border-radius: 5px;
            padding: 0 6px 0 8px;
            font-size: 9.5pt;
            min-height: 28px;
            max-height: 28px;
            height: 28px;
        }

        #ConsoleMonitor {
            background-color: #050811;
            color: #e2e8f0;
            font-family: 'Consolas', 'Fira Code', 'Courier New', monospace;
            font-size: 10pt;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            padding: 10px;
        }

        QScrollBar:vertical {
            background: #0b0f17;
            width: 8px;
            border-radius: 4px;
        }
        QScrollBar::handle:vertical {
            background: #334155;
            min-height: 20px;
            border-radius: 4px;
        }
        QScrollBar::handle:vertical:hover {
            background: #475569;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
        """
        self.setStyleSheet(qss)


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
def main():
    app = QApplication(sys.argv)
    from PyQt6.QtGui import QFont
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindowV2()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
