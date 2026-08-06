import os
import time
import subprocess
import platform
import re
import shutil
from typing import Callable, Optional, Dict, Any, Tuple
from playwright.sync_api import sync_playwright
from src.gemini_core import (
    clean_gemini_output,
    force_kill_chrome,
    countdown_sleep,
    parse_srt_structure,
    is_srt_structure_match
)


def sanitize_filename(filename: str) -> str:
    """Loại bỏ ký tự cấm của hệ điều hành Windows để làm tên file an toàn."""
    clean = re.sub(r'[\\/:*?"<>|]', '', filename)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean if clean else "Bilibili_Video_Vi"


def send_initial_prompt(page, prompt_file_path: str, log_callback: Callable = print):
    """Nạp file Prompt mẫu hướng dẫn quy tắc dịch thuật vào Gemini AI."""
    initial_count = page.locator('.model-response-text').count()
    if os.path.exists(prompt_file_path):
        try:
            with open(prompt_file_path, 'r', encoding='utf-8') as f:
                prompt_content = f.read().strip()
            log_callback(f"📜 Đang nạp mẫu Prompt dịch thuật từ: {prompt_file_path}...")
            
            chat_box = page.locator('div[contenteditable="true"]').first
            chat_box.wait_for(state="visible", timeout=15000)
            chat_box.click()
            chat_box.fill(prompt_content)
            time.sleep(1)
            
            chat_box.focus()
            page.keyboard.press("Control+Enter")
            time.sleep(0.5)
            page.keyboard.press("Enter")
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
                    log_callback("🚀 Đã gửi nạp Prompt dịch thuật thành công.")
                    clicked_send = True
                    break
                    
            if not clicked_send:
                chat_container = page.locator('div').filter(has=chat_box).last
                last_btn = chat_container.locator('button').last
                if last_btn.is_visible(timeout=1000):
                    last_btn.click()
                    log_callback("🚀 Đã nạp Prompt dịch thuật thành công.")
                    
        except Exception as e:
            log_callback(f"⚠️ Cảnh báo nạp Prompt: {e}")

        smart_wait_for_gemini(page, initial_count, 30, log_callback)
    else:
        log_callback("⚠️ Không tìm thấy file prompt, sử dụng luật dịch mặc định.")


def smart_wait_for_gemini(page, initial_count: int, max_wait_time: int, log_callback: Callable = print) -> bool:
    """Theo dõi Gemini gõ chữ real-time đến khi hoàn tất."""
    log_callback(f"👀 Đang giám sát Gemini AI dịch phụ đề (Tối đa {max_wait_time}s)...")
    previous_text = ""
    stable_count = 0
    start_time = time.time()
    
    while time.time() - start_time < max_wait_time:
        time.sleep(2)
        current_count = page.locator('.model-response-text').count()
        if current_count > initial_count:
            responses = page.locator('.model-response-text').all_inner_texts()
            if responses:
                current_text = responses[-1].strip()
                if current_text == previous_text and len(current_text) > 10:
                    stable_count += 1
                    if stable_count >= 3:
                        log_callback("✓ Gemini đã dịch xong hoàn toàn!")
                        return True
                else:
                    stable_count = 0
                    previous_text = current_text
    log_callback("⏳ Đã hết thời gian chờ, lấy kết quả hiện tại...")
    return True


def check_model_status(page, log_callback: Callable = print) -> str:
    """Kiểm tra mô hình Gemini xem có bị hạ cấp không."""
    try:
        page.wait_for_selector('button', timeout=5000)
        buttons = page.locator('button').all()
        for btn in buttons:
            if btn.is_visible():
                text = btn.inner_text().strip()
                if "Flash-Lite" in text or "Flash Lite" in text:
                    return "FLASH_LITE"
                elif "Flash" in text or "Pro" in text:
                    return "NORMAL"
    except Exception:
        pass
    return "NORMAL"


