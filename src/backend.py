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

# Giới hạn an toàn khi gộp câu (Dành cho chế độ GROUP_SRT = True)
READ_RATE_CHARS_PER_SEC = 21
SAFETY_MARGIN_MS = 300
MAX_GAP_MS = 500
MAX_CHARS_PER_GROUP = 75

class CapCutBackend:
    def __init__(self, config, log_callback=None, progress_callback=None):
        self.cfg = config
        self.log_fn = log_callback if log_callback else print
        self.progress_fn = progress_callback if progress_callback else (lambda d, t, p: None)

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
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        
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

    def group_srt_blocks(self, subs):
        grouped_data = []
        current_text = ""
        current_start = None
        current_end = None

        for sub in subs:
            cleaned = self.clean_text(sub.text)
            if not cleaned: continue

            if current_start is None:
                current_text = cleaned
                current_start = sub.start.ordinal
                current_end = sub.end.ordinal
                continue

            candidate_text = f"{current_text} {cleaned}"
            candidate_end = sub.end.ordinal
            available_duration_ms = candidate_end - current_start
            estimated_read_ms = self.estimate_read_duration_ms(candidate_text)
            gap_ms = sub.start.ordinal - current_end
            should_split = False

            if gap_ms > MAX_GAP_MS: should_split = True
            elif len(candidate_text) > MAX_CHARS_PER_GROUP: should_split = True
            elif estimated_read_ms + SAFETY_MARGIN_MS > available_duration_ms: should_split = True

            if should_split:
                grouped_data.append({"text": current_text, "original_start_ms": current_start, "original_end_ms": current_end, "original_duration_ms": current_end - current_start})
                current_text = cleaned
                current_start = sub.start.ordinal
                current_end = sub.end.ordinal
            else:
                current_text = candidate_text
                current_end = candidate_end
                if self.is_sentence_end(cleaned):
                    grouped_data.append({"text": current_text, "original_start_ms": current_start, "original_end_ms": current_end, "original_duration_ms": current_end - current_start})
                    current_text = ""
                    current_start = None
                    current_end = None

        if current_text:
            grouped_data.append({"text": current_text, "original_start_ms": current_start, "original_end_ms": current_end, "original_duration_ms": current_end - current_start})
        return grouped_data

    def run_process(self, only_inject=False):
        t_start = time.time()
        audio_data = []
        
        is_grouping = self.cfg.get('GROUP_SRT', False)
        speed_ratio = float(self.cfg.get('SPEED_RATIO', 1.0))

        if not os.path.exists(self.cfg['SRT_FILE_PATH']):
            raise FileNotFoundError(f"Không tìm thấy file phụ đề tại: {self.cfg['SRT_FILE_PATH']}")
            
        subs = pysrt.open(self.cfg['SRT_FILE_PATH'], encoding="utf-8")
        
        self.log_fn("----------------")
        if is_grouping:
            self.log_fn("🧹 BƯỚC 1: CHẾ ĐỘ [GỘP CÂU] ĐANG BẬT - Dọn dẹp & Gộp câu phụ đề...")
            items_to_process = self.group_srt_blocks(subs)
            self.log_fn(f"✅ Đã gom {len(subs)} dòng SRT gốc thành {len(items_to_process)} câu hoàn chỉnh an toàn.")
        else:
            self.log_fn("✂️ BƯỚC 1: CHẾ ĐỘ [GIỮ NGUYÊN] ĐANG BẬT - Đọc từng dòng SRT riêng lẻ...")
            items_to_process = []
            for sub in subs:
                cleaned = self.clean_text(sub.text)
                if cleaned:
                    items_to_process.append({"text": cleaned, "original_start_ms": sub.start.ordinal, "original_end_ms": sub.end.ordinal, "original_duration_ms": sub.end.ordinal - sub.start.ordinal})
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
                            result = client.predict(
                                text=text, lang="Vietnamese", ref_aud=uploaded_ref, ref_text=self.cfg['REF_TEXT'], 
                                instruct="", ns=32, gs=2.0, dn=True, sp=1.0, du=0, pp=True, po=True, api_name="/_clone_fn"
                            )
                            final_wav_path = os.path.join(self.cfg['AUDIO_OUT_DIR'], f"clip_{index:03d}.wav")
                            
                            audio = AudioSegment.from_file(result[0])
                            non_silent = detect_nonsilent(audio, min_silence_len=50, silence_thresh=-40)
                            if non_silent:
                                audio = audio[non_silent[0][0]:min(len(audio), non_silent[-1][1] + 125)]
                                audio = audio.fade_out(50)
                            
                            audio.export(final_wav_path, format="wav")
                            
                            if os.path.exists(final_wav_path) and os.path.getsize(final_wav_path) > 0:
                                dur_ms = len(AudioSegment.from_file(final_wav_path))
                                self.log_fn(f"✨ [Thành công] Dòng {index:03d} -> {os.path.basename(final_wav_path)} ({dur_ms}ms)")
                                item['path'] = final_wav_path
                                item['actual_duration_ms'] = dur_ms
                                return item
                            else:
                                raise FileNotFoundError("Voice sinh ra bị trống.")
                        except Exception as e:
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
                    itm['path'] = wav_path
                    itm['actual_duration_ms'] = len(AudioSegment.from_file(wav_path))
                    audio_data.append(itm)
                    self.log_fn(f"✨ [Quét File] Tìm thấy -> clip_{i:03d}.wav")

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


        # -------------------------------------------------------------
        # NHÁNH A: NẾU GỘP CÂU = ĐẨY TIMELINE TỊNH TIẾN + 1 TRACK
        # -------------------------------------------------------------
        if is_grouping:
            self.log_fn("🔄 Logic Áp dụng: Tịnh tiến thời gian (Freeze Frame) -> 1 Track duy nhất.")
            
            global_offset_ms = 0
            previous_end_ms = 0
            new_srt = pysrt.SubRipFile()
            
            new_track_id = str(uuid.uuid4()).upper()
            new_track = {"attribute": 0, "flag": 0, "id": new_track_id, "type": "audio", "name": "OmniVoice_Grouped", "is_default_name": False, "segments": []}
            track_render_index = len(draft.get('tracks', [])) + 1  

            for i, data in enumerate(audio_data):
                ideal_start_ms = data['original_start_ms'] + global_offset_ms
                source_duration = data['actual_duration_ms']
                timeline_duration = int(source_duration / speed_ratio)
                
                if previous_end_ms > ideal_start_ms:
                    global_offset_ms += (previous_end_ms - ideal_start_ms)
                    ideal_start_ms = previous_end_ms 
                    
                target_start_ms = ideal_start_ms
                target_end_ms = target_start_ms + timeline_duration
                previous_end_ms = target_end_ms
                
                new_srt.append(pysrt.SubRipItem(index=i+1, start=pysrt.SubRipTime(milliseconds=int(target_start_ms)), end=pysrt.SubRipTime(milliseconds=int(target_end_ms)), text=data['text']))
                
                audio_id, segment_id, speed_id, placeholder_id, beat_id, sound_ch_id, vocal_sep_id = [str(uuid.uuid4()).upper() for _ in range(7)]
                draft['materials']['audios'].append({
                    "id": audio_id, "unique_id": "", "type": "extract_music", "name": os.path.basename(data['path']), "duration": source_duration * 1000, 
                    "path": data['path'].replace("\\", "/"), "category_name": "local", "check_flag": 1, "local_material_id": str(uuid.uuid4()).lower()
                })
                draft['materials']['speeds'].append({"id": speed_id, "type": "speed", "mode": 0, "speed": speed_ratio, "curve_speed": None})
                draft['materials']['placeholder_infos'].append({"id": placeholder_id, "type": "placeholder_info", "meta_type": "none"})
                draft['materials']['beats'].append({"id": beat_id, "type": "beats", "enable_ai_beats": False, "gear": 404})
                draft['materials']['sound_channel_mappings'].append({"id": sound_ch_id, "type": "none", "audio_channel_mapping": 0})
                draft['materials']['vocal_separations'].append({"id": vocal_sep_id, "type": "vocal_separation", "choice": 0})

                new_track['segments'].append({
                    "id": segment_id, "material_id": audio_id, "extra_material_refs": [speed_id, placeholder_id, beat_id, sound_ch_id, vocal_sep_id],
                    "source_timerange": {"start": 0, "duration": source_duration * 1000}, "target_timerange": {"start": target_start_ms * 1000, "duration": timeline_duration * 1000},
                    "speed": speed_ratio, "volume": 1.0, "track_id": new_track_id, "render_index": 0, "track_render_index": track_render_index, "visible": True
                })

            draft['tracks'].append(new_track)
            
            synced_srt_path = self.cfg['SRT_FILE_PATH'].replace(".srt", "_synced.srt")
            new_srt.save(synced_srt_path, encoding='utf-8')
            self.log_fn(f"✅ Đã xuất phụ đề đồng bộ (do thay đổi timeline) tại: {synced_srt_path}")
            
            if audio_data:
                total_dur = (audio_data[-1]['original_start_ms'] + global_offset_ms + (audio_data[-1]['actual_duration_ms']/speed_ratio)) * 1000
                if draft.get("duration", 0) < total_dur: draft["duration"] = int(total_dur)


        # -------------------------------------------------------------
        # NHÁNH B: KHÔNG GỘP = MULTI-TRACK + CHỐNG ĐÈ 2 CHIỀU (Tiến N & Lùi N-1)
        # -------------------------------------------------------------
        else:
            self.log_fn("🔄 Logic Áp dụng: Chia Đa Track + Chống đè tiếng 2 chiều (Tinh chỉnh N và N-1).")
            
            total_clips = len(audio_data)
            timeline = []
            #Khoảng lặng ở đầu và cuối block để tránh đè tiếng, tính bằng micro giây (1ms = 1000 micro giây)
            #GAP_MICRO = 100_000 # Khoảng hở bắt buộc 100ms
            GAP_MICRO = 0
            
            # --- PASS 1: TÍNH TOÁN TOẠ ĐỘ THỜI GIAN TRÊN RAM ---
            for clip in audio_data:
                orig_start = clip['original_start_ms'] * 1000
                actual_dur = clip['actual_duration_ms'] * 1000
                target_dur = int(actual_dur / speed_ratio)
                timeline.append({
                    "orig_start": orig_start,
                    "final_start": orig_start,
                    "dur": target_dur,
                    "actual_dur": actual_dur,
                    "path": clip['path']
                })
                
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

                draft['materials']['audios'].append({
                    "id": audio_id, "unique_id": "", "type": "extract_music", "name": os.path.basename(t_data['path']), "duration": actual_dur_micro, 
                    "path": t_data['path'].replace("\\", "/"), "category_name": "local", "check_flag": 1, "local_material_id": str(uuid.uuid4()).lower()
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
        return str(timedelta(seconds=int(time.time() - t_start)))