import os
from src.srt_manager import clean_gemini_output
import shutil
import time
import re
from src.gemini_bot import (
    force_kill_chrome,
        countdown_sleep,
    resolve_profile_path,
    get_available_profiles,
    record_profile_cooldown,
    is_profile_in_cooldown,
    get_next_available_pro_profile
)

from src.srt_manager import replace_blocks_in_folder
from playwright.sync_api import sync_playwright

def run_auto_qa_repair(prompt_file, report_folder, original_srt_folder, fixed_srt_folder, profile_folder="chrome_data_1", wait_time=300, delay_time=15, log_callback=print, progress_callback=None, check_pause_callback=None):
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

    os.makedirs(report_folder, exist_ok=True)
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

    from src.gemini_bot import GeminiBot, is_profile_in_cooldown, get_next_available_pro_profile, record_profile_cooldown

    # --- BLOCKER: Chờ 60 phút ở vòng ngoài nếu TẤT CẢ profile đều bị khóa ---
    while True:
        in_cd, rem_str, _ = is_profile_in_cooldown(current_profile)
        if in_cd:
            next_p = get_next_available_pro_profile(current_profile, available_profiles, log_callback)
            if next_p:
                log_callback(f"⏩ [CẢNH BÁO] Profile [{current_profile}] đang chờ (Còn {rem_str}). Đã chuyển sang: [{next_p}]", "warning")
                current_profile = next_p
                current_profile_idx = available_profiles.index(next_p)
                break
            else:
                log_callback("🛑 CẢNH BÁO: TOÀN BỘ Profile đều bị phạt 5 tiếng! Ngủ đông 60 phút chờ hồi phục (không tốn RAM)...")
                countdown_sleep(3600, log_callback, "⏳ Đang ngủ đông:")
        else:
            break
    # -----------------------------------------------------------------------

    with sync_playwright() as p:
        bot = GeminiBot(log_callback=log_callback)
        browser, page, model_status = bot.launch(p, current_profile, prompt_file, check_pause_callback)
        
        while model_status == "FLASH_LITE":
            record_profile_cooldown(current_profile, 5.0, log_callback)
            next_p = get_next_available_pro_profile(current_profile, available_profiles, log_callback)
            if next_p:
                log_callback(f"🔄 Profile ban đầu [{current_profile}] bị hết ngạch 5h. TỰ ĐỘNG BỎ QUA & CHUYỂN SANG: [{next_p}]...", "info")
                try: browser.close()
                except Exception: pass
                current_profile = next_p
                current_profile_idx = available_profiles.index(next_p)
                browser, page, model_status = bot.launch(p, current_profile, prompt_file, check_pause_callback)
                if model_status != "FLASH_LITE":
                    log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG DÙNG MÔ HÌNH PRO! 🚀", "success")
                    break
            else:
                log_callback("🛑 TẤT CẢ CÁC PROFILE CHROME ĐỀU ĐÃ HẾT HẠN MỨC PRO! Tạm dừng 60 phút...")
                countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:")
                try:
                    page.goto("https://gemini.google.com/app", timeout=60000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                except Exception:
                    pass
                status, _ = bot.check_model_status()
                model_status = status
                if model_status != "FLASH_LITE":
                    bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                    break

        files_processed_in_session = 0
        BATCH_SIZE = 3

        for file_name in report_files:
            if check_pause_callback:
                check_pause_callback()
            report_path = os.path.join(report_folder, file_name)
            
            status, model_name = bot.check_model_status()
            while status == "FLASH_LITE":
                log_callback(f"⚠️ Profile [{current_profile}] bị hạ cấp xuống Flash-Lite! Tự động chuyển Profile còn hạn mức Pro...", "warning")
                record_profile_cooldown(current_profile, 5.0, log_callback)
                
                next_p = get_next_available_pro_profile(current_profile, available_profiles, log_callback)
                switched_ok = False
                if next_p:
                    log_callback(f"🔄 TỰ ĐỘNG BỎ QUA PROFILE KHÓA & CHUYỂN SANG: [{next_p}]...", "info")
                    try: browser.close()
                    except Exception: pass
                    current_profile = next_p
                    current_profile_idx = available_profiles.index(next_p)
                    browser, page, new_status = bot.launch(p, current_profile, prompt_file, check_pause_callback)
                    if new_status != "FLASH_LITE":
                        switched_ok = True
                        status = new_status
                        log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG KHÔI PHỤC MODEL PRO! 🚀", "success")

                if not switched_ok:
                    log_callback("🛑 TẤT CẢ CÁC PROFILE CHROME ĐỀU ĐÃ BỊ KHÓA HẠN MỨC PRO (5 TIẾNG)! Tạm dừng 60 phút...")
                    countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:")
                    page.goto("https://gemini.google.com/app", timeout=60000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    new_status, _ = bot.check_model_status()
                    status = new_status
                    if status != "FLASH_LITE":
                        bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                        break

            if files_processed_in_session >= BATCH_SIZE:
                log_callback(f"\n🔄 Đã hoàn thành 1 mẻ ({BATCH_SIZE} báo cáo). Đang làm mới phiên chat...")
                try:
                    page.goto("https://gemini.google.com/app", timeout=60000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                    files_processed_in_session = 0
                except Exception as e:
                    log_callback(f"⚠️ Cảnh báo khi làm mới: {e}")

            log_callback(f"\n--- Đang gửi báo cáo lỗi: {file_name} ---")

            short_prompt = (
                "Dưới đây là báo cáo lỗi SRT. Hãy đọc và trả về TẤT CẢ các block đã sửa nằm trong DUY NHẤT 1 CODE BLOCK ```srt ... ```.\\n"
                "TUYỆT ĐỐI KHÔNG viết lời giải thích và KHÔNG tách thành nhiều code block riêng lẻ. Chỉ trả về duy nhất 1 code block chứa các block đã sửa."
            )
            
            max_upload_retries = 2
            initial_count = None
            for retry_idx in range(max_upload_retries):
                initial_count = bot.upload_file_and_send(report_path, short_prompt)
                if initial_count is not None:
                    break
                log_callback(f"⚠️ Upload thất bại lần {retry_idx + 1} cho {file_name}. Thử F5 tải lại trang...")
                try:
                    page.goto("https://gemini.google.com/app", timeout=60000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                except Exception as e_retry:
                    log_callback(f"⚠️ Lỗi khi tải lại trang: {e_retry}")

            if initial_count is None:
                log_callback(f"❌ Upload thất bại báo cáo {file_name} sau {max_upload_retries} lần thử. Bỏ qua file này...")
                continue

            bot.wait_for_response(initial_count, wait_time, check_pause_callback=check_pause_callback)
            latest_response = bot.get_latest_response()
            
            if latest_response:
                patch_text = clean_gemini_output(latest_response)
                
                log_callback(f"⚖️ Đang áp dụng các sửa đổi vào các file SRT...")
                replaced_count, deleted_count = replace_blocks_in_folder(fixed_srt_folder, patch_text, log_callback)
                
                if replaced_count > 0 or deleted_count > 0:
                    try:
                        repaired_name = os.path.splitext(file_name)[0] + "_done.txt"
                        repaired_path = os.path.join(report_folder, repaired_name)
                        os.rename(report_path, repaired_path)
                        log_callback(f"✅ Đã xử lý xong. Đổi tên file báo cáo trực tiếp thành '{repaired_name}' để lưu tiến trình.")
                        files_processed_in_session += 1
                    except Exception as e:
                        log_callback(f"⚠️ Lỗi khi đổi tên file báo cáo: {e}")
                else:
                    log_callback("⚠️ CẢNH BÁO: Không có block nào được thay thế. Không đánh dấu hoàn thành để thử lại lần sau.")
            else:
                log_callback(f"❌ Không lấy được phản hồi từ Gemini.")

            countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ, còn:")

        log_callback("\n🎉 ĐÃ HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH SỬA LỖI QA!")
        try: browser.close()
        except Exception: pass
