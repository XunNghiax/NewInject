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
