import os
import re
import time
import platform
import subprocess
from typing import Callable, Dict, Any, Optional


def clean_gemini_output(text: str) -> str:
    """Làm sạch kết quả đầu ra từ Gemini AI, trích xuất duy nhất nội dung trong code block SRT."""
    if not text:
        return ""

    # 1. Ưu tiên trích xuất nội dung nằm bên trong code block ```srt ... ```
    code_block_match = re.search(r'```(?:srt)?\s*\n(.*?)\n\s*```', text, re.DOTALL | re.IGNORECASE)
    if code_block_match:
        text = code_block_match.group(1).strip()
    else:
        # Nếu không có thẻ đóng, xóa các dòng ``` ở đầu/cuối
        text = re.sub(r'^```[a-zA-Z]*\n', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n```$', '', text, flags=re.MULTILINE)
        text = text.replace('```', '')

    # 2. Định vị từ vị trí Block 1 (1 \n HH:MM:SS,ms --> ...)
    match = re.search(r'(?:\n|^)(1\s*\n\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->)', text)
    if match:
        text = text[match.start():].strip()

    lines = text.split('\n')
    while lines:
        first_line = lines[0].strip().lower()
        if first_line.startswith('```') or first_line.startswith('dưới đây') or first_line.startswith('đây là') or first_line == '':
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


def force_kill_chrome(log_callback: Callable = print):
    """Giữ nguyên các tiến trình Chrome hiện tại và nạp trực tiếp Profile người dùng."""
    log_callback("🌐 Khởi động Playwright với Profile Chrome lưu sẵn (Giữ nguyên các cửa sổ Chrome hiện tại)...")


def countdown_sleep(seconds: int, log_callback: Callable = print, message_prefix: str = "⏳ Còn khoảng", check_pause_callback: Optional[Callable] = None):
    """Hàm đếm ngược thời gian nghỉ giữa các file hỗ trợ tạm dừng real-time."""
    for remaining in range(seconds, 0, -1):
        if check_pause_callback:
            check_pause_callback()
        if remaining % 5 == 0 or remaining <= 5:
            log_callback(f"   {message_prefix} {remaining} giây...")
        time.sleep(1)


def parse_srt_structure(filepath: str) -> Dict[str, str]:
    """Đọc file SRT và trích xuất cấu trúc Block ID và Timeline chính xác."""
    structure = {}
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read().replace('\r\n', '\n').strip()
            
        pattern = r'(?m)^(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3})'
        matches = re.findall(pattern, content)
        for block_id, timeline in matches:
            structure[block_id.strip()] = timeline.strip()
    except Exception:
        pass
    return structure


def is_srt_structure_match(file_a: str, file_b: str, log_callback: Callable = print) -> bool:
    """So sánh cấu trúc 2 file SRT. Trả về True nếu khớp, False nếu sai lệch."""
    struct_a = parse_srt_structure(file_a)
    struct_b = parse_srt_structure(file_b)
    keys_a = sorted(struct_a.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    keys_b = sorted(struct_b.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    if not keys_a or not keys_b:
        return False
    return keys_a == keys_b


def get_matched_blocks_count(cn_path: str, vi_path: str) -> int:
    """Đếm số lượng block SRT tính từ đầu khớp mốc thời gian hoàn toàn giữa 2 file."""
    try:
        struct_cn = parse_srt_structure(cn_path)
        struct_vi = parse_srt_structure(vi_path)
        
        cn_keys = sorted(struct_cn.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        vi_keys = sorted(struct_vi.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        
        if not cn_keys or not vi_keys:
            return 0

        match_count = 0
        for k_cn, k_vi in zip(cn_keys, vi_keys):
            if k_cn == k_vi and struct_cn[k_cn] == struct_vi.get(k_vi):
                match_count += 1
            else:
                break
        return match_count
    except Exception:
        return 0


def resolve_profile_path(profile_folder: str) -> str:
    """
    Trả về đường dẫn thực tế của thư mục Profile Chrome.
    Hỗ trợ tìm kiếm ở các vị trí:
    1. Đường dẫn tuyệt đối
    2. ./<profile_folder> (Thư mục gốc project)
    3. ./user_data/<profile_folder>
    4. ./user_data/chrome_profiles/<profile_folder>
    """
    if not profile_folder:
        profile_folder = "chrome_data_1"

    if os.path.isabs(profile_folder) and os.path.exists(profile_folder):
        return profile_folder

    candidates = [
        os.path.abspath(profile_folder),
        os.path.abspath(os.path.join("user_data", profile_folder)),
        os.path.abspath(os.path.join("user_data", "chrome_profiles", profile_folder)),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    return os.path.abspath(profile_folder)


def get_available_profiles() -> list:
    """Tự động phát hiện tất cả các thư mục Profile Chrome đã tạo."""
    profiles = []
    
    # 1. Quét thư mục user_data/chrome_profiles/
    cp_dir = os.path.join("user_data", "chrome_profiles")
    if os.path.exists(cp_dir):
        for entry in os.listdir(cp_dir):
            full_p = os.path.join(cp_dir, entry)
            if os.path.isdir(full_p) and entry not in profiles:
                profiles.append(entry)

    # 2. Quét thư mục gốc project và user_data/
    for i in range(1, 30):
        p_dir = f"chrome_data_{i}"
        if os.path.exists(p_dir) and p_dir not in profiles:
            profiles.append(p_dir)
        u_p = os.path.join("user_data", p_dir)
        if os.path.exists(u_p) and p_dir not in profiles:
            profiles.append(p_dir)

    if not profiles:
        profiles = ["chrome_data_1", "chrome_data_2"]
        os.makedirs(os.path.join("user_data", "chrome_profiles", "chrome_data_1"), exist_ok=True)
        os.makedirs(os.path.join("user_data", "chrome_profiles", "chrome_data_2"), exist_ok=True)

    def sort_key(name):
        nums = re.findall(r'\d+', name)
        return int(nums[0]) if nums else 999
    profiles.sort(key=sort_key)
    return profiles

