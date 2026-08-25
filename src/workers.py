import os
import time
import requests
import shutil

from PyQt6.QtCore import QThread, pyqtSignal, QMutex, QWaitCondition

# Add src to path
import sys
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.append(src_dir)

try:
    from bilibili_downloader import BilibiliDownloader
except ImportError:
    BilibiliDownloader = None

try:
    from subtitle_generator import SubtitleGenerator
except ImportError:
    SubtitleGenerator = None

try:
    from workflow_translate import run_auto_translate_srt
except ImportError:
    run_auto_translate_srt = None

try:
    from srt_manager import analyze_srt_to_file
except ImportError:
    analyze_srt_to_file = None

try:
    from workflow_qa import run_auto_qa_repair
except ImportError:
    run_auto_qa_repair = None

try:
    from srt_manager import process_and_renumber_srt, merge_numbered_srt_files, process_srt_speed, split_srt_file
except ImportError:
    process_and_renumber_srt = None
    merge_numbered_srt_files = None
    process_srt_speed = None
    split_srt_file = None

try:
    from gemini_bot import get_available_profiles, create_new_profile, open_chrome_for_login, is_profile_logged_in
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
    badge_signal = pyqtSignal(str, str)       # Tín hiệu cập nhật UI System Badge (text, màu nền)

    def __init__(self, link: str, output_dir: str = "./downloads", auto_gen_srt: bool = False, auto_translate_srt: bool = False, local_media_path: str = None, srt_translate_path: str = None, qa_scan_path: str = None, qa_repair_mode: bool = False, profile_folder: str = "chrome_data_1", auto_inject_capcut: bool = False, capcut_draft_path: str = "", enable_tts: bool = True, gradio_url: str = "", ref_audio_path: str = "", ref_text: str = "", fast_forward_mode: bool = False, workflow_mode: str = "video"):
        super().__init__()
        self.workflow_mode = workflow_mode
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
                except Exception as e:
                    self.emit_log(f"⚠️ Cảnh báo: Không thể xóa file output.srt cũ. Vui lòng đóng các app đang mở file này để tránh lỗi hiển thị! ({e})", "warning")

            # ── BƯỚC 1: DOWNLOAD / NẠP NGUỒN MEDIA ──
            self.badge_signal.emit("📥 ĐANG TẢI NGUỒN", "#0891b2")
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
                
            else:
    
                # ── BƯỚC 2: SPEECH-TO-TEXT (WHISPER STT) ──
                self.badge_signal.emit("📝 ĐANG BÓC BĂNG", "#4f46e5")
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
                        self.badge_signal.emit("✂️ ĐANG CẮT FILE", "#6366f1")
                        prefix_path = os.path.join(cn_folder, "part")
                        split_srt_file(srt_08, output_prefix=prefix_path, blocks_per_file=100, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))
                else:
                    self.emit_log(f"✅ Đã tìm thấy thư mục phụ đề gốc: {cn_folder}", "info")
    
                # ── BƯỚC 3: DỊCH THUẬT AI & SO KHỚP TIMECODE 100% ──
                self.badge_signal.emit("🤖 AI ĐANG DỊCH", "#2563eb")
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
                                expected_vi = f"{os.path.splitext(cn_file)[0]}_vi.srt"
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
                self.badge_signal.emit("🕵️ ĐANG VÁ LỖI (QA)", "#ea580c")
                self.emit_progress(0, "Chuẩn bị Auto QA...")
                
                self.global_progress_signal.emit(65, "4. Đang kiểm tra QA & tự động sửa lỗi trực tiếp trên từng part...")
                report_folder = os.path.join(root_downloads, f"temp_split_qa_reports_{raw_title}")
                fixed_vi_folder = os.path.join(root_downloads, f"temp_split_vi_fixed_{raw_title}")
                
                # CẬP NHẬT: Kiểm tra xem tiến trình cũ có tồn tại không
                is_resume = False
                if os.path.exists(fixed_vi_folder) and any(f.endswith('.srt') for f in os.listdir(fixed_vi_folder)):
                    is_resume = True
                    self.emit_log("♻️ Phát hiện thư mục fixed đã tồn tại. Ưu tiên quét và xử lý lỗi tiếp trên thư mục này...", "info")
                
                os.makedirs(report_folder, exist_ok=True)
                os.makedirs(fixed_vi_folder, exist_ok=True)
    
                # Khởi tạo bản sao các part sang fixed_vi_folder
                for fn in os.listdir(vi_folder):
                    if fn.endswith('.srt'):
                        src_p = os.path.join(vi_folder, fn)
                        dst_p = os.path.join(fixed_vi_folder, fn)
                        if not os.path.exists(dst_p):
                            shutil.copy2(src_p, dst_p)
    
                # Vòng lặp Iterative Healing
                max_qa_passes = 5
                qa_scan_mode = 'semantic' if self.workflow_mode == "audio_only" else 'all'
                for pass_idx in range(max_qa_passes):
                    self.emit_log(f"🔄 Đang chạy vòng lặp QA lần {pass_idx + 1}/{max_qa_passes}...")
                    
                    # LOGIC ƯU TIÊN QUÉT: Nếu có resume thì quét luôn fixed_vi_folder ngay từ Pass 1
                    if pass_idx == 0:
                        analyze_src = fixed_vi_folder if is_resume else vi_folder
                    else:
                        analyze_src = fixed_vi_folder
                    
                    if analyze_srt_to_file:
                        analyze_srt_to_file(analyze_src, report_folder, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), scan_mode=qa_scan_mode, check_pause_callback=self.check_pause)
        
                    report_files = [f for f in os.listdir(report_folder) if f.endswith('.txt') and not f.endswith('_da_sua.txt') and not f.endswith('_done.txt') and not f.endswith('.done') and ('report' in f.lower() or 'qa' in f.lower())]
        
                    if not report_files:
                        self.emit_log("✅ Phụ đề tiếng Việt đạt chuẩn chất lượng 100%, không phát hiện lỗi QA nghiêm trọng cần sửa!", "success")
                        break
        
                    if run_auto_qa_repair:
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
        
                        if analyze_srt_to_file:
                            re_err, re_crit, re_warn = analyze_srt_to_file(fixed_vi_folder, report_folder, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), scan_mode=qa_scan_mode, check_pause_callback=self.check_pause)
                            if re_crit == 0:
                                self.emit_log(f"✅ RE-SCAN HOÀN HẢO: 0 Lỗi nghiêm trọng (Critical) sau khi sửa! (Còn {re_warn} cảnh báo nhỏ)", "success")
                                break
                            else:
                                self.emit_log(f"⚠️ RE-SCAN: Còn lại {re_crit} lỗi nghiêm trọng và {re_warn} cảnh báo.", "warning")
                                if pass_idx < max_qa_passes - 1:
                                    self.emit_log("🔄 Đang chuyển sang vòng lặp sửa lỗi tiếp theo...", "info")
    
                # ── CỔNG 3: XUẤT DUY NHẤT 1 FILE output.srt THÀNH PHẨM HOÀN HẢO ──
                if merge_numbered_srt_files and os.path.exists(fixed_vi_folder) and os.listdir(fixed_vi_folder):
                    self.emit_log("🧩 CỔNG 3: Hoàn tất kiểm duyệt QA! Tiến hành xuất DUY NHẤT file thành phẩm: output.srt...", "info")
                    merge_numbered_srt_files(fixed_vi_folder, final_srt_path, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl))
                    self.emit_log(f"🎉 ĐÃ XUẤT FILE THÀNH PHẨM DUY NHẤT: {final_srt_path} (Sẵn sàng 100% để sinh Audio TTS / Nhúng CapCut)", "success")
                else:
                    self.finished_signal.emit(False, "❌ Thất bại khi tạo file output.srt cuối cùng!")
                    return

            # ── BƯỚC 5: SINH AUDIO (TTS GRADIO) VỚI ĐIỂM DỪNG THÔNG MINH (JUST-IN-TIME) ──
            self.badge_signal.emit("🎙️ ĐANG TẠO GIỌNG", "#d946ef")
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

            # ── BƯỚC 6: CAPCUT DRAFT INJECT HOẶC SINH AUDIO ĐỘC LẬP ──
            if self.workflow_mode == "audio_only":
                self.badge_signal.emit("🎵 ĐANG TẠO AUDIO", "#16a34a")
            elif self.workflow_mode == "hybrid_video":
                self.badge_signal.emit("🧪 ĐANG CHẠY HYBRID", "#16a34a")
            else:
                self.badge_signal.emit("💉 ĐANG NHÚNG CAPCUT", "#16a34a")
            self.step_signal.emit(6)
            
            if self.workflow_mode == "audio_only":
                self.global_progress_signal.emit(92, "6. Đang tạo file Audio Tự nhiên Độc lập (Audio Only)...")
                self.emit_log("🎙️ Bắt đầu sinh Audio Tự nhiên (Bỏ qua CapCut)...", "info")
                if CapCutBackend:
                    try:
                        cfg = {
                            "SRT_FILE_PATH": final_srt_path,
                            "AUDIO_OUT_DIR": os.path.join(root_downloads, f"voice_{raw_title}_Natural"),
                            "SERVER_URL": self.gradio_url,
                            "REF_AUDIO_PATH": self.ref_audio_path,
                            "REF_TEXT": self.ref_text,
                            "SPEED_RATIO": 1.0,
                            "CREATE_NATURAL_AUDIO_ONLY": True,
                            "EXPERIMENTAL_HYBRID_MODE": False
                        }
                        backend = CapCutBackend(cfg, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), progress_callback=lambda d, t, msg: self.emit_progress(int((d/t)*100) if t > 0 else 0, msg), check_pause_callback=self.check_pause)
                        backend.run_process()
                        self.emit_log("🎉 ĐÃ TẠO XONG FILE AUDIO TỰ NHIÊN ĐỘC LẬP!", "success")
                    except Exception as audio_e:
                        self.emit_log(f"⚠️ Thất bại khi tạo Audio tự nhiên: {audio_e}", "warning")
            elif self.auto_inject_capcut and self.capcut_draft_path and os.path.exists(self.capcut_draft_path):
                self.global_progress_signal.emit(92, "6. Đang bơm phụ đề trực tiếp vào dự án CapCut PC...")
                self.emit_log(f"💉 Bơm phụ đề vào CapCut Draft: {self.capcut_draft_path}...", "info")
                
                if self.capcut_draft_path.lower().endswith(".json"):
                    draft_json = self.capcut_draft_path
                else:
                    draft_json = os.path.join(self.capcut_draft_path, "draft_content.json")
                    
                if CapCutBackend:
                    try:
                        cfg = {
                            "SRT_FILE_PATH": final_srt_path,
                            "CAPCUT_JSON_PATH": draft_json,
                            "AUDIO_OUT_DIR": os.path.join(root_downloads, f"voice_{raw_title}"),
                            "SERVER_URL": self.gradio_url,
                            "REF_AUDIO_PATH": self.ref_audio_path,
                            "REF_TEXT": self.ref_text,
                            "SPEED_RATIO": 1.25,
                            "CREATE_NATURAL_AUDIO_ONLY": False,
                            "EXPERIMENTAL_HYBRID_MODE": (self.workflow_mode == "hybrid_video")
                        }
                        backend = CapCutBackend(cfg, log_callback=lambda msg, lvl="info": self.emit_log(msg, lvl), progress_callback=lambda d, t, msg: self.emit_progress(int((d/t)*100) if t > 0 else 0, msg), check_pause_callback=self.check_pause)
                        backend.ensure_capcut_closed()
                        backend.run_process(only_inject=not self.enable_tts)
                        if self.workflow_mode == "hybrid_video":
                            self.emit_log("🎉 ĐÃ HOÀN TẤT NHÚNG CAPCUT (CHẾ ĐỘ HYBRID THỬ NGHIỆM)!", "success")
                        else:
                            self.emit_log("🎉 ĐÃ NHÚNG PHỤ ĐỀ TRỰC TIẾP VÀO CAPCUT DRAFT THÀNH CÔNG!", "success")
                    except Exception as inject_e:
                        self.emit_log(f"⚠️ Thất bại khi nhúng vào CapCut: {inject_e}", "warning")
                else:
                    if not CapCutBackend:
                        self.emit_log("❌ BỎ QUA NHÚNG CAPCUT: Module backend bị lỗi Import (Thiếu thư viện pysrt, pydub, psutil hoặc file backend.py bị lỗi).", "warning")

            self.global_progress_signal.emit(100, "HOÀN TẤT DỰ ÁN! 🎉")
            self.finished_signal.emit(True, f"🎉 ĐÃ HOÀN TẤT TOÀN BỘ DỰ ÁN TỰ ĐỘNG!\n📁 Phụ đề lưu tại: {final_srt_path}")

        except Exception as e:
            self.emit_log(f"❌ Lỗi tiến trình tự động: {e}", "error")
            self.finished_signal.emit(False, str(e))


