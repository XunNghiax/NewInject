import os
import time
import subprocess
import platform
import re
import shutil
from typing import Callable, Optional, Dict, Any, Tuple, List
from playwright.sync_api import sync_playwright
from src.gemini_core import (
    clean_gemini_output,
    force_kill_chrome,
    countdown_sleep,
    parse_srt_structure,
    is_srt_structure_match,
    get_matched_blocks_count,
    resolve_profile_path,
    get_available_profiles,
    record_profile_cooldown,
    is_profile_in_cooldown,
    get_next_available_pro_profile
)
from src.srt_utils import split_srt_file, merge_numbered_srt_files, process_srt_speed


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
            log_callback(f"📜 Đang nạp Prompt từ: {prompt_file_path}...")
            
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
                    log_callback("🚀 Đã nạp Prompt thành công.")
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


def check_model_status(page, log_callback: Callable = print) -> Tuple[str, str]:
    """
    Quét thông minh tên mô hình Gemini và báo trạng thái:
    - Trả về ("FLASH_LITE", text) nếu bị hạ cấp xuống Flash-Lite / Flash / Hết hạn ngạch Pro
    - Trả về ("NORMAL", text) nếu đang ở Gemini Pro / Advanced / 1.5 Pro
    """
    try:
        page.wait_for_timeout(1500)
        
        # 1. Tìm tất cả các phần tử giao diện hiển thị tên mô hình Gemini
        selectors = [
            'bard-mode-switcher', 'gmp-model-picker', '[aria-label*="model" i]',
            '[aria-label*="gemini" i]', '[aria-label*="chế độ" i]', '.model-picker-button',
            'button', '[role="button"]', '[role="combobox"]', 'mat-select', '.model-title'
        ]
        
        found_model_text = ""
        for sel in selectors:
            try:
                elements = page.locator(sel).all()
                for el in elements:
                    if el.is_visible():
                        txt = el.inner_text().strip()
                        if any(kw in txt.lower() for kw in ["gemini", "pro", "flash", "advanced", "lite", "fast", "1.5"]):
                            found_model_text = txt
                            # Kiểm tra xem có từ khóa bị hạ cấp không
                            if any(down in txt.lower() for down in ["flash-lite", "flash lite", "fast", "hạ cấp", "giới hạn"]):
                                if "advanced" not in txt.lower() and "pro" not in txt.lower():
                                    return "FLASH_LITE", f"Đã hạ cấp: {txt}"
            except Exception:
                pass

        # 2. Quét thông báo popup/banner cảnh báo hết quota trên màn hình
        content_lower = page.content().lower()
        if any(msg in content_lower for msg in [
            "flash-lite", "flash lite", "đạt đến giới hạn", "reached your limit",
            "chuyển sang flash", "switched to flash", "hết lượt dùng pro"
        ]):
            return "FLASH_LITE", "Phát hiện thông báo hết hạn ngạch Gemini Pro (Chuyển Flash-Lite)"

        if found_model_text:
            return "NORMAL", found_model_text

    except Exception as e:
        log_callback(f"⚠️ Cảnh báo khi kiểm tra model: {e}")

    return "NORMAL", "Gemini Pro/Advanced"


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

        try:
            chat_box.fill(full_text, timeout=10000)
        except Exception:
            # Fallback cho văn bản lớn trong contenteditable div để tránh timeout 30s của Playwright
            page.evaluate("""
                ([el, text]) => {
                    el.innerText = text;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            """, [chat_box.element_handle(), full_text])
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
        
        # Thử tìm và bấm nút với Visual Debug & Bấm dồn dập
        for _ in range(3): # Thử bấm tối đa 3 chu kỳ nếu nút chưa phản hồi
            for sel in send_selectors:
                elements = page.locator(sel).all()
                for el in elements:
                    if el.is_visible() and el.is_enabled():
                        # VISUAL DEBUG: Đánh dấu nút bấm bằng viền Đỏ, nền Vàng
                        try:
                            page.evaluate("(element) => { element.style.border = '4px solid red'; element.style.backgroundColor = 'yellow'; }", el.element_handle())
                            time.sleep(1) # Dừng 1 giây để người dùng quan sát tận mắt
                        except Exception:
                            pass
                        
                        el.click(timeout=1500)
                        clicked_send = True
                        break
                if clicked_send:
                    break
            
            if clicked_send:
                break
                
            time.sleep(1.5) # Đợi một lát nếu web lag chưa nạp xong nút Gửi
            
        if not clicked_send:
            chat_container = page.locator('div').filter(has=chat_box).last
            last_btn = chat_container.locator('button').last
            if last_btn.is_visible(timeout=1000):
                try:
                    page.evaluate("(element) => { element.style.border = '4px solid red'; element.style.backgroundColor = 'yellow'; }", last_btn.element_handle())
                    time.sleep(1)
                except Exception:
                    pass
                last_btn.click()
                clicked_send = True
                
        # AUTO-F5 Kiểm tra: Nếu sau khi click 2 giây mà text vẫn kẹt trong ô nhập liệu -> Lỗi Web
        time.sleep(2)
        if chat_box.is_visible():
            try:
                remaining_text = chat_box.inner_text().strip()
                if len(remaining_text) > 50:
                    log_callback("⚠️ LỖI KẸT GỬI: Giao diện Gemini đóng băng, không nhận lệnh Click. Ép F5 tải lại...")
                    return None # Trả về None để hệ thống tự động Reload trình duyệt
            except Exception:
                pass
                
        log_callback(f"🚀 Đã tìm thấy và click nút gửi thành công!")
        return initial_count
    except Exception as e:
        log_callback(f"❌ Lỗi gửi prompt dịch cho Gemini: {e}")
        return None


