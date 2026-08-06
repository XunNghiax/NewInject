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
from src.srt_utils import split_srt_file, merge_numbered_srt_files


def sanitize_filename(filename: str) -> str:
    """Loại bỏ ký tự cấm của hệ điều hành Windows để làm tên file an toàn."""
    clean = re.sub(r'[\\/:*?"<>|]', '', filename)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean if clean else "Bilibili_Video_Vi"


def count_srt_blocks(file_path: str) -> int:
    """Đếm tổng số lượng block SRT trong tệp."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        matches = re.findall(r'(?m)^(\d+)\s*\n\d{2}:\d{2}:\d{2}', content)
        return len(matches)
    except Exception:
        return 0


def send_initial_prompt(page, prompt_file_path: str, log_callback: Callable = print, check_pause_callback: Optional[Callable] = None):
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

        smart_wait_for_gemini(page, initial_count, 60, log_callback, check_pause_callback=check_pause_callback)
    else:
        log_callback("⚠️ Không tìm thấy file prompt, sử dụng luật dịch mặc định.")


def smart_wait_for_gemini(page, initial_count: int, max_wait_time: int, log_callback: Callable = print, check_pause_callback: Optional[Callable] = None) -> bool:
    """Theo dõi Gemini gõ chữ real-time đến khi hoàn tất, có hỗ trợ tạm dừng."""
    log_callback(f"👀 Đang giám sát Gemini AI dịch phụ đề (Tối đa {max_wait_time}s)...")
    previous_text = ""
    stable_count = 0
    start_time = time.time()
    
    while time.time() - start_time < max_wait_time:
        if check_pause_callback:
            check_pause_callback()
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
    Truyền nội dung SRT cho Gemini AI qua File Upload hoặc Direct Text Paste (100% Fail-safe)
    """
    initial_count = page.locator('.model-response-text').count()
    uploaded = False
    
    log_callback(f"🔍 Đang chuẩn bị truyền file SRT: {os.path.basename(cn_file_path)}...")
    
    # 1. Thử Upload File
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

    if not uploaded:
        try:
            with page.expect_file_chooser(timeout=3000) as fc_info:
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


