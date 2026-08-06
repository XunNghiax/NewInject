import os
import time
import subprocess
import platform
import re
import shutil 
from playwright.sync_api import sync_playwright

# ============================================================
# CÁC HÀM TIỆN ÍCH VÀ XỬ LÝ CHUỖI
# ============================================================
def clean_gemini_output(text):
    lines = text.strip().split('\n')

    while lines:
        first_line = lines[0].strip().lower()
        if first_line.startswith('```') or first_line.startswith('đoạn mã') or first_line == '':
            lines.pop(0)
        else:
            break  
        
    while lines:
        last_line = lines[-1].strip()
        if last_line.startswith('```') or last_line == '':
            lines.pop()
        else:
            break
            
    return '\n'.join(lines).strip()

def force_kill_chrome(log_callback):
    """Dọn dẹp triệt để các tiến trình Chrome chạy ngầm trước khi mở Playwright"""
    try:
        log_callback("🧹 Đang dọn dẹp các tiến trình Chrome chạy ngầm...")
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["pkill", "-f", "Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
    except Exception as e:
        log_callback(f"⚠️ Cảnh báo dọn dẹp Chrome: {e}")

def countdown_sleep(seconds, log_callback, message_prefix="⏳ Còn khoảng"):
    """Hàm đếm ngược thời gian nghỉ giữa các file"""
    for remaining in range(seconds, 0, -5):
        if remaining > 5:
            log_callback(f"   {message_prefix} {remaining} giây...")
            time.sleep(5)
        else:
            log_callback(f"   {message_prefix} {remaining} giây...")
            time.sleep(remaining)

# ============================================================
# [THÊM MỚI] LOGIC KIỂM TRA CẤU TRÚC SRT
# ============================================================
def parse_srt_structure(filepath):
    """Đọc file SRT và trích xuất cấu trúc Block ID và Timeline"""
    structure = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().replace('\r\n', '\n').strip()
        
        blocks = re.split(r'\n\s*\n', content)
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 2:
                block_id = lines[0].strip()
                timeline = lines[1].strip()
                if "-->" in timeline:
                    structure[block_id] = timeline
    except Exception:
        pass
    return structure

def is_srt_structure_match(file_a, file_b, log_callback):
    """So sánh cấu trúc 2 file SRT. Trả về True nếu khớp, False nếu sai lệch."""
    struct_a = parse_srt_structure(file_a)
    struct_b = parse_srt_structure(file_b)
    
    keys_a = sorted(struct_a.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    keys_b = sorted(struct_b.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    
    if keys_a != keys_b:
        log_callback(f"   ❌ Lỗi: Số lượng hoặc thứ tự Block ID không khớp ({len(keys_a)} vs {len(keys_b)}).")
        return False
        
    for key in keys_a:
        if struct_a[key] != struct_b[key]:
            log_callback(f"   ❌ Lỗi tại Block {key}: Timeline bị thay đổi.")
            log_callback(f"      Gốc: {struct_a[key]}")
            log_callback(f"      Dịch: {struct_b[key]}")
            return False
            
    return True

# ============================================================
# TƯƠNG TÁC VỚI GIAO DIỆN GEMINI
# ============================================================
def smart_wait_for_gemini(page, initial_count, max_wait_time, log_callback):
    """Liên tục giám sát text, nếu Gemini ngừng gõ trong 6 giây thì tính là xong"""
    log_callback(f"👀 Đang giám sát Gemini gõ chữ (Tối đa {max_wait_time}s)...")
    previous_text = ""
    stable_count = 0
    check_interval = 2
    max_loops = int(max_wait_time / check_interval)

    for i in range(max_loops):
        try:
            current_count = page.locator('.model-response-text').count()
            if current_count <= initial_count:
                time.sleep(check_interval)
                continue

            responses = page.locator('.model-response-text').all_inner_texts()
            if responses:
                current_text = responses[-1]
                if current_text and current_text == previous_text:
                    stable_count += 1
                    if stable_count >= 3:
                        log_callback(f"✨ Đã phát hiện Gemini gõ xong (Mất khoảng {i * check_interval}s)!")
                        return True
                else:
                    stable_count = 0
                    previous_text = current_text
        except Exception:
            pass
        time.sleep(check_interval)

    log_callback("⚠️ Đã đạt giới hạn thời gian chờ tối đa nhưng web có vẻ lag.")
    return False

def check_model_status(page, log_callback):
    """Quét giao diện để kiểm tra xem tài khoản có bị ép xuống 3.1 Flash-Lite không"""
    log_callback("🔍 Đang kiểm tra phiên bản mô hình trước khi dịch...")
    try:
        time.sleep(2)
        buttons = page.locator('button').all()
        for btn in buttons:
            if btn.is_visible():
                text = btn.inner_text().strip().lower()
                if "flash-lite" in text:
                    log_callback(f"❌ PHÁT HIỆN HẠ CẤP: Giao diện đang bị ép về '{text}'")
                    return "FLASH_LITE"
                elif "3.1 pro" in text or "3.5 flash" in text or "tư duy mở rộng" in text:
                    log_callback(f"✅ Hệ thống đang chạy mô hình ổn định: '{text}'")
                    return "OK"
        
        page_text = page.locator('body').inner_text().lower()
        if "3.1 flash-lite" in page_text:
             log_callback("❌ PHÁT HIỆN HẠ CẤP: Tìm thấy chữ 3.1 Flash-Lite trên trang.")
             return "FLASH_LITE"
             
        log_callback("ℹ️ Không nhìn thấy rõ tên mô hình. Mặc định tiếp tục chạy.")
        return "UNKNOWN"
    except Exception as e:
        log_callback(f"⚠️ Lỗi khi quét kiểm tra mô hình: {e}")
        return "UNKNOWN"

def send_initial_prompt(page, prompt_file, log_callback):
    """Đọc file luật dịch và nạp cho Gemini để chuẩn bị làm việc"""
    if prompt_file and os.path.exists(prompt_file):
        log_callback(f"📝 Đang gửi file luật dịch: {os.path.basename(prompt_file)}...")
        with open(prompt_file, 'r', encoding='utf-8') as f:
            prompt_content = f.read()

        chat_box = page.locator('div[contenteditable="true"]').first
        chat_box.wait_for(state="visible", timeout=30000)
        chat_box.click()
        time.sleep(1)

        full_prompt = f"Đây là file hướng dẫn dịch thuật của tôi. Hãy đọc kỹ, ghi nhớ phong cách dịch. Chỉ cần trả lời 'Tôi đã hiểu'.\n\nNỘI DUNG HƯỚNG DẪN:\n{prompt_content}"
        initial_count = page.locator('.model-response-text').count()
        chat_box.fill(full_prompt)

        time.sleep(1.5)
        chat_box.press("Enter")

        try:
            chat_box = page.locator('div[contenteditable="true"]').first
            chat_box.focus()
            time.sleep(0.5)
            
            chat_box.press("Enter")
            time.sleep(1.5) 
            
            send_btn_locators = [
                'button[aria-label*="Gửi"]', 'button[aria-label*="gửi"]', 
                'button[aria-label*="Send"]', 'button[aria-label*="send"]',
                'button[mattooltip*="Gửi"]', 'button[mattooltip*="Send"]',
                '[data-testid="send-button"]'
            ]
            
            clicked_send = False
            for sel in send_btn_locators:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=500):
                    btn.click()
                    log_callback("🚀 Đã click trúng nút Gửi (bằng tên gọi).")
                    clicked_send = True
                    break
                    
            if not clicked_send:
                chat_container = page.locator('div').filter(has=chat_box).last
                last_btn = chat_container.locator('button').last
                if last_btn.is_visible(timeout=1000):
                    last_btn.click()
                    log_callback("🚀 Đã click trúng nút Gửi (bằng vị trí cuối cùng).")
                    
        except Exception as e:
            log_callback(f"⚠️ Lỗi ở công đoạn Gửi tin nhắn: {e}")

        smart_wait_for_gemini(page, initial_count, 30, log_callback)
    else:
        log_callback("⚠️ Không tìm thấy file prompt, bỏ qua bước setup luật dịch.")

def upload_srt_and_send(page, cn_file_path, short_prompt, log_callback):
    """Tải file .srt lên Gemini qua File Chooser và gửi."""
    initial_count = page.locator('.model-response-text').count()
    uploaded = False
    
    log_callback(f"🔍 Đang thao tác mở hộp thoại tải file cho {os.path.basename(cn_file_path)}...")
    
    try:
        with page.expect_file_chooser(timeout=10000) as fc_info:
            chat_box = page.locator('div[contenteditable="true"]').first
            chat_box.wait_for(state="visible", timeout=5000)
            
            box = chat_box.bounding_box()
            if not box: return None
                
            buttons = page.locator('button').all()
            target_btn = None
            closest_dist = 9999
            
            for btn in buttons:
                if btn.is_visible():
                    btn_box = btn.bounding_box()
                    if btn_box:
                        chat_center_y = box['y'] + box['height'] / 2
                        btn_center_y = btn_box['y'] + btn_box['height'] / 2
                        
                        if abs(chat_center_y - btn_center_y) < 30:
                            if btn_box['x'] < box['x']:
                                dist = box['x'] - btn_box['x']
                                if dist < closest_dist:
                                    closest_dist = dist
                                    target_btn = btn
                                    
            if target_btn:
                target_btn.click()
                log_callback("📎 Đã click chuẩn xác nút [+]!")
            else:
                page.locator('button:left-of(div[contenteditable="true"])').first.click(timeout=3000)
            
            time.sleep(1.5) 
            menu_items = page.locator('[role="menuitem"], [role="button"], mat-list-item').all()
            
            for item in menu_items:
                if item.is_visible():
                    text = item.inner_text().lower()
                    if "xuống" not in text and "drive" not in text:
                        if any(k in text for k in ["tải", "tệp", "máy tính", "computer", "upload", "file"]):
                            item.click()
                            break
                            
        file_chooser = fc_info.value
        file_chooser.set_files(cn_file_path)
        uploaded = True
        log_callback(f"✅ Đã nạp file thành công: {os.path.basename(cn_file_path)}")
        
    except Exception as e:
        log_callback(f"❌ Lỗi mở hộp thoại: {e}")
        
    if uploaded:
        time.sleep(3.5) 
        try:
            chat_box = page.locator('div[contenteditable="true"]').first
            chat_box.click()
            chat_box.fill(short_prompt)
            time.sleep(1)
            
            chat_box.focus()
            time.sleep(1) 
            
            page.keyboard.press("Control+Enter")
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(0.5)
            
            send_selectors = ['[aria-label*="gửi" i]', '[aria-label*="send" i]', '[mattooltip*="gửi" i]', '[mattooltip*="send" i]']
            
            clicked_send = False
            for sel in send_selectors:
                elements = page.locator(sel).all()
                for el in elements:
                    if el.is_visible():
                        el.click(timeout=2000)
                        clicked_send = True
                        break
                if clicked_send: break
                    
            if not clicked_send:
                right_btns = page.locator('button:right-of(div[contenteditable="true"])').all()
                for btn in reversed(right_btns):
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        break
            
            return initial_count
        except Exception as e:
            return None
    return None

# ============================================================
# LUỒNG CHẠY CHÍNH (MAIN LOOP)
# ============================================================
def run_auto_translate_srt(prompt_file, cn_folder, vi_folder, wait_time=40, delay_time=15, log_callback=print, **kwargs):
    force_kill_chrome(log_callback)
    log_callback("🚀 Khởi động trình duyệt Playwright...")
    os.makedirs(vi_folder, exist_ok=True)

    srt_files = [f for f in os.listdir(cn_folder) if f.endswith('.srt')]
    
    def sort_by_number(filename):
        numbers = re.findall(r'\d+', filename)
        return int(numbers[-1]) if numbers else 0
        
    srt_files.sort(key=sort_by_number)

    if not srt_files:
        log_callback("⚠️ Không tìm thấy file .srt nào trong thư mục nguồn!")
        return

    with sync_playwright() as p:
        user_data_dir = "./chrome_data"
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )

        page = browser.new_page()
        page.goto("https://gemini.google.com/app", timeout=60000)
        page.wait_for_load_state("load")

        log_callback("⏳ Đang chờ 3s để tải xong giao diện Gemini...")
        time.sleep(3)

        send_initial_prompt(page, prompt_file, log_callback)

        files_translated_in_session = 0
        BATCH_SIZE = 3
        MAX_RETRIES = 3 # Số lần tối đa cho phép dịch lại 1 file nếu lỗi cấu trúc

        for file_name in srt_files:
            cn_file_path = os.path.join(cn_folder, file_name)
            vi_file_path = os.path.join(vi_folder, file_name)

            if os.path.exists(vi_file_path):
                log_callback(f"⏭️ File {file_name} đã được dịch thành công, bỏ qua...")
                continue
                
            # Kiểm tra làm mới theo Batch (Mẻ)
            if files_translated_in_session >= BATCH_SIZE:
                log_callback(f"\n🔄 [HỆ THỐNG] Đã hoàn thành 1 mẻ ({BATCH_SIZE} file). Đang làm mới phiên chat...")
                try:
                    page.goto("https://gemini.google.com/app", timeout=45000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    send_initial_prompt(page, prompt_file, log_callback)
                    files_translated_in_session = 0
                except Exception as e:
                    log_callback(f"⚠️ Cảnh báo khi làm mới: {e}")

            log_callback(f"\n--- Đang xử lý: {file_name} ---")
            
            attempt = 0
            is_file_success = False

            # VÒNG LẶP RETRY: Dịch, So sánh, Dịch lại nếu sai
            while attempt < MAX_RETRIES and not is_file_success:
                attempt += 1
                if attempt > 1:
                    log_callback(f"\n♻️ TIẾN HÀNH DỊCH LẠI {file_name} (Lần thử: {attempt}/{MAX_RETRIES})...")

                # 1. Kiểm tra mô hình trước khi dịch
                while True:
                    model_status = check_model_status(page, log_callback)
                    if model_status == "FLASH_LITE":
                        log_callback("🛑 BỊ HẠ CẤP XUỐNG FLASH-LITE! Script tạm dừng 60 phút...")
                        countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:")
                        page.goto("https://gemini.google.com/app", timeout=60000)
                        page.wait_for_load_state("load")
                        time.sleep(5)
                        send_initial_prompt(page, prompt_file, log_callback)
                    else:
                        break

                # 2. Upload và Dịch
                short_prompt = (
                    "Hãy dịch file SRT đính kèm sang tiếng Việt.\n"
                    "NHẮC LẠI LUẬT QUAN TRỌNG:\n"
                    "- Giữ nguyên hoàn toàn cấu trúc ID và Timecode.\n"
                    "- Tuân thủ nghiêm ngặt phong cách dịch và xưng hô đã thống nhất ở tin nhắn đầu tiên.\n"
                    "- Chỉ trả về nội dung file SRT đã dịch, không giải thích thêm."
                )

                initial_count = upload_srt_and_send(page, cn_file_path, short_prompt, log_callback)

                if initial_count is None:
                    log_callback(f"❌ Upload thất bại. F5 tải lại trang...")
                    page.goto("https://gemini.google.com/app", timeout=45000)
                    page.wait_for_load_state("load")
                    time.sleep(5)
                    send_initial_prompt(page, prompt_file, log_callback)
                    continue

                success = smart_wait_for_gemini(page, initial_count, wait_time, log_callback)
                responses = page.locator('.model-response-text').all_inner_texts()
                
                if responses:
                    latest_response = responses[-1]
                    clean_srt = clean_gemini_output(latest_response)

                    # --- ĐOẠN CODE ĐÃ ĐƯỢC SỬA LẠI TÊN BIẾN ---
                    regex_pattern = r'\[' + r'cite:\s*\d+\]'
                    clean_srt = re.sub(regex_pattern, '', clean_srt)
                    # Lưu file tạm thời để kiểm tra
                    with open(vi_file_path, "w", encoding="utf-8") as f:
                        f.write(clean_srt)
                    
                    # 3. TIẾN HÀNH KIỂM TRA SO SÁNH VỚI BẢN GỐC
                    log_callback(f"⚖️ Đang so sánh cấu trúc định dạng cho {file_name}...")
                    if is_srt_structure_match(cn_file_path, vi_file_path, log_callback):
                        log_callback(f"✅ ĐẠT YÊU CẦU: Trùng khớp 100% ID và Timeline. Đã lưu {file_name}.")
                        files_translated_in_session += 1
                        is_file_success = True
                    else:
                        log_callback(f"🗑️ LỖI CẤU TRÚC: AI đã làm hỏng định dạng SRT. Đang xóa file và chuẩn bị dịch lại...")
                        os.remove(vi_file_path)
                        
                        # F5 Tải lại trang để xóa sạch trí nhớ/ngữ cảnh sai lệch của AI
                        page.goto("https://gemini.google.com/app", timeout=45000)
                        page.wait_for_load_state("load")
                        time.sleep(5)
                        send_initial_prompt(page, prompt_file, log_callback)
                        
                        # Nghỉ một chút trước khi thử lại
                        time.sleep(5)
                else:
                    log_callback(f"❌ Không lấy được text. Thử lại...")

            # Kết thúc quá trình thử của 1 file
            if not is_file_success:
                log_callback(f"⚠️ BỎ QUA FILE: Đã thử {MAX_RETRIES} lần nhưng AI liên tục làm hỏng định dạng file {file_name}.")

            log_callback(f"Bắt đầu nghỉ {delay_time}s trước khi chạy file tiếp theo...")
            countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ, còn:")

        log_callback("\n🎉 ĐÃ HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH DỊCH!")
        browser.close()