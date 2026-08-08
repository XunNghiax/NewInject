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

    # 1. Trích xuất TẤT CẢ nội dung nằm bên trong các code block ```srt ... ``` (gộp lại nếu Gemini bị tách thành nhiều code block)
    code_blocks = re.findall(r'```(?:srt)?\s*\n(.*?)\n\s*```', text, re.DOTALL | re.IGNORECASE)
    if code_blocks:
        text = "\n\n".join(b.strip() for b in code_blocks if b.strip())
    else:
        # Nếu không có thẻ đóng, xóa các dòng ``` ở đầu/cuối
        text = re.sub(r'^```[a-zA-Z]*\n', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n```$', '', text, flags=re.MULTILINE)
        text = text.replace('```', '')

    # 2. Định vị từ vị trí Block đầu tiên
    match = re.search(r'(?:\n|^)(\d+\s*\n\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->)', text)
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


def is_profile_logged_in(profile_folder: str) -> bool:
    """
    Kiểm tra xem thư mục Profile Chrome đã chứa dữ liệu phiên đăng nhập/cookie hay chưa.
    """
    target_dir = resolve_profile_path(profile_folder)
    if not os.path.exists(target_dir):
        return False
    
    indicators = [
        os.path.join(target_dir, "Default"),
        os.path.join(target_dir, "Preferences"),
        os.path.join(target_dir, "Default", "Preferences"),
        os.path.join(target_dir, "Default", "Cookies"),
        os.path.join(target_dir, "Default", "Network", "Cookies")
    ]
    for ind in indicators:
        if os.path.exists(ind):
            return True
            
    try:
        entries = os.listdir(target_dir)
        if len(entries) >= 2:
            return True
    except Exception:
        pass

    return False


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


def create_new_profile() -> str:
    """Tạo một thư mục Profile Chrome mới theo thứ tự chrome_data_N."""
    existing = get_available_profiles()
    max_num = 0
    for p in existing:
        nums = re.findall(r'\d+', p)
        if nums:
            max_num = max(max_num, int(nums[0]))
    new_num = max_num + 1
    new_profile_name = f"chrome_data_{new_num}"
    new_path = os.path.join("user_data", "chrome_profiles", new_profile_name)
    os.makedirs(new_path, exist_ok=True)
    return new_profile_name


def open_chrome_for_login(profile_folder: str = "chrome_data_1", log_callback: Callable = print) -> bool:
    """Mở trình duyệt Chrome cho một Profile cụ thể để người dùng thực hiện đăng nhập Google/Gemini."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log_callback("❌ Thư viện Playwright chưa được cài đặt! Vui lòng chạy 'pip install playwright'.", "error")
        return False

    target_dir = resolve_profile_path(profile_folder)
    os.makedirs(target_dir, exist_ok=True)
    log_callback(f"🌐 Đang khởi động Chrome cho Profile [{profile_folder}]...")
    log_callback(f"📁 Đường dẫn dữ liệu: {target_dir}")
    log_callback("💡 Hướng dẫn: Đăng nhập tài khoản Google trên cửa sổ Chrome vừa mở. Sau khi đăng nhập xong, hãy ĐÓNG CỬA SỔ CHROME để lưu Profile.", "info")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=target_dir,
                headless=False,
                channel="chrome",
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            try:
                page.goto("https://gemini.google.com/app", timeout=60000)
            except Exception as nav_e:
                log_callback(f"⚠️ Không thể tự động mở trang Gemini: {nav_e}", "warning")

            is_closed = [False]
            def on_close(ctx):
                is_closed[0] = True

            try:
                browser.on("close", on_close)
            except Exception:
                pass

            while not is_closed[0]:
                try:
                    pages = browser.pages
                    if not pages or len(pages) == 0:
                        break
                except Exception:
                    break
                time.sleep(0.5)

        log_callback(f"✅ Đã đóng trình duyệt và lưu thành công Profile [{profile_folder}]!", "success")
        return True
    except Exception as e:
        err_msg = str(e)
        if any(term in err_msg.lower() for term in ["closed", "epipe", "broken pipe", "target page", "context"]):
            log_callback(f"✅ Đã đóng trình duyệt và lưu thành công Profile [{profile_folder}]!", "success")
            return True
        log_callback(f"❌ Lỗi khi thao tác Profile [{profile_folder}]: {e}", "error")
        return False


# ==============================================================================
# QUẢN LÝ THỜI GIAN KHÓA 5 TIẾNG KHI HẾT HẠN MỨC PRO (5-HOUR COOLDOWN TRACKER)
# ==============================================================================
COOLDOWN_FILE = os.path.join("user_data", "config", "profile_cooldowns.json")

def record_profile_cooldown(profile_name: str, cooldown_hours: float = 5.0, log_callback: Callable = print):
    """Lưu thời gian khóa 5 tiếng cho Profile Chrome bị hết ngạch Pro."""
    try:
        os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
        cooldowns = {}
        if os.path.exists(COOLDOWN_FILE):
            try:
                import json
                with open(COOLDOWN_FILE, "r", encoding="utf-8") as f:
                    cooldowns = json.load(f)
            except Exception:
                cooldowns = {}
        
        expire_time = time.time() + (cooldown_hours * 3600)
        from datetime import datetime
        expire_str = datetime.fromtimestamp(expire_time).strftime("%H:%M:%S %d/%m/%Y")
        cooldowns[profile_name] = expire_time
        
        import json
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(cooldowns, f, ensure_ascii=False, indent=2)
            
        log_callback(f"🔒 Profile [{profile_name}] hết ngạch Pro. Đã lưu thời gian chờ reset 5 tiếng (Khôi phục vào: {expire_str})", "warning")
    except Exception as e:
        log_callback(f"⚠️ Lỗi khi lưu thời gian khóa Profile: {e}", "warning")


def is_profile_in_cooldown(profile_name: str) -> tuple:
    """
    Kiểm tra Profile có đang trong thời gian chờ 5 tiếng không.
    Trả về (is_in_cooldown, remaining_str, expire_timestamp).
    """
    if not os.path.exists(COOLDOWN_FILE):
        return False, "", 0.0
    try:
        import json
        with open(COOLDOWN_FILE, "r", encoding="utf-8") as f:
            cooldowns = json.load(f)
        expire_time = cooldowns.get(profile_name, 0.0)
        if expire_time:
            now = time.time()
            if now < expire_time:
                remaining_sec = int(expire_time - now)
                hours = remaining_sec // 3600
                mins = (remaining_sec % 3600) // 60
                rem_str = f"{hours}h {mins}m" if hours > 0 else f"{mins} phút"
                return True, rem_str, expire_time
            else:
                del cooldowns[profile_name]
                with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
                    json.dump(cooldowns, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return False, "", 0.0


def get_next_available_pro_profile(current_profile: str, available_profiles: list, log_callback: Callable = print) -> Optional[str]:
    """
    Tự động tìm Profile tiếp theo CHƯA BỊ KHÓA 5 TIẾNG.
    Bỏ qua hoàn toàn các Profile đang trong thời gian chờ reset 5 tiếng mà không cần mở trình duyệt.
    """
    if not available_profiles:
        return None
    
    start_idx = available_profiles.index(current_profile) if current_profile in available_profiles else 0
    num_p = len(available_profiles)
    
    for step in range(1, num_p + 1):
        idx = (start_idx + step) % num_p
        candidate = available_profiles[idx]
        if candidate == current_profile:
            continue
        in_cd, rem_str, _ = is_profile_in_cooldown(candidate)
        if in_cd:
            log_callback(f"⏩ [BỎ QUA PROFILE KHÓA 5H] Profile [{candidate}] đang chờ reset Pro (Còn {rem_str}). Tự động bỏ qua!", "warning")
            continue
        return candidate
        
    return None



