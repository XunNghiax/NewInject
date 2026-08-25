import os
import json
import uuid
import time
import shutil
import pysrt
import re
import threading
import concurrent.futures
from datetime import timedelta
import hashlib

try:
    import imageio_ffmpeg
    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if _ffmpeg_exe and os.path.exists(_ffmpeg_exe):
        _ffmpeg_dir = os.path.dirname(_ffmpeg_exe)
        if _ffmpeg_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

from pydub import AudioSegment
from pydub.silence import detect_nonsilent
from gradio_client import Client, handle_file
import psutil


READ_RATE_CHARS_PER_SEC = 21
SAFETY_MARGIN_MS = 300
MAX_GAP_MS = 500
MAX_CHARS_PER_GROUP = 75

class CapCutBackend:
    def __init__(self, config, log_callback=None, progress_callback=None, check_pause_callback=None):
        self.cfg = config
        self.log_fn = log_callback if log_callback else print
        self.progress_fn = progress_callback if progress_callback else (lambda d, t, p: None)
        self.check_pause_callback = check_pause_callback

    def ensure_capcut_closed(self):
        """Kiểm tra và ép đóng tiến trình CapCut để tránh lỗi Permission Denied"""
        self.log_fn("🔍 Kiểm tra trạng thái ứng dụng CapCut...")
        closed_any = False
        
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                process_name = proc.info['name'].lower()
                if 'capcut' in process_name and 'helper' not in process_name:
                    self.log_fn(f"⚠️ Phát hiện CapCut đang mở (PID: {proc.info['pid']}). Đang tiến hành đóng lại...")
                    proc.kill()
                    closed_any = True
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            except psutil.AccessDenied:
                self.log_fn("❌ LỖI QUYỀN: Không thể tự động đóng CapCut. Vui lòng tắt thủ công hoặc chạy Tool bằng Run as Administrator!")
                raise PermissionError("Access Denied khi cố gắng đóng CapCut.")
        
        if closed_any:
            self.log_fn("✅ Đã đóng ứng dụng CapCut.")
            time.sleep(2) 
        else:
            self.log_fn("✅ CapCut đang tắt, không có xung đột.")        

    def clean_text(self, text):
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\[.*?\]|\(.*?\)', '', text)
        text = text.replace('\n', ' ')
        
        # Xóa ngoặc kép để tránh AI đọc sai hoặc khựng lại
        text = re.sub(r'["\u201c\u201d]', '', text)
        
        # CHỈNH SỬA TẠI ĐÂY: Thay dấu (:) bằng (!) để tạo khoảng nghỉ sâu hơn cho TTS
        text = text.replace(':', '!')
        
        text = text.strip()
        if text and text[0].islower():
            text = text[0].upper() + text[1:]
        
        return re.sub(r'\s+', ' ', text).strip()

    def is_sentence_end(self, text):
        return text.endswith(('.', '?', '!'))
    
    def estimate_read_duration_ms(self, text):
        return int((len(text) / READ_RATE_CHARS_PER_SEC) * 1000)

    def clean_text_natural(self, text):
        # Chỉ xóa thẻ HTML <>, giữ lại biểu cảm [] và ()
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace('\n', ' ')
        return re.sub(r'\s+', ' ', text).strip()

    def run_natural_audio_process(self):
        t_start = time.time()
        self.log_fn("\n------------------------")
        self.log_fn("🚀 CHẾ ĐỘ CHỈ TẠO AUDIO TỰ NHIÊN (ĐỘC LẬP)")
        
        if not os.path.exists(self.cfg['SRT_FILE_PATH']):
            raise FileNotFoundError(f"Không tìm thấy file phụ đề tại: {self.cfg['SRT_FILE_PATH']}")
            
        subs = pysrt.open(self.cfg['SRT_FILE_PATH'], encoding="utf-8")
        
        # Gom text và tách câu
        full_text = " ".join([self.clean_text_natural(sub.text) for sub in subs if sub.text.strip()])
        # Tách dựa trên . ? ! (Giữ lại dấu câu)
        sentences = re.split(r'(?<=[.!?])\s+', full_text.strip())
        sentences = [s.strip() for s in sentences if s.strip()]
        
        self.log_fn(f"✅ Đã gom và tách thành {len(sentences)} câu hoàn chỉnh.")
        
        audio_out_dir = self.cfg['AUDIO_OUT_DIR']
        os.makedirs(audio_out_dir, exist_ok=True)
        
        try:
            client = Client(self.cfg['SERVER_URL'])
            client.timeout = 120
            uploaded_ref = handle_file(self.cfg['REF_AUDIO_PATH'])
        except Exception as e:
            raise RuntimeError(f"Thất bại khi kết nối Gradio: {e}")

        combined_audio = AudioSegment.silent(duration=0)
        
        for i, text in enumerate(sentences, 1):
            if self.check_pause_callback:
                self.check_pause_callback()
                
            self.log_fn(f"⏳ [Đang tạo {i}/{len(sentences)}]: \"{text[:40]}...\"")
            
            MAX_RETRIES = 3
            attempt = 0
            success = False
            
            while attempt <= MAX_RETRIES and not success:
                try:
                    # Chú ý: tăng ns=50 cho chất lượng tốt hơn
                    result = client.predict(
                        text=text, lang="Vietnamese", ref_aud=uploaded_ref, ref_text=self.cfg['REF_TEXT'], 
                        instruct="", ns=50, gs=2.0, dn=True, sp=1.0, du=0, pp=True, po=True, api_name="/_clone_fn"
                    )
                    
                    audio_segment = AudioSegment.from_file(result[0])
                    # Nối vào audio tổng (có thể thêm 200ms khoảng lặng giữa các câu để tự nhiên hơn)
                    combined_audio += audio_segment + AudioSegment.silent(duration=200)
                    success = True
                except Exception as e:
                    attempt += 1
                    if attempt <= MAX_RETRIES:
                        self.log_fn(f"🔄 [Thử lại {attempt}/{MAX_RETRIES}] Câu {i} gặp sự cố: {e}")
                        time.sleep(2)
                    else:
                        raise RuntimeError(f"❌ [LỖI] Câu {i} thất bại hoàn toàn sau 3 lần thử: {e}")
            
            self.progress_fn(i, len(sentences), "Tạo giọng nói Tự nhiên")

        # Xuất file tổng
        final_wav_path = os.path.join(audio_out_dir, "Natural_Voice_Full.wav")
        combined_audio.export(final_wav_path, format="wav")
        self.log_fn(f"\n🎉 Đã xuất thành công file Audio tự nhiên tại: {final_wav_path}")
        
        return str(timedelta(seconds=int(time.time() - t_start)))

    def run_process(self, only_inject=False):
        t_start = time.time()
        
        if self.cfg.get('CREATE_NATURAL_AUDIO_ONLY', False):
            return self.run_natural_audio_process()
            
        audio_data = []
        
        speed_ratio = round(float(self.cfg.get('SPEED_RATIO', 1.0)), 4)

        if not os.path.exists(self.cfg['SRT_FILE_PATH']):
            raise FileNotFoundError(f"Không tìm thấy file phụ đề tại: {self.cfg['SRT_FILE_PATH']}")
            
        subs = pysrt.open(self.cfg['SRT_FILE_PATH'], encoding="utf-8")
        
        items_to_process = []
        if self.cfg.get('EXPERIMENTAL_HYBRID_MODE', False):
            self.log_fn("----------------")
            self.log_fn("🧪 BƯỚC 1 (HYBRID): Đang gom block SRT thành CÂU HOÀN CHỈNH...")
            char_map = []
            for sub in subs:
                text = sub.text.replace('\n', ' ').strip()
                text = re.sub(r'\s+', ' ', text)
                if not text: continue
                
                start_ms = sub.start.ordinal
                end_ms = sub.end.ordinal
                duration = end_ms - start_ms
                time_per_char = duration / len(text)
                
                if char_map and char_map[-1]['char'] != ' ' and text[0] != ' ':
                    char_map.append({'char': ' ', 'time': start_ms})
                    
                for i, char in enumerate(text):
                    char_map.append({'char': char, 'time': start_ms + int(i * time_per_char)})

            full_text = "".join([c['char'] for c in char_map])
            boundaries = [0]
            for i in range(len(full_text) - 1):
                if full_text[i] in ['.', '!', '?'] and full_text[i+1].isspace():
                    if full_text[i] == '.' and i > 0 and full_text[i-1] == '.':
                        continue
                    if i + 2 < len(full_text) and full_text[i+1] == '"' and full_text[i+2].isspace():
                        boundaries.append(i + 2)
                    else:
                        boundaries.append(i + 1)
            boundaries.append(len(full_text))
            
            for k in range(len(boundaries) - 1):
                start_idx = boundaries[k]
                end_idx = boundaries[k+1] - 1
                
                sentence = full_text[start_idx:end_idx+1].strip()
                if not sentence or not re.search(r'[a-zA-Z0-9À-ỹ]', sentence):
                    continue
                    
                while start_idx < end_idx and char_map[start_idx]['char'].isspace():
                    start_idx += 1
                while end_idx > start_idx and char_map[end_idx]['char'].isspace():
                    end_idx -= 1
                    
                start_ms = char_map[start_idx]['time']
                end_ms = char_map[end_idx]['time']
                
                # Làm sạch text như cũ để đọc TTS tốt hơn
                cleaned = self.clean_text(sentence)
                if cleaned:
                    items_to_process.append({
                        "text": cleaned, 
                        "original_start_ms": start_ms, 
                        "original_end_ms": end_ms, 
                        "original_duration_ms": end_ms - start_ms
                    })
            self.log_fn(f"✅ Đã gom thành công {len(items_to_process)} câu (từ {len(subs)} block SRT).")
        else:
            self.log_fn("----------------")
            self.log_fn("✂️ BƯỚC 1: Đọc từng dòng SRT riêng lẻ (Mode Cũ)...")
            for sub in subs:
                cleaned = self.clean_text(sub.text)
                if cleaned:
                    items_to_process.append({
                        "text": cleaned, 
                        "original_start_ms": sub.start.ordinal, 
                        "original_end_ms": sub.end.ordinal, 
                        "original_duration_ms": sub.end.ordinal - sub.start.ordinal
                    })
            self.log_fn(f"✅ Đã tải {len(items_to_process)} block SRT.")

        total_items = len(items_to_process)
        if total_items == 0:
            raise RuntimeError("File phụ đề SRT trống, không có dữ liệu để chạy!")

        if not only_inject:
            self.log_fn("\n------------------------")
            self.log_fn("🔌 BƯỚC 2: KẾT NỐI SERVER GRADIO VÀ SINH AUDIO...")
            
            # 1. Đảm bảo thư mục tồn tại
            audio_out_dir = self.cfg['AUDIO_OUT_DIR']
            os.makedirs(audio_out_dir, exist_ok=True)

            # 2. Thay vì xóa, Quét kiểm tra các file đã tồn tại
            self.log_fn("🔍 Đang kiểm tra thư mục voice để resume tiến trình nếu có...")
            missing_items = []
            for i, itm in enumerate(items_to_process, 1):
                wav_path = os.path.join(audio_out_dir, f"clip_{i:03d}.wav")
                itm['index'] = i  # Lưu index gốc để tạo đúng tên file
                if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                    try:
                        dur_ms = len(AudioSegment.from_file(wav_path))
                        if dur_ms < 100:
                            self.log_fn(f"⚠️ [Lỗi] clip_{i:03d}.wav quá ngắn ({dur_ms}ms). Đưa vào danh sách tạo lại.")
                            missing_items.append(itm)
                        else:
                            itm['path'] = wav_path
                            itm['actual_duration_ms'] = dur_ms
                            audio_data.append(itm)
                            self.log_fn(f"✨ [Resume] Tìm thấy file đã tạo -> clip_{i:03d}.wav ({dur_ms}ms)")
                    except Exception:
                        missing_items.append(itm)
                else:
                    missing_items.append(itm)

            # Sắp xếp lại audio_data theo đúng index ban đầu
            audio_data.sort(key=lambda x: x.get('index', 0))

            if not missing_items:
                self.log_fn("⏩ Tiến trình voice đã hoàn tất 100% trước đó, bỏ qua gọi Gradio API!")
            else:
                self.log_fn(f"🔄 Tiến hành tạo {len(missing_items)} voice còn thiếu thông qua Gradio...")
                try:
                    client = Client(self.cfg['SERVER_URL'])
                    client.timeout = 120
                    uploaded_ref = handle_file(self.cfg['REF_AUDIO_PATH'])
                except Exception as e:
                    raise RuntimeError(f"Thất bại khi kết nối Gradio: {e}")

                def generate_voice_clip(item):
                    if self.check_pause_callback:
                        self.check_pause_callback()
                    index = item['index']
                    text = item['text']
                    MAX_RETRIES = 3
                    attempt = 0
                    
                    while attempt <= MAX_RETRIES:
                        if attempt == 0:
                            self.log_fn(f"⏳ [Luồng chạy] Gửi dòng {index:03d}/{total_items}: \"{text[:30]}...\"")
                        else:
                            self.log_fn(f"🔄 [Thử lại {attempt}/{MAX_RETRIES}] Dòng {index:03d} gặp sự cố...")
                        
                        try:
                            if self.check_pause_callback:
                                self.check_pause_callback()
                                
                            result = client.predict(
                                text=text, lang="Vietnamese", ref_aud=uploaded_ref, ref_text=self.cfg['REF_TEXT'], 
                                instruct="", ns=32, gs=2.0, dn=True, sp=1.0, du=0, pp=True, po=True, api_name="/_clone_fn"
                            )
                            
                            if self.check_pause_callback:
                                self.check_pause_callback()
                                
                            final_wav_path = os.path.join(self.cfg['AUDIO_OUT_DIR'], f"clip_{index:03d}.wav")
                            
                            audio = AudioSegment.from_file(result[0])
                            
                            # 1. Chuẩn hóa âm lượng (Normalization)
                            target_dBFS = -20.0
                            change_in_dBFS = target_dBFS - audio.dBFS
                            audio = audio.apply_gain(change_in_dBFS)
                            
                            # 2. Ngưỡng cắt động (Dynamic Threshold)
                            dynamic_thresh = audio.dBFS - 16
                            non_silent = detect_nonsilent(audio, min_silence_len=50, silence_thresh=dynamic_thresh)
                            
                            if non_silent:
                                # 3. Padding lùi lại 30ms ở đầu
                                start_idx = max(0, non_silent[0][0] - 30)
                                end_idx = min(len(audio), non_silent[-1][1] + 125)
                                audio = audio[start_idx:end_idx]
                                
                                # 4. Fade in 20ms và Fade out 50ms
                                audio = audio.fade_in(20).fade_out(50)
                            
                            audio.export(final_wav_path, format="wav")
                            
                            if os.path.exists(final_wav_path) and os.path.getsize(final_wav_path) > 0:
                                dur_ms = len(AudioSegment.from_file(final_wav_path))
                                if dur_ms < 100:
                                    raise FileNotFoundError(f"Voice quá ngắn ({dur_ms}ms), file rác.")
                                self.log_fn(f"✨ [Thành công] Dòng {index:03d} -> {os.path.basename(final_wav_path)} ({dur_ms}ms)")
                                item['path'] = final_wav_path
                                item['actual_duration_ms'] = dur_ms
                                return item
                            else:
                                raise FileNotFoundError("Voice sinh ra bị trống.")
                        except Exception as e:
                            if "hủy bỏ" in str(e).lower():
                                raise e
                            attempt += 1
                            if attempt <= MAX_RETRIES:
                                time.sleep(1.5)
                            else:
                                self.log_fn(f"❌ [LỖI] Dòng {index:03d} thất bại: {e}")
                                return None

                done_count = len(items_to_process) - len(missing_items)
                MAX_WORKERS = 6
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = {executor.submit(generate_voice_clip, itm): itm for itm in missing_items}
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res: 
                            audio_data.append(res)
                        done_count += 1
                        self.progress_fn(done_count, total_items, "Tạo giọng nói AI")
                
                # Sắp xếp lại audio_data một lần nữa để đảm bảo tính tuyến tính
                audio_data.sort(key=lambda x: x.get('index', 0))

        else:
            self.log_fn("\n------------------------")
            self.log_fn("🚀 CHẾ ĐỘ INJECT ONLY")
            for i, itm in enumerate(items_to_process, 1):
                wav_path = os.path.join(self.cfg['AUDIO_OUT_DIR'], f"clip_{i:03d}.wav")
                if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                    try:
                        dur_ms = len(AudioSegment.from_file(wav_path))
                        if dur_ms < 100:
                            self.log_fn(f"⚠️ [Bỏ qua] clip_{i:03d}.wav quá ngắn ({dur_ms}ms) -> Tránh lỗi CapCut 10011.")
                            continue
                        itm['path'] = wav_path
                        itm['actual_duration_ms'] = dur_ms
                        audio_data.append(itm)
                        self.log_fn(f"✨ [Quét File] Tìm thấy -> clip_{i:03d}.wav ({dur_ms}ms)")
                    except Exception:
                        pass

        audio_data.sort(key=lambda x: x['original_start_ms'])
        
        if not audio_data:
            raise RuntimeError("Không thu thập được file âm thanh hợp lệ nào!")

        self.log_fn("\n------------------------")
        self.ensure_capcut_closed()
        self.log_fn("💉 BƯỚC 3: XỬ LÝ TIMELINE & BƠM VÀO CAPCUT...")
        
        capcut_dir = os.path.dirname(self.cfg['CAPCUT_JSON_PATH'])
        main_json_path = self.cfg['CAPCUT_JSON_PATH']
        main_backup_path = main_json_path + ".backup"

        if os.path.exists(main_backup_path):
            if os.path.exists(main_json_path): os.remove(main_json_path)
            shutil.move(main_backup_path, main_json_path)
            
        for file_name in os.listdir(capcut_dir):
            if file_name.endswith('.backup') and not file_name == os.path.basename(main_backup_path):
                backup_full_path = os.path.join(capcut_dir, file_name)
                if backup_full_path.endswith(".backup"):
                    original_full_path = backup_full_path[:-7]
                else:
                    original_full_path = backup_full_path.replace(".backup", "")
                try:
                    if os.path.exists(original_full_path): os.remove(original_full_path)
                    shutil.move(backup_full_path, original_full_path)
                except: pass

        if not os.path.exists(main_json_path):
            raise FileNotFoundError("Không tìm thấy file JSON của CapCut.")

        shutil.copy(main_json_path, main_backup_path)
        
        with open(main_json_path, 'r', encoding='utf-8') as f:
            draft = json.load(f)

        for key in ['audios', 'speeds', 'placeholder_infos', 'beats', 'sound_channel_mappings', 'vocal_separations']:
            if key not in draft['materials']: draft['materials'][key] = []


        self.log_fn("🔄 Logic Áp dụng: Chia Đa Track + Chống đè tiếng 2 chiều (Tinh chỉnh N và N-1).")
        
        total_clips = len(audio_data)
        timeline = []
        #Khoảng lặng ở đầu và cuối block để tránh đè tiếng, tính bằng micro giây (1ms = 1000 micro giây)
        #GAP_MICRO = 100_000 # Khoảng hở bắt buộc 100ms
        GAP_MICRO = 0
        
        # --- PASS 1: TÍNH TOÁN TOẠ ĐỘ THỜI GIAN TRÊN RAM ---
        ignore_timeline = self.cfg.get('IGNORE_TIMELINE', False)
        smart_timeline = self.cfg.get('SMART_TIMELINE', False)
        current_time_micro = 0
        last_orig_end_micro = 0
        last_text = ""

        for i, clip in enumerate(audio_data):
            orig_start = clip['original_start_ms'] * 1000
            orig_end = clip.get('original_end_ms', clip['original_start_ms']) * 1000
            actual_dur = clip['actual_duration_ms'] * 1000
            target_dur = int(actual_dur / speed_ratio)
            text = clip.get('text', '').strip()
            
            if ignore_timeline:
                if smart_timeline:
                    if i == 0:
                        current_time_micro = orig_start
                    else:
                        orig_gap = max(0, orig_start - last_orig_end_micro)
                        punct_gap = 50_000
                        if last_text.endswith(('.', '!', '?', '...')):
                            punct_gap = 800_000
                        elif last_text.endswith((',', '-', ':')):
                            punct_gap = 300_000
                        chosen_gap = max(orig_gap, punct_gap)
                        current_time_micro += chosen_gap
                
                final_start = current_time_micro
            else:
                final_start = orig_start

            timeline.append({
                "orig_start": orig_start,
                "final_start": final_start,
                "dur": target_dur,
                "actual_dur": actual_dur,
                "path": clip['path']
            })
            
            if ignore_timeline:
                current_time_micro += target_dur
                last_orig_end_micro = orig_end
                last_text = text
                
        if not ignore_timeline:
            for i in range(1, total_clips):
                prev = timeline[i-1] # Đây là Block N-1
                curr = timeline[i]   # Đây là Block N
            
            prev_end = prev['final_start'] + prev['dur']
            curr_start = curr['final_start']
            
            # Tính độ lấn (overlap) cộng thêm 100ms an toàn
            overlap = (prev_end + GAP_MICRO) - curr_start
            
            if overlap > 0:
                # 1. Tìm không gian trống phía trước của N (Giới hạn bởi N+1)
                if i + 1 < total_clips:
                    next_orig_start = timeline[i+1]['orig_start']
                    max_push_fwd = next_orig_start - (curr_start + curr['dur'] + GAP_MICRO)
                else:
                    max_push_fwd = float('inf')
                    
                # 2. Tìm không gian trống phía sau của N-1 (Giới hạn bởi N-2)
                if i - 2 >= 0:
                    prev2_end = timeline[i-2]['final_start'] + timeline[i-2]['dur']
                    max_push_bwd = prev['final_start'] - (prev2_end + GAP_MICRO)
                else:
                    max_push_bwd = prev['final_start'] # Block đầu tiên đẩy về 0 được
                    
                max_push_fwd = max(0, max_push_fwd)
                max_push_bwd = max(0, max_push_bwd)
                
                # 3. Phân bổ khoảng dời nếu không gian đủ rộng
                if max_push_fwd + max_push_bwd >= overlap:
                    push_fwd = min(overlap, max_push_fwd)
                    push_bwd = overlap - push_fwd
                    
                    curr['final_start'] += push_fwd
                    prev['final_start'] -= push_bwd
                    
                    if push_fwd > 0 or push_bwd > 0:
                        self.log_fn(f"⚖️ Tinh chỉnh khớp 100ms: Block {i:03d} lùi {push_bwd/1000000:.2f}s, Block {i+1:03d} tới {push_fwd/1000000:.2f}s.")
                else:
                    # Kẹt cứng cả 2 đầu -> Huỷ dời, ném xuống Track 2 như trong ảnh bạn gửi
                    if i <= 15:
                        self.log_fn(f"⚠️ Kẹt cứng 2 đầu: Giữ nguyên vị trí Block {i:03d} & {i+1:03d} (Sẽ tự động chia Layer).")
        
        # --- PASS 2: BƠM DỮ LIỆU VÀO JSON ---
        self.log_fn("=== DEBUG TIMELINE SAU PASS 1 ===")
        for idx, t in enumerate(timeline):
            end_time = (t['final_start'] + t['dur']) / 1_000_000
            self.log_fn(
                f"  clip_{idx+1:03d}: "
                f"orig={t['orig_start']/1_000_000:.3f}s | "
                f"final={t['final_start']/1_000_000:.3f}s | "
                f"dur={t['dur']/1_000_000:.3f}s | "
                f"end={end_time:.3f}s"
            )
        self.log_fn("=================================")

        # --- PASS 2: BƠM DỮ LIỆU VÀO JSON ---
        tracks_data = []
        for i in range(total_clips):
            t_data = timeline[i]
            idx = i + 1
            
            audio_id, segment_id, speed_id, placeholder_id, beat_id, sound_ch_id, vocal_sep_id = [str(uuid.uuid4()).upper() for _ in range(7)]
            
            start_micro = t_data['final_start']
            target_dur_micro = t_data['dur']
            actual_dur_micro = t_data['actual_dur']
            
            assigned_track = None
            for t in tracks_data:
                if t['last_end'] <= start_micro:
                    assigned_track = t
                    break
            
            if not assigned_track:
                assigned_track = {"id": str(uuid.uuid4()).upper(), "last_end": 0, "segments": []}
                tracks_data.append(assigned_track)
                
            assigned_track['last_end'] = start_micro + target_dur_micro
            layer_num = tracks_data.index(assigned_track) + 1
            self.log_fn(
                f"  [ASSIGN] clip_{i+1:03d} → Layer {layer_num} | "
                f"start={start_micro/1_000_000:.3f}s | "
                f"end={(start_micro+target_dur_micro)/1_000_000:.3f}s"
            )

            abs_path = os.path.abspath(t_data['path']).replace("\\", "/")
            try:
                with open(t_data['path'], 'rb') as _f:
                    file_md5 = hashlib.md5(_f.read()).hexdigest()
            except Exception:
                file_md5 = ""

            draft['materials']['audios'].append({
                "id": audio_id, "unique_id": file_md5, "type": "extract_music", "name": os.path.basename(t_data['path']), "duration": actual_dur_micro, 
                "path": abs_path, "category_name": "local", "check_flag": 1, "local_material_id": str(uuid.uuid4()).lower()
            })
            draft['materials']['speeds'].append({"id": speed_id, "type": "speed", "mode": 0, "speed": speed_ratio, "curve_speed": None})
            draft['materials']['placeholder_infos'].append({"id": placeholder_id, "type": "placeholder_info", "meta_type": "none"})
            draft['materials']['beats'].append({"id": beat_id, "type": "beats", "enable_ai_beats": False, "gear": 404})
            draft['materials']['sound_channel_mappings'].append({"id": sound_ch_id, "type": "none", "audio_channel_mapping": 0})
            draft['materials']['vocal_separations'].append({"id": vocal_sep_id, "type": "vocal_separation", "choice": 0})

            assigned_track['segments'].append({
                "id": segment_id, "material_id": audio_id, "extra_material_refs": [speed_id, placeholder_id, beat_id, sound_ch_id, vocal_sep_id],
                "source_timerange": {"start": 0, "duration": actual_dur_micro}, "target_timerange": {"start": start_micro, "duration": target_dur_micro},
                "speed": speed_ratio, "volume": 1.0, "track_id": assigned_track['id'], "render_index": 0, "track_render_index": 0, "visible": True
            })

            self.progress_fn(idx, total_clips, f"Inject cấu hình JSON (Tốc độ {speed_ratio}x)")

        base_render_index = len(draft.get('tracks', [])) + 1
        for i, t in enumerate(tracks_data):
            track_render_idx = base_render_index + i
            for seg in t['segments']:
                seg['track_render_index'] = track_render_idx
            draft['tracks'].append({"attribute": 0, "flag": 0, "id": t['id'], "type": "audio", "name": f"AI_Auto_Layer_{i+1}", "is_default_name": False, "segments": t['segments']})
            self.log_fn(f"📈 Đã tạo Track [Layer_{i+1}] chứa {len(t['segments'])} block.")
        
        if audio_data:
            max_end_time = max([t['last_end'] for t in tracks_data])
            if draft.get("duration", 0) < max_end_time: draft["duration"] = int(max_end_time)


        # --- GHI FILE CHUNG ---
        with open(main_json_path, 'w', encoding='utf-8') as f:
            json.dump(draft, f, ensure_ascii=False, indent=4)
        
        self.log_fn("\n🎉 Đã ghi đè thành công dữ liệu vào draft_content.json.")

        # --- XUẤT THÊM FILE WAV TỔNG HỢP NHƯ MỘT BẢN BACKUP ---
        self.log_fn("\n------------------------")
        self.log_fn("🎵 Đang tạo file WAV tổng hợp (Combined Audio)...")
        try:
            if timeline:
                max_duration_ms = int(max([(t['final_start'] + t['actual_dur']) / 1000.0 for t in timeline])) + 1000
                combined = AudioSegment.silent(duration=max_duration_ms)
                for t in timeline:
                    clip = AudioSegment.from_file(t['path'])
                    # Apply speed change to the clip if needed, or overlay actual duration
                    pos_ms = int(t['final_start'] / 1000.0)
                    combined = combined.overlay(clip, position=pos_ms)
                
                # Use a specific filename for the combined file
                combined_wav_path = os.path.join(self.cfg['AUDIO_OUT_DIR'], "Combined_Output_Final.wav")
                combined.export(combined_wav_path, format="wav")
                self.log_fn(f"🎉 Đã xuất thành công file WAV tổng hợp tại: {combined_wav_path}")
                self.log_fn("💡 MẸO: Nếu CapCut bị lỗi, bạn có thể kéo trực tiếp file WAV tổng hợp này vào timeline!")
        except Exception as e:
            self.log_fn(f"⚠️ Lỗi khi tạo file WAV tổng hợp: {e}")

        return str(timedelta(seconds=int(time.time() - t_start)))