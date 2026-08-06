import os
import re
import sys
import glob
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional, Dict, Any

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

FFMPEG_DIR = None
try:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if exe and os.path.exists(exe):
        bin_dir = os.path.dirname(exe)
        target_ffmpeg = os.path.join(bin_dir, "ffmpeg.exe")
        if not os.path.exists(target_ffmpeg):
            shutil.copy2(exe, target_ffmpeg)
        FFMPEG_DIR = bin_dir
except Exception:
    FFMPEG_DIR = None


class BilibiliDownloader:
    """
    Modul riêng biệt quản lý việc tải video từ Bilibili.com 
    - Tối ưu hóa tốc độ tải với multi-threading/aria2c
    - Tự động gộp FFmpeg & dọn dẹp các file phân đoạn tạm (.f*.mp4, .f*.m4a)
    - Hỗ trợ mã hóa Tiếng Trung Unicode UTF-8 chuẩn
    - Tự động nạp file cookies.txt VIP
    """

    def __init__(
        self, 
        output_dir: str = "./downloads", 
        log_callback: Optional[Callable[[str, str], None]] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.log_fn = log_callback if log_callback else (lambda msg, lvl="info": print(f"[{lvl.upper()}] {msg}"))
        self.progress_fn = progress_callback if progress_callback else (lambda pct, status: None)
        self._is_cancelled = False

    def cancel(self):
        """Đánh dấu hủy tiến trình tải"""
        self._is_cancelled = True

    @staticmethod
    def extract_bilibili_url(text: str) -> Optional[str]:
        if not text:
            return None
        text = text.strip()

        pattern = r"(https?://(?:www\.|m\.)?(?:bilibili\.com/video/[a-zA-Z0-9]+|b23\.tv/[a-zA-Z0-9]+)[^\s]*)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)

        bv_match = re.search(r"(BV[a-zA-Z0-9]{10})", text)
        if bv_match:
            return f"https://www.bilibili.com/video/{bv_match.group(1)}"

        if "bilibili.com" in text or "b23.tv" in text:
            url_match = re.search(r"(https?://[^\s]+)", text)
            if url_match:
                return url_match.group(1)

        return None

    @staticmethod
    def is_valid_bilibili_url(url: str) -> bool:
        return BilibiliDownloader.extract_bilibili_url(url) is not None

    def cleanup_temp_fragments(self):
        """Tự động dọn dẹp tất cả các tệp tạm dạng .f100026.mp4, .f30280.m4a sau khi gộp"""
        try:
            pattern = os.path.join(self.output_dir, "*.f[0-9]*.*")
            temp_files = glob.glob(pattern)
            for tf in temp_files:
                if os.path.exists(tf):
                    os.remove(tf)
        except Exception:
            pass

    def _ytdlp_progress_hook(self, d: Dict[str, Any]):
        if self._is_cancelled:
            raise Exception("Người dùng đã hủy tiến trình tải xuống!")

        status = d.get("status")
        if status == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded_bytes = d.get("downloaded_bytes", 0)
            
            if total_bytes > 0:
                percentage = int((downloaded_bytes / total_bytes) * 100)
            else:
                percentage = 0

            speed_str = d.get("_speed_str", "N/A").strip()
            eta_str = d.get("_eta_str", "N/A").strip()
            
            status_text = f"Đang tải video Bilibili... [{percentage}%] - ⚡ Tốc độ: {speed_str} - ⏳ Còn lại: {eta_str}"
            self.progress_fn(percentage, status_text)

        elif status == "finished":
            self.log_fn("✓ Đã tải xong các phân đoạn. Đang gộp file MP4...", "info")
            self.progress_fn(98, "Đang xử lý gộp file MP4 chất lượng cao...")

    def download(self, raw_input: str) -> Dict[str, Any]:
        self._is_cancelled = False
        clean_url = self.extract_bilibili_url(raw_input)

        if not clean_url:
            err_msg = "Không tìm thấy URL Bilibili hợp lệ trong nội dung dán!"
            self.log_fn(f"❌ {err_msg}", "error")
            return {"success": False, "error": err_msg}

        self.log_fn(f"🚀 Bắt đầu tiến trình tải Bilibili: {clean_url}", "info")
        self.progress_fn(1, "Khởi tạo kết nối tới server Bilibili...")

        res = None
        if yt_dlp is not None:
            res = self._download_via_ytdlp_python(clean_url)
        else:
            res = self._download_via_subprocess(clean_url)

        # Dọn dẹp tệp phân đoạn tạm sau khi tải thành công
        self.cleanup_temp_fragments()
        return res

    def _download_via_ytdlp_python(self, url: str) -> Dict[str, Any]:
        """Sử dụng trực tiếp Python API của yt-dlp với mã hóa UTF-8 chuẩn"""
        out_template = os.path.join(self.output_dir, "%(title)s.%(ext)s")
        
        base_ydl_opts = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": out_template,
            "http_headers": {
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
            "progress_hooks": [self._ytdlp_progress_hook],
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 8,
        }

        if FFMPEG_DIR and os.path.exists(FFMPEG_DIR):
            base_ydl_opts["ffmpeg_location"] = FFMPEG_DIR

        custom_cookie_files = ["./cookies.txt", "./downloads/cookies.txt"]
        found_cookie_file = None
        for cf in custom_cookie_files:
            if os.path.exists(cf):
                found_cookie_file = os.path.abspath(cf)
                break

        attempts = []
        if found_cookie_file:
            attempts.append(("file", f"File {os.path.basename(found_cookie_file)}"))
        
        attempts.extend([
            ("chrome", "Chrome"),
            ("edge", "Edge"),
            (None, "Trực tiếp (Không Cookie)")
        ])

        last_error = None
        for browser_id, browser_name in attempts:
            ydl_opts = base_ydl_opts.copy()
            if browser_id == "file":
                ydl_opts["cookiefile"] = found_cookie_file
                self.log_fn(f"🔍 Sử dụng Cookie VIP từ file: {found_cookie_file}", "info")
            elif browser_id:
                ydl_opts["cookiesfrombrowser"] = (browser_id,)
                self.log_fn(f"🔍 Thử tải Cookie từ trình duyệt {browser_name}...", "info")
            else:
                self.log_fn("⚡ Chuyển sang chế độ tải trực tiếp...", "warning")

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    self.log_fn("📥 Đang phân tích luồng Video & Audio chất lượng cao nhất...", "info")
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "Bilibili_Video")
                    final_filename = ydl.prepare_filename(info)
                    
                    if not final_filename.endswith(".mp4"):
                        final_filename = os.path.splitext(final_filename)[0] + ".mp4"

                    self.progress_fn(100, "Hoàn tất tải 100%! 🎉")
                    self.log_fn(f"🎉 Tải thành công video: '{title}'", "success")
                    self.log_fn(f"📁 Lưu tại: {final_filename}", "success")
                    
                    return {
                        "success": True,
                        "file_path": final_filename,
                        "title": title,
                        "error": None
                    }

            except Exception as e:
                err_str = str(e)
                last_error = err_str
                if "Could not copy" in err_str or "cookie database" in err_str.lower() or "permission denied" in err_str.lower():
                    self.log_fn(f"⚠️ Trình duyệt {browser_name} đang mở làm khóa file Cookie. Tự động chuyển phương án...", "warning")
                    continue
                else:
                    if "hủy" in err_str.lower():
                        return {"success": False, "error": "Đã bị hủy bởi người dùng"}
                    self.log_fn(f"⚠️ Phương án {browser_name} gặp thông báo: {err_str[:100]}...", "warning")

        self.log_fn(f"❌ Tất cả các phương án đều gặp lỗi: {last_error}", "error")
        return {"success": False, "error": last_error}

    def _download_via_subprocess(self, url: str) -> Dict[str, Any]:
        out_template = os.path.join(self.output_dir, "%(title)s.%(ext)s")
        cmd = [
            sys.executable, "-m", "yt_dlp",
            url,
            "--add-header", "Referer: https://www.bilibili.com/",
            "--add-header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", out_template,
            "--concurrent-fragments", "8"
        ]

        if FFMPEG_DIR and os.path.exists(FFMPEG_DIR):
            cmd.extend(["--ffmpeg-location", FFMPEG_DIR])

        try:
            self.log_fn("📥 Đang chạy subprocess yt-dlp...", "info")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            
            for line in proc.stdout:
                if self._is_cancelled:
                    proc.kill()
                    return {"success": False, "error": "Đã bị hủy bởi người dùng"}
                
                line_str = line.strip()
                if "[download]" in line_str and "%" in line_str:
                    match = re.search(r"(\d+\.\d+)%", line_str)
                    if match:
                        pct = int(float(match.group(1)))
                        self.progress_fn(pct, f"Đang tải... [{pct}%]")
                elif line_str:
                    self.log_fn(line_str, "info")

            proc.wait()
            if proc.returncode == 0:
                self.progress_fn(100, "Hoàn thành 100%! 🎉")
                return {"success": True, "file_path": self.output_dir, "title": "Bilibili Video", "error": None}
            else:
                return {"success": False, "error": f"Mã lỗi exit: {proc.returncode}"}

        except Exception as e:
            return {"success": False, "error": str(e)}