def create_browser_context(p, profile_folder: str, prompt_file: str, log_callback: Callable = print, check_pause_callback: Optional[Callable] = None):
    """Tạo phiên làm việc trình duyệt mới và KIỂM TRA MÔ HÌNH TRƯỚC KHI GỬI PROMPT."""
    target_dir = resolve_profile_path(profile_folder)
    log_callback(f"🌐 Đang mở trình duyệt với Profile: [{profile_folder}] ({target_dir})...")
    os.makedirs(target_dir, exist_ok=True)
    
    browser = p.chromium.launch_persistent_context(
        user_data_dir=target_dir,
        headless=False,
        channel="chrome",
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
    )
    page = browser.new_page()
    page.goto("https://gemini.google.com/app", timeout=60000)
    page.wait_for_load_state("load")

    log_callback(f"⏳ Đang kiểm tra phiên bản Gemini AI trên Profile [{profile_folder}]...")
    time.sleep(3)
    
    # 🔍 KIỂM TRA PHIÊN BẢN MODEL TRƯỚC KHI GỬI BẤT KỲ PROMPT NÀO
    status, model_name = check_model_status(page, log_callback)
    if status == "FLASH_LITE":
        log_callback(f"⚠️ CẢNH BÁO TRƯỚC KHI GỬI PROMPT: Profile [{profile_folder}] bị hạ cấp ({model_name})!", "warning")
        return browser, page, status

    log_callback(f"✅ XÁC NHẬN MÔ HÌNH HỢP LỆ: {model_name} 🚀 -> Tiến hành nạp Prompt ...", "success")
    send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
    return browser, page, status