def translate_single_srt_file(
    page,
    cn_file_path: str,
    vi_file_path: str,
    prompt_file: str,
    wait_time: int = 300,
    log_callback: Callable = print,
    check_pause_callback: Optional[Callable] = None
) -> bool:
    """Hàm xử lý dịch 1 file SRT đơn lẻ qua Gemini AI."""
    file_name = os.path.basename(cn_file_path)
    MAX_RETRIES = 3
    attempt = 0

    short_prompt = (
        "Hãy dịch toàn bộ nội dung file SRT đính kèm sang Tiếng Việt chuẩn văn phong phim.\n"
        "NHẮC LẠI LUẬT BẮT BUỘC:\n"
        "- Giữ nguyên 100% cấu trúc ID và mốc thời gian (Timeline).\n"
        "- CHỈ trả về duy nhất nội dung file SRT đã dịch và BẮT BUỘC đặt trong khối code block markdown (```srt\n...\n```).\n"
        "- Không thêm bất kỳ câu chào hay lời giải thích nào ngoài khối code block."
    )

    while attempt < MAX_RETRIES:
        attempt += 1
        if attempt > 1:
            log_callback(f"\n♻️ TIẾN HÀNH DỊCH LẠI {file_name} (Lần thử: {attempt}/{MAX_RETRIES})...")

        while True:
            model_status = check_model_status(page, log_callback)
            if model_status == "FLASH_LITE":
                log_callback("🛑 BỊ HẠ CẤP XUỐNG FLASH-LITE! Script tạm dừng 60 phút...")
                countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:", check_pause_callback=check_pause_callback)
                page.goto("https://gemini.google.com/app", timeout=60000)
                page.wait_for_load_state("load")
                time.sleep(5)
                send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
            else:
                break

        initial_count = upload_srt_and_send(page, cn_file_path, short_prompt, log_callback)
        if initial_count is None:
            log_callback("❌ Gửi prompt thất bại. Tải lại trang...")
            page.goto("https://gemini.google.com/app", timeout=45000)
            page.wait_for_load_state("load")
            time.sleep(4)
            send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
            continue

        success = smart_wait_for_gemini(page, initial_count, wait_time, log_callback, check_pause_callback=check_pause_callback)
        responses = page.locator('.model-response-text').all_inner_texts()
        
        if responses:
            latest_response = responses[-1]
            clean_srt = clean_gemini_output(latest_response)
            clean_srt = re.sub(r'\[cite:\s*\d+\]', '', clean_srt)

            os.makedirs(os.path.dirname(os.path.abspath(vi_file_path)), exist_ok=True)
            with open(vi_file_path, "w", encoding="utf-8") as f:
                f.write(clean_srt)
            
            log_callback(f"⚖️ Đang kiểm tra cấu trúc mốc thời gian cho: {os.path.basename(vi_file_path)}...")
            if is_srt_structure_match(cn_file_path, vi_file_path, log_callback):
                log_callback(f"✅ ĐẠT YÊU CẦU! Đã dịch xong tệp phụ đề SRT sang Tiếng Việt.", "success")
                return True
            else:
                log_callback(f"🗑️ LỖI CẤU TRÚC: AI làm hỏng mốc thời gian. Đang thử dịch lại...")
                if os.path.exists(vi_file_path):
                    os.remove(vi_file_path)
                
                page.goto("https://gemini.google.com/app", timeout=45000)
                page.wait_for_load_state("load")
                time.sleep(4)
                send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
                time.sleep(3)
        else:
            log_callback("❌ Không nhận được phản hồi từ AI. Thử lại...")

    return False


