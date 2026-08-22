import os
from src.srt_manager import clean_gemini_output, parse_srt_structure, is_srt_structure_match, get_matched_blocks_count
import time
import subprocess
import platform
import re
import shutil
from typing import Callable, Optional, Dict, Any, Tuple, List
from playwright.sync_api import sync_playwright
from src.gemini_bot import (
        force_kill_chrome,
    countdown_sleep,
                resolve_profile_path,
    get_available_profiles,
    record_profile_cooldown,
    is_profile_in_cooldown,
    get_next_available_pro_profile
)
from src.srt_manager import split_srt_file, merge_numbered_srt_files, process_srt_speed


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

    # --- PRE-CHECK: Lọc các file đã dịch hoàn chỉnh ---
    pending_files = []
    for file_name in srt_files:
        cn_file_path = os.path.join(cn_folder, file_name)
        if not find_existing_translated_file(cn_file_path, vi_folder):
            pending_files.append(file_name)
    
    if not pending_files:
        log_callback("⏩ TẤT CẢ các tệp đã được dịch và đồng bộ Timecode hoàn tất từ trước. BỎ QUA bước mở trình duyệt AI!", "success")
        return
    
    if len(pending_files) < len(srt_files):
        log_callback(f"⚠️ Đã lọc bỏ các file hoàn thành. Chỉ tiến hành mở trình duyệt và dịch bù {len(pending_files)} tệp còn thiếu...", "info")
    
    srt_files = pending_files
    # ---------------------------------------------------

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

    from src.gemini_bot import GeminiBot

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
        if model_status == "FLASH_LITE":
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
                    log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG! 🚀", "success")

        files_translated_in_session = 0
        BATCH_SIZE = 3

        total_files_for_progress = len(srt_files)
        for idx_prog, file_name in enumerate(srt_files):
            if progress_callback:
                progress_callback(int((idx_prog / total_files_for_progress) * 100), f"Đang xử lý tệp {idx_prog+1}/{total_files_for_progress}...")

            cn_file_path = os.path.join(cn_folder, file_name)

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

            existing_final = find_existing_translated_file(cn_file_path, vi_folder)
            if existing_final:
                log_callback(f"⏩ Tệp '{file_name}' đã được dịch hoàn tất từ trước tại [{os.path.basename(existing_final)}]. TỰ ĐỘNG BỎ QUA TIẾP TỤC TỆP TIẾP THEO!", "success")
                continue

            total_blocks = count_srt_blocks(cn_file_path)
            log_callback(f"\n--- 🎬 Đang xử lý tệp phụ đề: {file_name} ({total_blocks} block) ---")

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
                existing_part = find_existing_translated_file(part_cn_path, vi_folder)
                if existing_part:
                    log_callback(f"⏩ [CHECKPOINT] Phân đoạn '{part_label}' đã dịch hoàn tất trước đó tại [{os.path.basename(existing_part)}]. BỎ QUA CHUYỂN SANG TỆP TIẾP THEO!", "info")
                    continue

                status, model_name = bot.check_model_status()
                if status == "FLASH_LITE":
                    log_callback(f"⚠️ Profile [{current_profile}] bị hạ cấp xuống Flash-Lite (Hết hạn mức Pro trong ngày)!", "warning")
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
                            log_callback(f"✅ ĐÃ CHUYỂN SANG PROFILE [{current_profile}] THÀNH CÔNG! Tiếp tục duy trì Mô hình Pro 🚀", "success")

                    if not switched_ok:
                        log_callback("🛑 TẤT CẢ CÁC PROFILE CHROME ĐỀU ĐÃ BỊ KHÓA HẠN MỨC PRO (5 TIẾNG)! Tạm dừng 60 phút chờ Gemini reset...")
                        countdown_sleep(3600, log_callback, "⏳ Đang chờ hồi phục:", check_pause_callback=check_pause_callback)
                        page.goto("https://gemini.google.com/app", timeout=60000)
                        page.wait_for_load_state("load")
                        time.sleep(5)
                        bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)

                if files_translated_in_session >= BATCH_SIZE:
                    log_callback(f"\n🔄 [HỆ THỐNG] Đã hoàn thành mẻ {BATCH_SIZE} tệp. Làm mới phiên chat...")
                    try:
                        page.goto("https://gemini.google.com/app", timeout=45000)
                        page.wait_for_load_state("load")
                        time.sleep(4)
                        bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                        files_translated_in_session = 0
                    except Exception as e:
                        log_callback(f"⚠️ Cảnh báo làm mới: {e}")

                log_callback(f"🔹 Đang dịch tệp: {part_label}...")
                
                MAX_RETRIES = 3
                part_ok = False
                short_prompt = (
                    "Hãy dịch toàn bộ nội dung file SRT đính kèm sang Tiếng Việt chuẩn văn phong phim.\\n"
                    "NHẮC LẠI LUẬT BẮT BUỘC:\\n"
                    "- Giữ nguyên 100% cấu trúc ID và mốc thời gian (Timeline).\\n"
                    "- CHỈ trả về duy nhất nội dung file SRT đã dịch và BẮT BUỘC đặt trong khối code block markdown (```srt\\n...\\n```).\\n"
                    "- Không thêm bất kỳ câu chào hay lời giải thích nào ngoài khối code block."
                )

                for attempt in range(1, MAX_RETRIES + 1):
                    if attempt > 1:
                        log_callback(f"\n♻️ TIẾN HÀNH DỊCH LẠI {part_label} (Lần thử: {attempt}/{MAX_RETRIES})...")

                    initial_count = bot.upload_file_and_send(part_cn_path, short_prompt)
                    if initial_count is None:
                        log_callback("❌ Gửi prompt thất bại. Tải lại trang...")
                        page.goto("https://gemini.google.com/app", timeout=45000)
                        page.wait_for_load_state("load")
                        time.sleep(4)
                        bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                        continue

                    bot.wait_for_response(initial_count, wait_time, check_pause_callback=check_pause_callback)
                    latest_response = bot.get_latest_response()
                    
                    if latest_response:
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
                            if os.path.exists(part_vi_path): os.remove(part_vi_path)
                            page.goto("https://gemini.google.com/app", timeout=45000)
                            page.wait_for_load_state("load")
                            time.sleep(4)
                            bot.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
                            time.sleep(3)

                if part_ok:
                    files_translated_in_session += 1
                    countdown_sleep(delay_time, log_callback, "☕ Đang nghỉ:", check_pause_callback=check_pause_callback)
                else:
                    all_targets_ok = False
                    log_callback(f"❌ Thất bại khi dịch phần {part_label}.")
                    break

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