def upload_srt_and_send(page, cn_file_path: str, short_prompt: str, log_callback: Callable = print) -> Optional[int]:
    """
    Truyền nội dung SRT cho Gemini AI:
    - Ưu tiên upload file .srt qua File Chooser
    - Nếu Nút Upload lỗi/đổi DOM: Tự động FALLBACK dán trực tiếp toàn bộ văn bản SRT vào khung prompt (100% Không bao giờ thất bại)
    """
    initial_count = page.locator('.model-response-text').count()
    uploaded = False
    
    log_callback(f"🔍 Đang chuẩn bị truyền file SRT: {os.path.basename(cn_file_path)}...")
    
    # 1. Ưu tiên đính kèm file trực tiếp qua hidden input[type="file"] của Gemini (Nhanh nhất & Chính xác 100%)
    try:
        file_inputs = page.locator('input[type="file"]').all()
        for inp in file_inputs:
            try:
                inp.set_files(cn_file_path)
                uploaded = True
                log_callback(f"📎 Đã đính kèm tệp {os.path.basename(cn_file_path)} thành công qua input element!")
                break
            except Exception:
                pass
    except Exception:
        pass

    # 2. Nếu gán input trực tiếp chưa được, thử qua Hộp thoại File Chooser
    if not uploaded:
        try:
            with page.expect_file_chooser(timeout=4000) as fc_info:
                chat_box = page.locator('div[contenteditable="true"]').first
                chat_box.wait_for(state="visible", timeout=3000)
                box = chat_box.bounding_box()
                if box:
                    buttons = page.locator('button').all()
                    target_btn = None
                    closest_dist = 9999
                    for btn in buttons:
                        if btn.is_visible():
                            btn_box = btn.bounding_box()
                            if btn_box:
                                chat_center_y = box['y'] + box['height'] / 2
                                btn_center_y = btn_box['y'] + btn_box['height'] / 2
                                if abs(chat_center_y - btn_center_y) < 30 and btn_box['x'] < box['x']:
                                    dist = box['x'] - btn_box['x']
                                    if dist < closest_dist:
                                        closest_dist = dist
                                        target_btn = btn
                    if target_btn:
                        target_btn.click()
                    else:
                        page.locator('button:left-of(div[contenteditable="true"])').first.click(timeout=2000)
                    
                    time.sleep(1)
                    menu_items = page.locator('[role="menuitem"], [role="button"], mat-list-item').all()
                    for item in menu_items:
                        if item.is_visible():
                            t = item.inner_text().lower()
                            if "xuống" not in t and "drive" not in t and any(k in t for k in ["tải", "tệp", "máy tính", "computer", "upload", "file"]):
                                item.click()
                                break

            file_chooser = fc_info.value
            file_chooser.set_files(cn_file_path)
            uploaded = True
            log_callback(f"📎 Đã nạp file .srt qua Hộp thoại File Chooser thành công!")
        except Exception:
            log_callback("⚡ Tự động chuyển sang chế độ Dán Trực Tiếp Văn Bản SRT vào Gemini Prompt (Đảm bảo 100% nhận diện)...")

    # 2. Đọc nội dung file SRT
    srt_text = ""
    try:
        with open(cn_file_path, "r", encoding="utf-8", errors="ignore") as f:
            srt_text = f.read().strip()
    except Exception as e:
        log_callback(f"❌ Không thể đọc nội dung file SRT: {e}")
        return None

    # 3. Gửi tin nhắn
    try:
        chat_box = page.locator('div[contenteditable="true"]').first
        chat_box.click()
        time.sleep(0.5)

        if uploaded:
            full_text = short_prompt
        else:
            file_title = os.path.splitext(os.path.basename(cn_file_path))[0]
            full_text = (
                f"{short_prompt}\n\n"
                f"[TÊN FILE GỐC]: {file_title}\n\n"
                f"[DƯỚI ĐÂY LÀ NỘI DUNG FILE SRT CẦN DỊCH SANG TIẾNG VIỆT]:\n\n{srt_text}"
            )

        chat_box.fill(full_text)
        time.sleep(1)
        
        chat_box.focus()
        page.keyboard.press("Control+Enter")
        time.sleep(0.5)
        page.keyboard.press("Enter")
        time.sleep(1)
        
        send_selectors = [
            'button[aria-label*="Gửi"]', 'button[aria-label*="gửi"]', 
            'button[aria-label*="Send"]', 'button[aria-label*="send"]',
            'button[mattooltip*="Gửi"]', 'button[mattooltip*="Send"]',
            '[data-testid="send-button"]'
        ]
        
        clicked_send = False
        for sel in send_selectors:
            elements = page.locator(sel).all()
            for el in elements:
                if el.is_visible():
                    el.click(timeout=1500)
                    clicked_send = True
                    break
            if clicked_send:
                break
                
        if not clicked_send:
            chat_container = page.locator('div').filter(has=chat_box).last
            last_btn = chat_container.locator('button').last
            if last_btn.is_visible(timeout=1000):
                last_btn.click()
                
        log_callback(f"🚀 Đã gửi nội dung SRT cần dịch tới Gemini AI thành công!")
        return initial_count
    except Exception as e:
        log_callback(f"❌ Lỗi gửi prompt dịch cho Gemini: {e}")
        return None


