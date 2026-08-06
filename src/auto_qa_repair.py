import os
import shutil
import time
import re
from src.gemini_core import (
    force_kill_chrome,
    clean_gemini_output,
    countdown_sleep,
    resolve_profile_path
)
from src.gemini_translate import send_initial_prompt, upload_srt_and_send, smart_wait_for_gemini, check_model_status
from src.batch_replace_srt import replace_blocks_in_folder
from playwright.sync_api import sync_playwright

def run_auto_qa_repair(prompt_file, report_folder, original_srt_folder, fixed_srt_folder, profile_folder="chrome_data_1", wait_time=60, delay_time=15, log_callback=print):
    """
    Tự động hóa gửi báo cáo lỗi cho LLM, nhận patch và sửa trên bản sao SRT.
    - BẢO VỆ TIẾN TRÌNH CŨ
    - LƯU TRẠNG THÁI (.DONE)
    - CHỌN PROFILE ĐỘNG
    - CHỜ ĐĂNG NHẬP GOOGLE
    """
    
    # 1. BẢO VỆ TIẾN TRÌNH CŨ
    os.makedirs(fixed_srt_folder, exist_ok=True)
    existing_srts = [f for f in os.listdir(fixed_srt_folder) if f.lower().endswith('.srt')]
    
    if not existing_srts:
        log_callback(f"📁 Khởi tạo lần đầu: Đang sao chép file gốc sang thư mục đầu ra: {fixed_srt_folder}...")
        for f_name in os.listdir(original_srt_folder):
            if f_name.lower().endswith('.srt'):
                src = os.path.join(original_srt_folder, f_name)
                dst = os.path.join(fixed_srt_folder, f_name)
                shutil.copy2(src, dst)
    else:
        log_callback(f"♻️ Phát hiện tiến trình cũ: Đang tiếp tục làm việc trên các file trong {fixed_srt_folder}...")

    # 2. LẤY DANH SÁCH BÁO CÁO 
    report_files = [f for f in os.listdir(report_folder) if f.lower().endswith('.txt')]
    
    def sort_by_number(filename):
        numbers = re.findall(r'\d+', filename)
        return int(numbers[-1]) if numbers else 0
        
    report_files.sort(key=sort_by_number)

    if not report_files:
        log_callback("✅ Tuyệt vời! Không tìm thấy file báo cáo .txt nào cần xử lý.")
        return

    force_kill_chrome(log_callback)
    log_callback(f"🚀 Khởi động trình duyệt Playwright (Còn {len(report_files)} báo cáo cần xử lý)...")

    # 3. KHỞI CHẠY PLAYWRIGHT VÀ CHỜ ĐĂNG NHẬP
    with sync_playwright() as p:
        user_data_dir = resolve_profile_path(profile_folder)
        log_callback(f"🌐 Đang sử dụng Chrome Profile tại: {user_data_dir}")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )

        page = browser.new_page()
        page.goto("https://gemini.google.com/app", timeout=60000)
        page.wait_for_load_state("load")
        
        # --- VÒNG LẶP CHỜ ĐĂNG NHẬP ---
        log_callback(f"⏳ Đang kiểm tra trạng thái đăng nhập của {profile_folder}...")
        while True:
            current_url = page.url
            if "accounts.google.com" in current_url or "signin" in current_url:
                log_callback("⚠️ BẠN CHƯA ĐĂNG NHẬP! Vui lòng đăng nhập Google trên trình duyệt Chrome vừa mở. Tool đang tạm dừng để chờ bạn...")
                try:
                    page.wait_for_url("**/gemini.google.com/app**", timeout=300000) 
                except:
                    pass
            
            try:
                page.locator("rich-textarea, div[role='textbox']").first.wait_for(state="visible", timeout=5000)
                log_callback("✅ Giao diện Gemini đã sẵn sàng!")
                break
            except Exception:
                log_callback("⏳ Đang chờ giao diện Gemini tải xong (Hoặc chờ bạn tắt các bảng thông báo chào mừng)...")
                time.sleep(3)
        # ------------------------------

        # Gửi luật sửa lỗi 
        send_initial_prompt(page, prompt_file, log_callback)

        # 4. TIẾN HÀNH XỬ LÝ THEO BATCH
        files_processed_in_session = 0
        BATCH_SIZE = 3

        for file_name in report_files:
            report_path = os.path.join(report_folder, file_name)
            
            if files_processed_in_session >= BATCH_SIZE:
                log_callback(f"\n🔄 Đã hoàn thành 1 mẻ ({BATCH_SIZE} báo cáo). Đang làm mới phiên chat...")
                try:
                    page.goto("https://gemini.google.com/app", timeout=60000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    send_initial_prompt(page, prompt_file, log_callback)
                    files_processed_in_session = 0
                except Exception as e:
                    log_callback(f"⚠️ Cảnh báo khi làm mới: {e}")

            log_callback(f"\n--- Đang gửi báo cáo lỗi: {file_name} ---")

            short_prompt = (
                "Dưới đây là báo cáo lỗi SRT. Hãy đọc và trả về các block đã sửa nội dung hoặc timestamp "
                "theo đúng chuẩn định dạng Repair Engine (chỉ trả về block, ghi [MERGED: x, y] nếu cần gộp)."
            )
            initial_count = upload_srt_and_send(page, report_path, short_prompt, log_callback)

            if initial_count is None:
                log_callback(f"❌ Upload thất bại báo cáo {file_name}. F5 tải lại trang và bỏ qua file này...")
                page.goto("https://gemini.google.com/app", timeout=60000)
                time.sleep(5)
                send_initial_prompt(page, prompt_file, log_callback)
                continue

            success = smart_wait_for_gemini(page, initial_count, wait_time, log_callback)
            responses = page.locator('.model-response-text').all_inner_texts()
            
            if responses:
                latest_response = responses[-1]
                patch_text = clean_gemini_output(latest_response)
                
                log_callback(f"⚖️ Đang áp dụng các sửa đổi vào các file SRT...")
                replace_blocks_in_folder(fixed_srt_folder, patch_text, log_callback)
                
                try:
                    done_path = report_path + ".done"
                    os.rename(report_path, done_path)
                    log_callback(f"✅ Đã xử lý xong. Đổi tên thành '{file_name}.done' để lưu tiến trình.")
                    files_processed_in_session += 1
                except Exception as e:
                    log_callback(f"⚠️ Lỗi khi đổi tên file báo cáo: {e}")
            else:
                log_callback(f"❌ Không lấy được phản hồi từ Gemini.")

            countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ, còn:")

        log_callback("\n🎉 ĐÃ HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH SỬA LỖI QA!")
        browser.close()