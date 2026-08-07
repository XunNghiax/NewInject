import os
import shutil
import time
import re
from src.gemini_core import (
    force_kill_chrome,
    clean_gemini_output,
    countdown_sleep,
    resolve_profile_path,
    get_available_profiles
)
from src.gemini_translate import (
    send_initial_prompt,
    upload_srt_and_send,
    smart_wait_for_gemini,
    check_model_status,
    create_browser_context
)
from src.batch_replace_srt import replace_blocks_in_folder
from playwright.sync_api import sync_playwright

def run_auto_qa_repair(prompt_file, report_folder, original_srt_folder, fixed_srt_folder, profile_folder="chrome_data_1", wait_time=300, delay_time=15, log_callback=print):
    """
    Tự động hóa gửi báo cáo lỗi cho LLM, nhận patch và sửa trên bản sao SRT.
    - BẢO VỆ TIẾN TRÌNH CŨ (.DONE)
    - KIỂM TRA MÔ HÌNH PRO TRƯỚC KHI GỬI PROMPT VÁ LỖI
    - XOAY VÒNG PROFILE CHROME TỰ ĐỘNG KHI HẾT HẠN MỨC PRO
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
    report_files = [
        f for f in os.listdir(report_folder) 
        if f.lower().endswith('.txt') 
        and not f.lower().endswith('_da_sua.txt') 
        and not f.lower().endswith('.done')
        and ('report' in f.lower() or 'qa' in f.lower())
    ]
    
    def sort_by_number(filename):
        numbers = re.findall(r'\d+', filename)
        return int(numbers[-1]) if numbers else 0
        
    report_files.sort(key=sort_by_number)

    if not report_files:
        log_callback("✅ Tuyệt vời! Không tìm thấy file báo cáo .txt nào cần xử lý.")
        return

    force_kill_chrome(log_callback)
    log_callback(f"🚀 Khởi động trình duyệt Playwright (Còn {len(report_files)} báo cáo cần xử lý)...")

    # 3. QUẢN LÝ DÂN SÁCH PROFILE CHROME VÀ KIỂM TRA MÔ HÌNH PRO
    available_profiles = get_available_profiles()
    if profile_folder and profile_folder in available_profiles:
        current_profile_idx = available_profiles.index(profile_folder)
    elif profile_folder and profile_folder not in available_profiles:
        available_profiles.insert(0, profile_folder)
        current_profile_idx = 0
    else:
        current_profile_idx = 0

    current_profile = available_profiles[current_profile_idx]
    log_callback(f"📋 Tìm thấy {len(available_profiles)} Profile Chrome: {', '.join(available_profiles)} (Bắt đầu vá lỗi với [{current_profile}])")

    with sync_playwright() as p:
        browser, page, model_status = create_browser_context(p, current_profile, prompt_file, log_callback)
        
        # Nếu Profile ban đầu bị hạ cấp xuống Flash-Lite -> Tự động chuyển Profile tiếp theo
        if model_status == "FLASH_LITE":
            start_idx = current_profile_idx
            switched_ok = False
            while True:
                current_profile_idx = (current_profile_idx + 1) % len(available_profiles)
                if current_profile_idx == start_idx:
                    break
                next_profile = available_profiles[current_profile_idx]
                log_callback(f"🔄 Profile ban đầu [{current_profile}] bị hạ cấp Flash-Lite. TỰ ĐỘNG CHUYỂN SANG: [{next_profile}]...", "info")
                try:
                    browser.close()
                except Exception:
                    pass
                current_profile = next_profile
                browser, page, model_status = create_browser_context(p, current_profile, prompt_file, log_callback)
                if model_status != "FLASH_LITE":
                    switched_ok = True
                    log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG DÙNG MÔ HÌNH PRO! 🚀", "success")
                    break

            if not switched_ok:
                log_callback("🛑 TẤT CẢ CÁC PROFILE CHROME ĐỀU ĐÃ HẾT HẠN MỨC PRO! Tạm dừng 60 phút...")
                countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:")
                page.goto("https://gemini.google.com/app", timeout=60000)
                page.wait_for_load_state("load")
                time.sleep(5)
                send_initial_prompt(page, prompt_file, log_callback)

        # 4. TIẾN HÀNH XỬ LÝ NẠP BÁO CÁO VÀ VÁ LỖI
        files_processed_in_session = 0
        BATCH_SIZE = 3

        for file_name in report_files:
            report_path = os.path.join(report_folder, file_name)
            
            # Kiểm tra hạn mức Pro trước khi gửi báo cáo
            status, model_name = check_model_status(page, log_callback)
            if status == "FLASH_LITE":
                log_callback(f"⚠️ Profile [{current_profile}] bị hạ cấp xuống Flash-Lite! Tự động chuyển Profile còn hạn mức Pro...", "warning")
                switched_ok = False
                start_idx = current_profile_idx
                while True:
                    current_profile_idx = (current_profile_idx + 1) % len(available_profiles)
                    if current_profile_idx == start_idx:
                        break
                    next_profile = available_profiles[current_profile_idx]
                    log_callback(f"🔄 TỰ ĐỘNG CHUYỂN SANG PROFILE TIẾP THEO: [{next_profile}]...", "info")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    current_profile = next_profile
                    browser, page, new_status = create_browser_context(p, current_profile, prompt_file, log_callback)
                    if new_status != "FLASH_LITE":
                        switched_ok = True
                        log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG KHÔI PHỤC MODEL PRO! 🚀", "success")
                        break

                if not switched_ok:
                    log_callback("🛑 TẤT CẢ CÁC PROFILE CHROME ĐỀU ĐÃ HẾT HẠN MỨC PRO! Tạm dừng 60 phút...")
                    countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:")
                    page.goto("https://gemini.google.com/app", timeout=60000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    send_initial_prompt(page, prompt_file, log_callback)

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
                    repaired_name = os.path.splitext(file_name)[0] + "_da_sua.txt"
                    repaired_path = os.path.join(report_folder, repaired_name)
                    os.rename(report_path, repaired_path)
                    log_callback(f"✅ Đã xử lý xong. Đổi tên file báo cáo trực tiếp thành '{repaired_name}' để lưu tiến trình.")
                    files_processed_in_session += 1
                except Exception as e:
                    log_callback(f"⚠️ Lỗi khi đổi tên file báo cáo: {e}")
            else:
                log_callback(f"❌ Không lấy được phản hồi từ Gemini.")

            countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ, còn:")

        log_callback("\n🎉 ĐÃ HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH SỬA LỖI QA!")
        browser.close()