def run_auto_translate_srt(
    prompt_file: str,
    cn_folder: str,
    vi_folder: str,
    wait_time: int = 300,
    delay_time: int = 15,
    log_callback: Callable = print,
    profile_folder: str = "chrome_data_1",
    check_pause_callback: Optional[Callable] = None,
    blocks_per_split: int = 100,
    **kwargs
):
    """
    Tiến trình dịch phụ đề tự động:
    - Nếu file SRT lớn (>100 block), tự động TÁCH THÀNH CÁC FILE NHỎ 100 block vào thư mục tạm.
    - Gửi dịch lần lượt từng file nhỏ qua Gemini AI.
    - Tự động GỘP THÀNH FILE SRT TIẾNG VIỆT HOÀN CHỈNH duy nhất và dọn dẹp thư mục tạm.
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

        send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)

        files_translated_in_session = 0
        BATCH_SIZE = 3

        for file_name in srt_files:
            cn_file_path = os.path.join(cn_folder, file_name)
            raw_title = os.path.splitext(file_name)[0]
            total_blocks = count_srt_blocks(cn_file_path)

            log_callback(f"\n--- 🎬 Đang xử lý tệp phụ đề: {file_name} ({total_blocks} block) ---")

            # ── TRƯỜNG HỢP 1: TỆP LỚN (>100 BLOCK) ➔ TÁCH FILE, DỊCH TUẦN TỰ VÀ GỘP ──
            if total_blocks > blocks_per_split:
                log_callback(f"📦 Phát hiện tệp SRT lớn ({total_blocks} block > {blocks_per_split}). Tự động kích hoạt chế độ TÁCH FILE...")
                
                temp_split_cn_dir = os.path.join(cn_folder, f"temp_split_cn_{raw_title}")
                temp_split_vi_dir = os.path.join(vi_folder, f"temp_split_vi_{raw_title}")
                os.makedirs(temp_split_cn_dir, exist_ok=True)
                os.makedirs(temp_split_vi_dir, exist_ok=True)

                split_prefix = os.path.join(temp_split_cn_dir, "part")
                split_srt_file(cn_file_path, output_prefix=split_prefix, blocks_per_file=blocks_per_split, log_callback=log_callback)

                split_files = [f for f in os.listdir(temp_split_cn_dir) if f.endswith('.srt')]
                split_files.sort(key=sort_by_number)

                all_parts_ok = True
                for part_name in split_files:
                    part_cn_path = os.path.join(temp_split_cn_dir, part_name)
                    part_vi_path = os.path.join(temp_split_vi_dir, part_name)

                    if files_translated_in_session >= BATCH_SIZE:
                        log_callback(f"\n🔄 [HỆ THỐNG] Đã hoàn thành mẻ {BATCH_SIZE} tệp nhỏ. Làm mới phiên chat...")
                        try:
                            page.goto("https://gemini.google.com/app", timeout=45000)
                            page.wait_for_load_state("load")
                            time.sleep(4)
                            send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
                            files_translated_in_session = 0
                        except Exception as e:
                            log_callback(f"⚠️ Cảnh báo làm mới: {e}")

                    log_callback(f"🔹 Đang dịch tệp nhỏ: {part_name}...")
                    part_ok = translate_single_srt_file(
                        page, part_cn_path, part_vi_path, prompt_file, 
                        wait_time=wait_time, log_callback=log_callback, check_pause_callback=check_pause_callback
                    )
                    
                    if part_ok:
                        files_translated_in_session += 1
                        countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ:", check_pause_callback=check_pause_callback)
                    else:
                        all_parts_ok = False
                        log_callback(f"❌ Không thể dịch tệp nhỏ {part_name}. Bỏ qua tệp lớn này.")
                        break

                if all_parts_ok:
                    out_filename = file_name if file_name.endswith('_vi.srt') else f"{raw_title}_vi.srt"
                    final_vi_path = os.path.join(vi_folder, out_filename)
                    log_callback(f"\n🧩 Đang tiến hành GỘP TẤT CẢ các tệp nhỏ đã dịch thành: {out_filename}...")
                    merge_numbered_srt_files(temp_split_vi_dir, final_vi_path, log_callback=log_callback)
                    log_callback(f"🎉 HOÀN THÀNH GỘP PHỤ ĐỀ TIẾNG VIỆT HOÀN CHỈNH: {final_vi_path}", "success")

                # Dọn dẹp thư mục tạm
                try:
                    shutil.rmtree(temp_split_cn_dir, ignore_errors=True)
                    shutil.rmtree(temp_split_vi_dir, ignore_errors=True)
                except Exception:
                    pass

            # ── TRƯỜNG HỢP 2: TỆP NHỎ (<=100 BLOCK) ➔ DỊCH TRỰC TIẾP ──
            else:
                if files_translated_in_session >= BATCH_SIZE:
                    log_callback(f"\n🔄 [HỆ THỐNG] Đã hoàn thành mẻ {BATCH_SIZE} file. Làm mới phiên chat...")
                    try:
                        page.goto("https://gemini.google.com/app", timeout=45000)
                        page.wait_for_load_state("load")
                        time.sleep(4)
                        send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
                        files_translated_in_session = 0
                    except Exception as e:
                        log_callback(f"⚠️ Cảnh báo làm mới: {e}")

                out_filename = file_name if file_name.endswith('_vi.srt') else f"{raw_title}_vi.srt"
                vi_file_path = os.path.join(vi_folder, out_filename)
                
                ok = translate_single_srt_file(
                    page, cn_file_path, vi_file_path, prompt_file, 
                    wait_time=wait_time, log_callback=log_callback, check_pause_callback=check_pause_callback
                )
                if ok:
                    files_translated_in_session += 1
                    countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ:", check_pause_callback=check_pause_callback)

        log_callback("\n🎉 HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH DỊCH & GỘP PHỤ ĐỀ SANG TIẾNG VIỆT!")