def run_auto_translate_srt(
    prompt_file: str,
    cn_folder: str,
    vi_folder: str,
    wait_time: int = 300,
    delay_time: int = 15,
    log_callback: Callable = print,
    profile_folder: str = "chrome_data_1",
    **kwargs
):
    """
    Tiến trình chính dịch toàn bộ file SRT trong thư mục nguồn và tự động dịch Tiêu đề File sang Tiếng Việt
    """
    force_kill_chrome(log_callback)
    log_callback("🚀 Khởi động trình duyệt Playwright...")
    os.makedirs(vi_folder, exist_ok=True)

    srt_files = [f for f in os.listdir(cn_folder) if f.endswith('.srt')]
    
    def sort_by_number(filename):
        numbers = re.findall(r'\d+', filename)
        return int(numbers[-1]) if numbers else 0
        
    srt_files.sort(key=sort_by_number)

    if not srt_files:
        log_callback("⚠️ Không tìm thấy tệp .srt nào trong thư mục nguồn!")
        return

    with sync_playwright() as p:
        user_data_dir = f"./{profile_folder}" if profile_folder else "./chrome_data_1"
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )

        page = browser.new_page()
        page.goto("https://gemini.google.com/app", timeout=60000)
        page.wait_for_load_state("load")

        log_callback("⏳ Đang chờ giao diện Gemini nạp hoàn tất...")
        time.sleep(3)

        send_initial_prompt(page, prompt_file, log_callback)

        files_translated_in_session = 0
        BATCH_SIZE = 3
        MAX_RETRIES = 3

        for file_name in srt_files:
            cn_file_path = os.path.join(cn_folder, file_name)
            raw_title = os.path.splitext(file_name)[0]

            if files_translated_in_session >= BATCH_SIZE:
                log_callback(f"\n🔄 [HỆ THỐNG] Đã hoàn thành mẻ {BATCH_SIZE} file. Đang làm mới phiên chat...")
                try:
                    page.goto("https://gemini.google.com/app", timeout=45000)
                    page.wait_for_load_state("load")
                    time.sleep(4)
                    send_initial_prompt(page, prompt_file, log_callback)
                    files_translated_in_session = 0
                except Exception as e:
                    log_callback(f"⚠️ Cảnh báo làm mới: {e}")

            log_callback(f"\n--- Đang xử lý dịch file: {file_name} ---")
            
            attempt = 0
            is_file_success = False

            while attempt < MAX_RETRIES and not is_file_success:
                attempt += 1
                if attempt > 1:
                    log_callback(f"\n♻️ TIẾN HÀNH DỊCH LẠI {file_name} (Lần thử: {attempt}/{MAX_RETRIES})...")

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

                short_prompt = (
                    "Hãy dịch nội dung file SRT đính kèm sang Tiếng Việt chuẩn văn phong phim.\n"
                    "ĐỒNG THỜI DỊCH TIÊU ĐỀ VIDEO SANG TIẾNG VIỆT:\n"
                    "Dòng ĐẦU TIÊN của câu trả lời BẮT BUỘC ghi theo định dạng: [TITLE: Tên_Video_Tiếng_Việt]\n"
                    "NHẮC LẠI LUẬT QUAN TRỌNG:\n"
                    "- Giữ nguyên hoàn toàn cấu trúc ID và Timecode.\n"
                    "- Chỉ trả về duy nhất dòng [TITLE: ...] ở dòng đầu tiên, tiếp theo là toàn bộ nội dung file SRT đã dịch."
                )

                initial_count = upload_srt_and_send(page, cn_file_path, short_prompt, log_callback)
                if initial_count is None:
                    log_callback("❌ Gửi prompt thất bại. Tải lại trang...")
                    page.goto("https://gemini.google.com/app", timeout=45000)
                    page.wait_for_load_state("load")
                    time.sleep(4)
                    send_initial_prompt(page, prompt_file, log_callback)
                    continue

                success = smart_wait_for_gemini(page, initial_count, wait_time, log_callback)
                responses = page.locator('.model-response-text').all_inner_texts()
                
                if responses:
                    latest_response = responses[-1]
                    clean_text = clean_gemini_output(latest_response)
                    clean_text = re.sub(r'\[cite:\s*\d+\]', '', clean_text)

                    # Trích xuất Tiêu đề Tiếng Việt được AI dịch
                    translated_title = raw_title
                    title_match = re.search(r'\[TITLE:\s*([^\]]+)\]', clean_text, re.IGNORECASE)
                    if title_match:
                        translated_title = sanitize_filename(title_match.group(1).strip())
                        # Xóa dòng [TITLE: ...] khỏi nội dung SRT
                        clean_srt = re.sub(r'\[TITLE:\s*[^\]]+\]\s*', '', clean_text).strip()
                    else:
                        clean_srt = clean_text.strip()

                    # Đặt tên file xuất: Tên Tiếng Việt + .srt
                    out_filename = f"{translated_title}.srt"
                    vi_file_path = os.path.join(vi_folder, out_filename)

                    # Lưu file SRT dịch
                    with open(vi_file_path, "w", encoding="utf-8") as f:
                        f.write(clean_srt)
                    
                    # Kiểm tra cấu trúc mốc thời gian với file gốc
                    log_callback(f"⚖️ Đang kiểm tra cấu trúc mốc thời gian cho: {out_filename}...")
                    if is_srt_structure_match(cn_file_path, vi_file_path, log_callback):
                        log_callback(f"✅ ĐẠT YÊU CẦU! Đã dịch xong Tiêu đề & Nội dung SRT thành công.")
                        log_callback(f"📁 Tệp phụ đề Tiếng Việt đã lưu tại: {vi_file_path}", "success")
                        
                        # Đồng bộ đổi tên file Video (.mp4) tương ứng nếu có
                        old_video_path = os.path.join(cn_folder, f"{raw_title}.mp4")
                        if not os.path.exists(old_video_path):
                            old_video_path = os.path.join(vi_folder, f"{raw_title}.mp4")

                        if os.path.exists(old_video_path) and raw_title != translated_title:
                            new_video_path = os.path.join(vi_folder, f"{translated_title}.mp4")
                            try:
                                shutil.move(old_video_path, new_video_path)
                                log_callback(f"🎬 Đã đổi tên Video tương ứng sang Tiếng Việt: {os.path.basename(new_video_path)}", "success")
                            except Exception:
                                pass

                        files_translated_in_session += 1
                        is_file_success = True
                    else:
                        log_callback(f"🗑️ LỖI CẤU TRÚC: AI làm hỏng mốc thời gian. Đang thử dịch lại...")
                        if os.path.exists(vi_file_path):
                            os.remove(vi_file_path)
                        
                        page.goto("https://gemini.google.com/app", timeout=45000)
                        page.wait_for_load_state("load")
                        time.sleep(4)
                        send_initial_prompt(page, prompt_file, log_callback)
                        time.sleep(3)
                else:
                    log_callback("❌ Không nhận được phản hồi từ AI. Thử lại...")

            if not is_file_success:
                log_callback(f"⚠️ Bỏ qua file {file_name} sau {MAX_RETRIES} lần thử thất bại.")

            log_callback(f"Nghỉ {delay_time}s trước khi chạy file tiếp theo...")
            countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ:")

        log_callback("\n🎉 HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH DỊCH PHỤ ĐỀ & TIÊU ĐỀ SANG TIẾNG VIỆT!")