def find_existing_translated_file(cn_file_path: str, vi_folder: str) -> Optional[str]:
    """
    Tìm file phụ đề tiếng Việt đã dịch (nằm ở thư mục vi hoặc temp_split_vi_)
    trùng khớp cấu trúc với cn_file_path. Bắt buộc KHÔNG so sánh với chính file gốc cn_file_path.
    """
    if not os.path.exists(cn_file_path):
        return None
        
    cn_file_path_abs = os.path.abspath(cn_file_path)
    filename = os.path.basename(cn_file_path)
    raw_title = os.path.splitext(filename)[0]
    cn_dir = os.path.dirname(cn_file_path_abs)
    vi_folder_abs = os.path.abspath(vi_folder)
    
    candidates = [
        os.path.join(vi_folder_abs, f"{raw_title}_vi.srt"),
        os.path.join(vi_folder_abs, f"{raw_title}.srt"),
        os.path.join(vi_folder_abs, filename),
    ]

    # Nếu cn_file_path nằm trong thư mục con speed_...
    current_dir_name = os.path.basename(cn_dir)
    check_dirs = [cn_dir]
    if current_dir_name.startswith("speed_"):
        real_parent = os.path.dirname(cn_dir)
        check_dirs.append(real_parent)

    for c_dir in check_dirs:
        parent_folder_name = os.path.basename(c_dir)
        if parent_folder_name.startswith("temp_split_cn_"):
            suffix = parent_folder_name.replace("temp_split_cn_", "")
            vi_temp_dir = os.path.join(vi_folder_abs, f"temp_split_vi_{suffix}")
            same_level_vi_temp = os.path.abspath(os.path.join(c_dir, "..", f"temp_split_vi_{suffix}"))
            
            for v_dir in [vi_temp_dir, same_level_vi_temp]:
                candidates.extend([
                    os.path.join(v_dir, filename),
                    os.path.join(v_dir, f"{raw_title}_vi.srt"),
                    os.path.join(v_dir, f"{raw_title}.srt"),
                ])

    # Lọc bỏ tuyệt đối file gốc cn_file_path để tránh tự so sánh với chính nó
    valid_candidates = []
    for cand in candidates:
        cand_abs = os.path.abspath(cand)
        if cand_abs != cn_file_path_abs and cand_abs not in valid_candidates:
            valid_candidates.append(cand_abs)

    # Kiểm tra sự tồn tại và khớp cấu trúc mốc thời gian
    for cand in valid_candidates:
        if os.path.exists(cand):
            if is_srt_structure_match(cn_file_path_abs, cand):
                return cand
    return None


