import os
import time
import re
from typing import Callable, Optional, Tuple, Dict, Any
from playwright.sync_api import Page, Locator

class GeminiBot:
    """
    Core Automation Class (Lõi Tự động hóa) đóng gói toàn bộ logic Playwright 
    để tương tác với giao diện Web của Google Gemini.
    """
    
    # --- CSS SELECTORS (Quản lý tập trung ở đây để dễ bảo trì) ---
    CHAT_BOX_LOCATOR = 'div[contenteditable="true"]'
    RESPONSE_TEXT_LOCATOR = '.model-response-text'
    SEND_BTN_LOCATORS = [
        'button[aria-label*="Gửi"]', 'button[aria-label*="gửi"]', 
        'button[aria-label*="Send"]', 'button[aria-label*="send"]',
        'button[mattooltip*="Gửi"]', 'button[mattooltip*="Send"]',
        '[data-testid="send-button"]'
    ]
    MODEL_PICKER_LOCATORS = [
        'bard-mode-switcher', 'gmp-model-picker', '[aria-label*="model" i]',
        '[aria-label*="gemini" i]', '[aria-label*="chế độ" i]', '.model-picker-button',
        'button', '[role="button"]', '[role="combobox"]', 'mat-select', '.model-title'
    ]
    DOWNGRADE_KEYWORDS = ["flash-lite", "flash lite", "fast", "hạ cấp", "giới hạn"]
    UPGRADE_KEYWORDS = ["advanced", "pro"]

    def __init__(self, log_callback: Callable = print):
        self.log_callback = log_callback
        self.browser = None
        self.page: Optional[Page] = None
        self.status = "UNKNOWN"
        self.model_name = "UNKNOWN"

    def log(self, msg: str, level="info"):
        # Phân biệt level cho log_callback nếu nó hỗ trợ 2 tham số
        try:
            self.log_callback(msg, level)
        except TypeError:
            self.log_callback(msg)

    def highlight_element(self, element_locator: Locator, border="4px solid red", bg="yellow", sleep_time=1):
        """Hàm giúp bôi màu phần tử trên trình duyệt để dễ quan sát"""
        try:
            self.page.evaluate(
                f"(element) => {{ element.style.border = '{border}'; element.style.backgroundColor = '{bg}'; }}", 
                element_locator.element_handle()
            )
            time.sleep(sleep_time)
        except Exception:
            pass

    def launch(self, p, profile_folder: str, prompt_file: Optional[str] = None, check_pause_callback: Optional[Callable] = None) -> Tuple[Any, Page, str]:
        """Tạo phiên làm việc trình duyệt mới và KIỂM TRA MÔ HÌNH TRƯỚC KHI GỬI PROMPT."""
        target_dir = resolve_profile_path(profile_folder)
        self.log(f"🌐 Đang mở trình duyệt với Profile: [{profile_folder}] ({target_dir})...")
        os.makedirs(target_dir, exist_ok=True)
        
        self.browser = p.chromium.launch_persistent_context(
            user_data_dir=target_dir,
            headless=False,
            channel="chrome",
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
        )
        self.page = self.browser.new_page()
        self.page.goto("https://gemini.google.com/app", timeout=60000)
        self.page.wait_for_load_state("load")

        self.log(f"⏳ Đang kiểm tra phiên bản Gemini AI trên Profile [{profile_folder}]...")
        time.sleep(3)
        
        self.status, self.model_name = self.check_model_status()
        if self.status == "FLASH_LITE":
            self.log(f"⚠️ CẢNH BÁO TRƯỚC KHI GỬI PROMPT: Profile [{profile_folder}] bị hạ cấp ({self.model_name})!", "warning")
            return self.browser, self.page, self.status

        self.log(f"✅ XÁC NHẬN MÔ HÌNH HỢP LỆ: {self.model_name} 🚀 -> Tiến hành nạp Prompt ...", "success")
        if prompt_file:
            self.send_initial_prompt(prompt_file, check_pause_callback=check_pause_callback)
            
        return self.browser, self.page, self.status

    def check_model_status(self) -> Tuple[str, str]:
        try:
            self.page.wait_for_timeout(1500)
            found_model_text = ""
            for sel in self.MODEL_PICKER_LOCATORS:
                try:
                    elements = self.page.locator(sel).all()
                    for el in elements:
                        if el.is_visible():
                            txt = el.inner_text().strip()
                            if any(kw in txt.lower() for kw in ["gemini", "pro", "flash", "advanced", "lite", "fast", "1.5"]):
                                found_model_text = txt
                                if any(down in txt.lower() for down in self.DOWNGRADE_KEYWORDS):
                                    if "advanced" not in txt.lower() and "pro" not in txt.lower():
                                        return "FLASH_LITE", f"Đã hạ cấp: {txt}"
                except Exception:
                    pass

            content_lower = self.page.content().lower()
            if any(msg in content_lower for msg in [
                "flash-lite", "flash lite", "đạt đến giới hạn", "reached your limit",
                "chuyển sang flash", "switched to flash", "hết lượt dùng pro"
            ]):
                return "FLASH_LITE", "Phát hiện thông báo hết hạn ngạch Gemini Pro (Chuyển Flash-Lite)"

            if found_model_text:
                return "NORMAL", found_model_text

        except Exception as e:
            self.log(f"⚠️ Cảnh báo khi kiểm tra model: {e}", "warning")

        return "NORMAL", "Gemini Pro/Advanced"

    def send_initial_prompt(self, prompt_file_path: str, check_pause_callback: Optional[Callable] = None):
        initial_count = self.page.locator(self.RESPONSE_TEXT_LOCATOR).count()
        if os.path.exists(prompt_file_path):
            try:
                with open(prompt_file_path, 'r', encoding='utf-8') as f:
                    prompt_content = f.read().strip()
                self.log(f"📜 Đang nạp Prompt từ: {prompt_file_path}...")
                
                chat_box = self.page.locator(self.CHAT_BOX_LOCATOR).first
                chat_box.wait_for(state="visible", timeout=15000)
                chat_box.click()
                chat_box.fill(prompt_content)
                time.sleep(1)
                
                chat_box.focus()
                self.page.keyboard.press("Control+Enter")
                time.sleep(0.5)
                self.page.keyboard.press("Enter")
                time.sleep(1.5)
                
                clicked_send = False
                for sel in self.SEND_BTN_LOCATORS:
                    btn = self.page.locator(sel).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        self.log("🚀 Đã nạp Prompt thành công.")
                        clicked_send = True
                        break
                        
                if not clicked_send:
                    chat_container = self.page.locator('div').filter(has=chat_box).last
                    last_btn = chat_container.locator('button').last
                    if last_btn.is_visible(timeout=1000):
                        last_btn.click()
                        self.log("🚀 Đã nạp Prompt dịch thuật thành công.")
                        
            except Exception as e:
                self.log(f"⚠️ Cảnh báo nạp Prompt: {e}", "warning")

            self.wait_for_response(initial_count, 60, check_pause_callback=check_pause_callback)
        else:
            self.log("⚠️ Không tìm thấy file prompt, sử dụng luật dịch mặc định.", "warning")

    def wait_for_response(self, initial_count: int, max_wait_time: int, check_pause_callback: Optional[Callable] = None) -> bool:
        self.log(f"👀 Đang giám sát Gemini AI trả lời (Tối đa {max_wait_time}s)...")
        previous_text = ""
        stable_count = 0
        start_time = time.time()
        
        while time.time() - start_time < max_wait_time:
            if check_pause_callback:
                check_pause_callback()
            time.sleep(2)
            current_count = self.page.locator(self.RESPONSE_TEXT_LOCATOR).count()
            if current_count > initial_count:
                responses = self.page.locator(self.RESPONSE_TEXT_LOCATOR).all_inner_texts()
                if responses:
                    current_text = responses[-1].strip()
                    if current_text == previous_text and len(current_text) > 10:
                        stable_count += 1
                        if stable_count >= 3:
                            self.log("✓ Gemini đã trả lời xong hoàn toàn!")
                            return True
                    else:
                        stable_count = 0
                        previous_text = current_text
        self.log("⏳ Đã hết thời gian chờ, lấy kết quả hiện tại...")
        return True

    def upload_file_and_send(self, file_path: str, short_prompt: str) -> Optional[int]:
        initial_count = self.page.locator(self.RESPONSE_TEXT_LOCATOR).count()
        uploaded = False
        
        self.log(f"🔍 Đang chuẩn bị truyền file: {os.path.basename(file_path)}...")
        
        # 1. Thử Upload File
        try:
            file_inputs = self.page.locator('input[type="file"]').all()
            for inp in file_inputs:
                try:
                    inp.set_files(os.path.abspath(file_path))
                    uploaded = True
                    self.log(f"📎 Đã đính kèm tệp {os.path.basename(file_path)} thành công qua input element!")
                    break
                except Exception:
                    pass
        except Exception:
            pass

        if not uploaded:
            try:
                with self.page.expect_file_chooser(timeout=10000) as fc_info:
                    chat_box = self.page.locator(self.CHAT_BOX_LOCATOR).first
                    chat_box.wait_for(state="visible", timeout=3000)
                    box = chat_box.bounding_box()
                    if box:
                        buttons = self.page.locator('button').all()
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
                            self.log("🔎 Phát hiện nút Thêm File (+), chuẩn bị click...")
                            self.highlight_element(target_btn)
                            target_btn.click()
                        else:
                            alt_btn = self.page.locator('button:left-of(div[contenteditable="true"])').first
                            self.highlight_element(alt_btn)
                            alt_btn.click(timeout=2000)
                        
                        time.sleep(1)
                        menu_items = self.page.locator('[role="menuitem"], [role="button"], mat-list-item').all()
                        for item in menu_items:
                            if item.is_visible():
                                t = item.inner_text().lower()
                                if "xuống" not in t and "drive" not in t and any(k in t for k in ["tải", "tệp", "máy tính", "computer", "upload", "file"]):
                                    self.log("🔎 Chọn menu Tải lên từ máy tính...")
                                    self.highlight_element(item, border="3px solid blue", bg="#e3f2fd", sleep_time=1)
                                    item.click()
                                    break

                file_chooser = fc_info.value
                file_chooser.set_files(os.path.abspath(file_path))
                uploaded = True
                self.log(f"📎 Đã nạp file qua File Chooser thành công!")
            except Exception:
                self.log("⚠️ Tự động chuyển sang chế độ Dán Trực Tiếp Văn Bản (Fallback)...", "warning")

        # 2. Đọc nội dung
        file_text = ""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                file_text = f.read().strip()
        except Exception as e:
            self.log(f"❌ Không thể đọc nội dung file: {e}", "error")
            return None

        # 3. Gửi tin nhắn
        try:
            chat_box = self.page.locator(self.CHAT_BOX_LOCATOR).first
            self.highlight_element(chat_box, border="4px solid green", bg="#e8f5e9", sleep_time=0.5)
            chat_box.click()
            time.sleep(0.5)

            if uploaded:
                full_text = short_prompt
            else:
                file_title = os.path.splitext(os.path.basename(file_path))[0]
                full_text = (
                    f"{short_prompt}\\n\\n"
                    f"[TÊN FILE GỐC]: {file_title}\\n\\n"
                    f"[DƯỚI ĐÂY LÀ NỘI DUNG FILE CẦN XỬ LÝ]:\\n\\n{file_text}"
                )

            try:
                chat_box.fill(full_text, timeout=10000)
            except Exception:
                self.page.evaluate("""
                    ([el, text]) => {
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                """, [chat_box.element_handle(), full_text])
            time.sleep(1)
            
            chat_box.focus()
            self.page.keyboard.press("Control+Enter")
            time.sleep(0.5)
            self.page.keyboard.press("Enter")
            time.sleep(1)
            
            send_selectors = [
                'button[aria-label*="gửi tin nhắn" i]', 
                'button[aria-label*="send message" i]',
                'button[aria-label*="gửi" i]:not([aria-label*="phản hồi"])',
                'button[aria-label*="send" i]:not([aria-label*="feedback"])',
                '[data-testid="send-button"]',
                'button:has(svg):right-of(div[contenteditable="true"])'
            ]
            
            clicked_send = False
            for _ in range(3):
                for sel in send_selectors:
                    elements = self.page.locator(sel).all()
                    for el in elements:
                        if el.is_visible() and el.is_enabled():
                            self.log(f"🔎 Đã tìm thấy nút Gửi qua selector: {sel}")
                            self.highlight_element(el, border="5px dashed #6200ea", bg="#b388ff", sleep_time=1.5)
                            el.click(timeout=1500)
                            clicked_send = True
                            break
                    if clicked_send: break
                if clicked_send: break
                time.sleep(1.5)
                
            if not clicked_send:
                self.log("⚠️ Không tìm thấy nút Gửi bằng selector chuẩn, thử click nút cuối cùng...")
                chat_container = self.page.locator('div').filter(has=chat_box).last
                all_btns = chat_container.locator('button').all()
                for btn in reversed(all_btns):
                    if btn.is_visible(timeout=500):
                        aria = btn.get_attribute("aria-label") or ""
                        if "xóa" not in aria.lower() and "remove" not in aria.lower() and "micro" not in aria.lower():
                            self.highlight_element(btn, border="5px dashed #00c853", bg="#b2ff59", sleep_time=1.5)
                            btn.click()
                            clicked_send = True
                            break

            self.log("🚀 Đã gửi file thành công!")
            return initial_count
            
        except Exception as e:
            self.log(f"❌ Lỗi khi gửi file: {e}", "error")
            return None

    def get_latest_response(self) -> str:
        responses = self.page.locator(self.RESPONSE_TEXT_LOCATOR).all_inner_texts()
        if responses:
            return responses[-1]
        return ""


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



