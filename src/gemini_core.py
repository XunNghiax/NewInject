import os
import re
import time
import platform
import subprocess
from typing import Callable, Dict, Any


def clean_gemini_output(text: str) -> str:
    """Làm sạch kết quả đầu ra từ Gemini AI (loại bỏ markdown block ``` và comment rác)."""
    if not text:
        return ""
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


def force_kill_chrome(log_callback: Callable = print):
    """Giữ nguyên các tiến trình Chrome hiện tại và nạp trực tiếp Profile người dùng."""
    log_callback("🌐 Khởi động Playwright với Profile Chrome lưu sẵn (Giữ nguyên các cửa sổ Chrome hiện tại)...")


def countdown_sleep(seconds: int, log_callback: Callable = print, message_prefix: str = "⏳ Còn khoảng"):
    """Hàm đếm ngược thời gian nghỉ giữa các file."""
    for remaining in range(seconds, 0, -5):
        if remaining > 5:
            log_callback(f"   {message_prefix} {remaining} giây...")
            time.sleep(5)
        else:
            log_callback(f"   {message_prefix} {remaining} giây...")
            time.sleep(remaining)


def parse_srt_structure(filepath: str) -> Dict[str, str]:
    """Đọc file SRT và trích xuất cấu trúc Block ID và Timeline."""
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


def is_srt_structure_match(file_a: str, file_b: str, log_callback: Callable = print) -> bool:
    """So sánh cấu trúc 2 file SRT. Trả về True nếu khớp, False nếu sai lệch."""
    struct_a = parse_srt_structure(file_a)
    struct_b = parse_srt_structure(file_b)
    keys_a = sorted(struct_a.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    keys_b = sorted(struct_b.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    return keys_a == keys_b
