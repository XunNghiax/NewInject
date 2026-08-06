import os
import sys
import shutil
import subprocess
from typing import Callable, Optional, Dict, Any, List

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

# Tìm vị trí FFmpeg
FFMPEG_EXE = None
try:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if exe and os.path.exists(exe):
        bin_dir = os.path.dirname(exe)
        target_ffmpeg = os.path.join(bin_dir, "ffmpeg.exe")
        if not os.path.exists(target_ffmpeg):
            shutil.copy2(exe, target_ffmpeg)
        FFMPEG_EXE = target_ffmpeg
except Exception:
    FFMPEG_EXE = shutil.which("ffmpeg")


class SubtitleGenerator:
    """
    Modul nhận diện giọng nói tự động (Speech-to-Text) và tạo file phụ đề .srt
    - Sử dụng mô hình Faster-Whisper chạy 100% Offline (Không mất phí API)
    - Tự động tách âm thanh từ Video bằng FFmpeg
    - Trích xuất mốc thời gian chuẩn milli-giây cho từng câu thoại
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
        self._loaded_model = None
        self._loaded_model_size = None

    def cancel(self):
        self._is_cancelled = True

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        """Chuyển đổi số giây float thành định dạng chuỗi mốc thời gian SRT HH:MM:SS,ms"""
        if seconds < 0:
            seconds = 0
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        if millis >= 1000:
            secs += 1
            millis -= 1000
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def extract_audio_from_media(self, media_path: str) -> Optional[str]:
        """Sử dụng FFmpeg tách âm thanh chuẩn 16kHz mono WAV phục vụ AI nhận diện"""
        if not os.path.exists(media_path):
            self.log_fn(f"❌ Không tìm thấy file nguồn: {media_path}", "error")
            return None

        base_name = os.path.splitext(os.path.basename(media_path))[0]
        temp_wav_path = os.path.join(self.output_dir, f"{base_name}_temp_audio.wav")

        if not FFMPEG_EXE or not os.path.exists(FFMPEG_EXE):
            self.log_fn("⚠️ Không tìm thấy thực thi FFmpeg để tách âm thanh!", "warning")
            return media_path  # Trả về file gốc nếu là audio có sẵn

        cmd = [
            FFMPEG_EXE,
            "-y",
            "-i", media_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            temp_wav_path
        ]

        try:
            self.log_fn("🎙️ Đang trích xuất luồng âm thanh 16kHz từ tệp media...", "info")
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            if proc.returncode == 0 and os.path.exists(temp_wav_path):
                return temp_wav_path
            else:
                self.log_fn(f"⚠️ Trích xuất âm thanh gặp cảnh báo: {proc.stderr[:100]}", "warning")
                return media_path
        except Exception as e:
            self.log_fn(f"⚠️ Lỗi khi chạy FFmpeg: {e}", "warning")
            return media_path

    def _get_model(self, model_size: str = "base", device: str = "cpu", compute_type: str = "int8") -> Any:
        """Khởi tạo và nạp mô hình Faster-Whisper AI vào bộ nhớ"""
        if WhisperModel is None:
            raise ImportError("Thư viện 'faster-whisper' chưa được cài đặt!")

        if self._loaded_model is not None and self._loaded_model_size == model_size:
            return self._loaded_model

        self.log_fn(f"🧠 Đang khởi chạy Mô hình AI Faster-Whisper (Kích thước: '{model_size}' - Offline)...", "info")
        self.progress_fn(5, f"Nạp mô hình AI Faster-Whisper ({model_size})...")

        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._loaded_model = model
        self._loaded_model_size = model_size
        return model

    def generate_srt(
        self,
        media_path: str,
        output_srt_path: Optional[str] = None,
        model_size: str = "base",
        language: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Hàm chính nhận diện giọng nói và tự động xuất tệp .srt chuẩn
        """
        self._is_cancelled = False
        if not os.path.exists(media_path):
            err_msg = f"Tệp đầu vào không tồn tại: {media_path}"
            self.log_fn(f"❌ {err_msg}", "error")
            return {"success": False, "error": err_msg}

        if output_srt_path is None:
            base_name = os.path.splitext(os.path.basename(media_path))[0]
            output_srt_path = os.path.join(self.output_dir, f"{base_name}.srt")

        self.log_fn(f"🚀 Bắt đầu tiến trình tạo phụ đề cho: {os.path.basename(media_path)}", "info")
        self.progress_fn(2, "Khởi tạo môi trường AI...")

        # Step 1: Tách âm thanh
        audio_file = self.extract_audio_from_media(media_path)
        if not audio_file:
            return {"success": False, "error": "Không thể trích xuất âm thanh từ media"}

        try:
            # Step 2: Nạp Mô hình AI
            model = self._get_model(model_size=model_size)

            self.log_fn("🗣️ Đang phân tích giọng nói và dịch ra văn bản + mốc thời gian...", "info")
            self.progress_fn(15, "Đang phân tích luồng thoại bằng AI...")

            transcribe_kwargs = {
                "beam_size": 5,
                "vad_filter": True,
                "vad_parameters": dict(min_silence_duration_ms=500),
            }
            if language:
                transcribe_kwargs["language"] = language

            segments, info = model.transcribe(audio_file, **transcribe_kwargs)
            self.log_fn(f"🌐 Đã phát hiện ngôn ngữ: '{info.language.upper()}' (Độ tin cậy: {info.language_probability*100:.1f}%)", "info")

            srt_blocks = []
            block_idx = 1
            total_duration = info.duration if info.duration > 0 else 1.0

            for segment in segments:
                if self._is_cancelled:
                    self.log_fn("🛑 Đã hủy tiến trình nhận diện giọng nói!", "warning")
                    return {"success": False, "error": "Đã bị hủy bởi người dùng"}

                start_ts = self.format_timestamp(segment.start)
                end_ts = self.format_timestamp(segment.end)
                text = segment.text.strip()

                if not text:
                    continue

                srt_block = f"{block_idx}\n{start_ts} --> {end_ts}\n{text}"
                srt_blocks.append(srt_block)

                pct = min(95, int(15 + (segment.end / total_duration) * 80))
                status_msg = f"Đang nhận diện giọng nói... [{pct}%] (Thời lượng: {segment.end:.1f}s / {total_duration:.1f}s)"
                self.progress_fn(pct, status_msg)
                block_idx += 1

            if not srt_blocks:
                self.log_fn("⚠️ AI không tìm thấy câu thoại hợp lệ nào trong tệp media này.", "warning")
                return {"success": False, "error": "Không phát hiện thấy câu thoại nào"}

            os.makedirs(os.path.dirname(os.path.abspath(output_srt_path)), exist_ok=True)
            with open(output_srt_path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(srt_blocks) + "\n\n")

            self.progress_fn(100, "Hoàn tất 100%! 🎉")
            self.log_fn(f"🎉 Tải & tạo phụ đề thành công! Tổng số: {len(srt_blocks)} block.", "success")
            self.log_fn(f"📁 Tệp phụ đề SRT đã xuất tại: {output_srt_path}", "success")

            # Dọn dẹp tệp âm thanh tạm
            if audio_file != media_path and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                except Exception:
                    pass

            return {
                "success": True,
                "srt_path": output_srt_path,
                "total_blocks": len(srt_blocks),
                "language": info.language,
                "error": None
            }

        except Exception as e:
            err_str = str(e)
            self.log_fn(f"❌ Lỗi khi nhận diện giọng nói: {err_str}", "error")
            return {"success": False, "error": err_str}