def run_auto_translate_srt(
    prompt_file: str,
    cn_folder: str,
    vi_folder: str,
    wait_time: int = 300,
    delay_time: int = 15,
    log_callback: Callable = print,
    profile_folder: str = "chrome_data_1",
    check_pause_callback: Optional[Callable] = None,
    progress_callback: Optional[Callable] = None,
    blocks_per_split: int = 100,
    target_speed: float = 1.0,
    **kwargs
):
    """
    Tiến trình dịch phụ đề tự động tích hợp CƠ CHẾ XOAY VÒNG ĐA PROFILE CHROME (AUTO PROFILE ROTATION):
    - Tự động kiểm tra phiên bản Gemini AI.
    - Nếu hết hạn ngạch Pro (chuyển sang Flash-Lite) ➔ TỰ ĐỘNG ĐỔI SANG PROFILE TIẾP THEO (chrome_data_2, chrome_data_3...).
    - Hỗ trợ tự động dãn mốc thời gian file Tiếng Trung gốc xuống tốc độ 0.8x (`target_speed=0.8`).
    """
    force_kill_chrome(log_callback)
    os.makedirs(vi_folder, exist_ok=True)

    srt_files = [f for f in os.listdir(cn_folder) if f.endswith('.srt')]
    
    def sort_by_number(filename):
        numbers = re.findall(r'\d+', filename)
        return int(numbers[-1]) if numbers else 0
        
    srt_files.sort(key=sort_by_number)

    if not srt_files:
        log_callback("⚠️ Không tìm thấy tệp .srt nào trong thư mục nguồn!")
        return

    available_profiles = get_available_profiles()
    if profile_folder and profile_folder in available_profiles:
        current_profile_idx = available_profiles.index(profile_folder)
    elif profile_folder and profile_folder not in available_profiles:
        available_profiles.insert(0, profile_folder)
        current_profile_idx = 0
    else:
        current_profile_idx = 0

    current_profile = available_profiles[current_profile_idx]
    log_callback(f"📋 Tìm thấy {len(available_profiles)} Profile Chrome: {', '.join(available_profiles)} (Bắt đầu với [{current_profile}])")

    with sync_playwright() as p:
        # Kiểm tra xem profile ban đầu có đang bị khóa 5 tiếng không
        in_cd, rem_str, _ = is_profile_in_cooldown(current_profile)
        if in_cd:
            log_callback(f"⏩ [CẢNH BÁO] Profile ban đầu [{current_profile}] đang trong thời gian chờ 5 tiếng (Còn {rem_str}). Tự động nhảy sang Profile tiếp theo!", "warning")
            next_p = get_next_available_pro_profile(current_profile, available_profiles, log_callback)
            if next_p:
                current_profile = next_p
                current_profile_idx = available_profiles.index(next_p)

        browser, page, model_status = create_browser_context(p, current_profile, prompt_file, log_callback, check_pause_callback)
        if model_status == "FLASH_LITE":
            record_profile_cooldown(current_profile, 5.0, log_callback)
            next_p = get_next_available_pro_profile(current_profile, available_profiles, log_callback)
            if next_p:
                log_callback(f"🔄 Profile ban đầu [{current_profile}] bị hết ngạch 5h. TỰ ĐỘNG BỎ QUA & CHUYỂN SANG: [{next_p}]...", "info")
                try:
                    browser.close()
                except Exception:
                    pass
                current_profile = next_p
                current_profile_idx = available_profiles.index(next_p)
                browser, page, model_status = create_browser_context(p, current_profile, prompt_file, log_callback, check_pause_callback)
                if model_status != "FLASH_LITE":
                    log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG! 🚀", "success")

        files_translated_in_session = 0
        BATCH_SIZE = 3

        total_files_for_progress = len(srt_files)
        for idx_prog, file_name in enumerate(srt_files):
            if progress_callback:
                progress_callback(int((idx_prog / total_files_for_progress) * 100), f"Đang xử lý tệp {idx_prog+1}/{total_files_for_progress}...")

            cn_file_path = os.path.join(cn_folder, file_name)

            # Tự động dãn mốc thời gian file Tiếng Trung gốc xuống 0.8x nếu bật
            if target_speed and target_speed != 1.0:
                speed_adj_dir = os.path.join(cn_folder, f"speed_{target_speed}x")
                os.makedirs(speed_adj_dir, exist_ok=True)
                adj_cn_file_path = os.path.join(speed_adj_dir, file_name)
                
                if not os.path.exists(adj_cn_file_path):
                    log_callback(f"⏩ [ĐỔI TỐC ĐỘ {target_speed}x] Đang dãn mốc thời gian file gốc '{file_name}' từ 1.0x xuống {target_speed}x...")
                    process_srt_speed(cn_file_path, adj_cn_file_path, old_speed=1.0, new_speed=target_speed, log_callback=log_callback)
                
                cn_file_path = adj_cn_file_path

            raw_title = os.path.splitext(file_name)[0]
            out_filename = file_name if file_name.endswith('_vi.srt') else f"{raw_title}_vi.srt"
            final_target_vi = os.path.join(vi_folder, out_filename)

            # CẤP ĐỘ 1: Kiểm tra tệp đích cuối cùng đã dịch hoàn tất chưa ở tất cả vị trí
            existing_final = find_existing_translated_file(cn_file_path, vi_folder)
            if existing_final:
                log_callback(f"⏩ Tệp '{file_name}' đã được dịch hoàn tất từ trước tại [{os.path.basename(existing_final)}]. TỰ ĐỘNG BỎ QUA TIẾP TỤC TỆP TIẾP THEO!", "success")
                continue

            total_blocks = count_srt_blocks(cn_file_path)
            log_callback(f"\n--- 🎬 Đang xử lý tệp phụ đề: {file_name} ({total_blocks} block) ---")

            # Danh sách tệp cần dịch (Nếu >100 block thì tách file nhỏ)
            if total_blocks > blocks_per_split:
                log_callback(f"📦 Tệp lớn ({total_blocks} block > {blocks_per_split}). Tự động TÁCH FILE...")
                temp_split_cn_dir = os.path.join(cn_folder, f"temp_split_cn_{raw_title}")
                temp_split_vi_dir = os.path.join(vi_folder, f"temp_split_vi_{raw_title}")
                os.makedirs(temp_split_cn_dir, exist_ok=True)
                os.makedirs(temp_split_vi_dir, exist_ok=True)

                split_prefix = os.path.join(temp_split_cn_dir, "part")
                split_srt_file(cn_file_path, output_prefix=split_prefix, blocks_per_file=blocks_per_split, log_callback=log_callback)

                split_files = [f for f in os.listdir(temp_split_cn_dir) if f.endswith('.srt')]
                split_files.sort(key=sort_by_number)
                targets = [(os.path.join(temp_split_cn_dir, sf), os.path.join(temp_split_vi_dir, sf), sf) for sf in split_files]
                is_batch_split = True
            else:
                targets = [(cn_file_path, final_target_vi, file_name)]
                is_batch_split = False

            all_targets_ok = True
            for part_cn_path, part_vi_path, part_label in targets:

                # CẤP ĐỘ 2: Kiểm tra phân đoạn nhỏ (Checkpoint) ở tất cả vị trí khả dĩ
                existing_part = find_existing_translated_file(part_cn_path, vi_folder)
                if existing_part:
                    log_callback(f"⏩ [CHECKPOINT] Phân đoạn '{part_label}' đã dịch hoàn tất trước đó tại [{os.path.basename(existing_part)}]. BỎ QUA CHUYỂN SANG TỆP TIẾP THEO!", "info")
                    continue

                # Kiểm tra hạn mức Pro trước khi dịch tệp
                status, model_name = check_model_status(page, log_callback)
                if status == "FLASH_LITE":
                    log_callback(f"⚠️ Profile [{current_profile}] bị hạ cấp xuống Flash-Lite (Hết hạn mức Pro trong ngày)!", "warning")
                    record_profile_cooldown(current_profile, 5.0, log_callback)
                    
                    next_p = get_next_available_pro_profile(current_profile, available_profiles, log_callback)
                    switched_ok = False
                    if next_p:
                        log_callback(f"🔄 TỰ ĐỘNG BỎ QUA PROFILE KHÓA & CHUYỂN SANG: [{next_p}]...", "info")
                        try:
                            browser.close()
                        except Exception:
                            pass
                        current_profile = next_p
                        current_profile_idx = available_profiles.index(next_p)
                        browser, page, new_status = create_browser_context(p, current_profile, prompt_file, log_callback, check_pause_callback)
                        if new_status != "FLASH_LITE":
                            switched_ok = True
                            log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG! Tiếp tục duy trì Mô hình Pro 🚀", "success")

                    if not switched_ok:
                        log_callback("🛑 TẤT CẢ CÁC PROFILE CHROME ĐỀU ĐÃ BỊ KHÓA HẠN MỨC PRO (5 TIẾNG)! Tạm dừng 60 phút chờ Gemini reset...")
                        countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:", check_pause_callback=check_pause_callback)
                        page.goto("https://gemini.google.com/app", timeout=60000)
                        page.wait_for_load_state("load")
                        time.sleep(5)
                        send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)

                # Làm mới mẻ chat nếu cần
                if files_translated_in_session >= BATCH_SIZE:
                    log_callback(f"\n🔄 [HỆ THỐNG] Đã hoàn thành mẻ {BATCH_SIZE} tệp. Làm mới phiên chat...")
                    try:
                        page.goto("https://gemini.google.com/app", timeout=45000)
                        page.wait_for_load_state("load")
                        time.sleep(4)
                        send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
                        files_translated_in_session = 0
                    except Exception as e:
                        log_callback(f"⚠️ Cảnh báo làm mới: {e}")

                log_callback(f"🔹 Đang dịch tệp: {part_label}...")
                
                # Thực hiện dịch
                MAX_RETRIES = 3
                part_ok = False
                short_prompt = (
                    "Hãy dịch toàn bộ nội dung file SRT đính kèm sang Tiếng Việt chuẩn văn phong phim.\n"
                    "NHẮC LẠI LUẬT BẮT BUỘC:\n"
                    "- Giữ nguyên 100% cấu trúc ID và mốc thời gian (Timeline).\n"
                    "- CHỈ trả về duy nhất nội dung file SRT đã dịch và BẮT BUỘC đặt trong khối code block markdown (```srt\n...\n```).\n"
                    "- Không thêm bất kỳ câu chào hay lời giải thích nào ngoài khối code block."
                )

                for attempt in range(1, MAX_RETRIES + 1):
                    if attempt > 1:
                        log_callback(f"\n♻️ TIẾN HÀNH DỊCH LẠI {part_label} (Lần thử: {attempt}/{MAX_RETRIES})...")

                    initial_count = upload_srt_and_send(page, part_cn_path, short_prompt, log_callback)
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

                        os.makedirs(os.path.dirname(os.path.abspath(part_vi_path)), exist_ok=True)
                        with open(part_vi_path, "w", encoding="utf-8") as f:
                            f.write(clean_srt)
                        
                        log_callback(f"⚖️ Đang kiểm tra cấu trúc mốc thời gian cho: {os.path.basename(part_vi_path)}...")
                        if is_srt_structure_match(part_cn_path, part_vi_path, log_callback):
                            log_callback(f"✅ ĐẠT YÊU CẦU! Đã dịch xong: {part_label}", "success")
                            part_ok = True
                            break
                        else:
                            log_callback(f"🗑️ LỖI CẤU TRÚC: AI làm hỏng mốc thời gian. Đang thử dịch lại...")
                            if os.path.exists(part_vi_path):
                                os.remove(part_vi_path)
                            page.goto("https://gemini.google.com/app", timeout=45000)
                            page.wait_for_load_state("load")
                            time.sleep(4)
                            send_initial_prompt(page, prompt_file, log_callback, check_pause_callback=check_pause_callback)
                            time.sleep(3)

                if part_ok:
                    files_translated_in_session += 1
                    countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ:", check_pause_callback=check_pause_callback)
                else:
                    all_targets_ok = False
                    log_callback(f"❌ Thất bại khi dịch phần {part_label}.")
                    break

            # Nếu là tệp lớn đã tách ➔ Tiến hành gộp sau khi dịch xong tất cả các part
            if is_batch_split and all_targets_ok:
                out_filename = file_name if file_name.endswith('_vi.srt') else f"{raw_title}_vi.srt"
                final_vi_path = os.path.join(vi_folder, out_filename)
                log_callback(f"\n🧩 Đang tiến hành GỘP TẤT CẢ các tệp nhỏ đã dịch thành: {out_filename}...")
                merge_numbered_srt_files(temp_split_vi_dir, final_vi_path, log_callback=log_callback)
                log_callback(f"🎉 HOÀN THÀNH GỘP PHỤ ĐỀ TIẾNG VIỆT HOÀN CHỈNH: {final_vi_path}", "success")

                try:
                    shutil.rmtree(temp_split_cn_dir, ignore_errors=True)
                    shutil.rmtree(temp_split_vi_dir, ignore_errors=True)
                except Exception:
                    pass

        log_callback("\n🎉 HOÀN THÀNH TOÀN BỘ TIẾN TRÌNH DỊCH & GỘP PHỤ ĐỀ SANG TIẾNG VIỆT!")