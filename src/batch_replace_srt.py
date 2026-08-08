import os
import re
from collections import defaultdict


# ==============================================================================
# HÀM PARSE CHUNG — Dùng cho cả chế độ file đơn lẻ lẫn thư mục
# ==============================================================================

def parse_patch_and_deletes(text):
    """
    Phân tích output của Repair Engine, trả về:
      - replace_dict : { block_id: new_block_text }  — các block cần thay thế
      - delete_set   : { block_id, ... }              — các block cần xóa

    Repair Engine output có 2 dạng thông tin:

    Dạng 1 — Block thay thế bình thường:
        92
        00:02:39,677 --> 00:02:42,000
        Tôi lườm Liễu Như Yên một cái, lạnh lùng đáp:

    Dạng 2 — Comment MERGED ngay sau block cuối của cụm:
        ; [MERGED: 13, 14, 15, 16 → xóa khỏi file gốc, renumber từ 17 trở đi]
        → Parse ra delete_set = {13, 14, 15, 16}

    Comment MERGED có thể xuất hiện ngay sau 1 block hoặc sau nhóm nhiều block.
    """
    replace_dict = {}
    delete_set   = set()

    # Chuẩn hóa line ending và loại bỏ markdown code fences
    text = text.replace('\r\n', '\n')
    text = re.sub(r'```(?:srt)?', '', text, flags=re.IGNORECASE).strip()

    # Tách thành các đoạn theo dòng trống
    raw_chunks = re.split(r'\n\s*\n', text)

    for chunk in raw_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        lines = chunk.split('\n')

        # Tách dòng comment [MERGED] ra khỏi phần SRT
        srt_lines    = []
        merged_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith('; [MERGED:') or stripped.startswith(';[MERGED:'):
                merged_lines.append(stripped)
            else:
                srt_lines.append(line)

        # Parse phần SRT (tìm dòng đầu tiên là số nguyên ID block)
        if srt_lines:
            id_idx = -1
            for idx, sl in enumerate(srt_lines):
                if re.match(r'^\d+$', sl.strip()):
                    id_idx = idx
                    break

            if id_idx != -1:
                block_id = int(srt_lines[id_idx].strip())
                clean_srt_text = '\n'.join(srt_lines[id_idx:]).strip()
                replace_dict[block_id] = clean_srt_text

        # Parse phần [MERGED] để lấy danh sách block cần xóa
        for mline in merged_lines:
            m = re.search(r'\[MERGED:\s*([^\]→]+)', mline)
            if m:
                ids_str = m.group(1)
                for num_str in re.findall(r'\d+', ids_str):
                    delete_set.add(int(num_str))

    return replace_dict, delete_set


def parse_patch_blocks(text):
    """
    Backward-compatible: Chỉ parse replace_dict (không xử lý MERGED).
    Giữ lại để không break code cũ nếu có nơi nào gọi trực tiếp.
    """
    replace_dict, _ = parse_patch_and_deletes(text)
    return replace_dict


# ==============================================================================
# HELPER: LOG THAY ĐỔI
# ==============================================================================

def _log_replace(log_callback, block_id, old_text, new_text):
    log_callback(f"   ✓ Đã thay thế Block {block_id}")

def _log_delete(log_callback, block_id):
    log_callback(f"   🗑️ Đã xóa Block {block_id} (gộp block)")



# ==============================================================================
# CHẾ ĐỘ THƯ MỤC
# ==============================================================================

def detect_prefix(folder):
    """
    Tự động quét thư mục để tìm tiền tố của file .srt.
    Ví dụ: Thấy file '0609_1.srt' → trả về '0609'
    """
    if os.path.exists(folder):
        for filename in os.listdir(folder):
            if filename.endswith(".srt"):
                parts = filename.rsplit('_', 1)
                if len(parts) == 2:
                    return parts[0]
    return ""


def build_block_index_map(folder):
    """
    Quét thực tế tất cả các file .srt trong folder và các thư mục con 
    để lập bản đồ: block_id -> đường dẫn file thực tế chứa block đó.
    """
    block_map = {}
    if not os.path.exists(folder):
        return block_map

    for root, dirs, files in os.walk(folder):
        for fname in sorted(files, key=lambda x: [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', x)]):
            if fname.endswith('.srt'):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        content = '\n' + f.read().replace('\r\n', '\n')
                    for m in re.finditer(r'\n(\d+)\n\d{2}:\d{2}:\d{2}', content):
                        b_id = int(m.group(1))
                        # Ưu tiên các folder tiếng Việt/temp_split_vi nếu có trùng
                        if b_id not in block_map or "temp_split_vi" in root or "_vi" in root:
                            block_map[b_id] = fpath
                except Exception:
                    pass
    return block_map


def get_target_file(block_id, folder, prefix, block_map=None):
    """Xác định chính xác tên file chứa block_id bằng block_map hoặc quét thực tế."""
    if block_map and block_id in block_map:
        return block_map[block_id]

    # Quét trực tiếp tìm file thực sự chứa block_id
    if os.path.exists(folder):
        for root, dirs, files in os.walk(folder):
            for fname in files:
                if fname.endswith('.srt'):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            content = '\n' + f.read().replace('\r\n', '\n')
                            if f"\n{block_id}\n" in content:
                                return fpath
                    except Exception:
                        pass

    file_no = ((block_id - 1) // 100) + 1
    if prefix:
        return os.path.join(folder, f"{prefix}_{file_no}.srt")
    return os.path.join(folder, f"part_{file_no}.srt")


def replace_blocks_in_folder(folder, patch_text, log_callback=print):
    """
    Quy trình Batch Replace + Delete cho CHẾ ĐỘ THƯ MỤC.

    Xử lý cả 2 loại thay đổi từ Repair Engine:
      - REPLACE: block đã được sửa nội dung / timestamp
      - DELETE : block đã bị gộp vào block khác (từ comment [MERGED])

    Sau khi chạy xong, cần chạy reindex.py để đánh lại số thứ tự.
    """
    replace_dict, delete_set = parse_patch_and_deletes(patch_text)

    if not replace_dict and not delete_set:
        log_callback("⚠️ Không tìm thấy block hợp lệ nào trong đoạn text đã dán.")
        return

    # Tự động nhận diện tiền tố
    prefix = detect_prefix(folder)
    if prefix:
        log_callback(f"🔍 Tự động nhận diện tiền tố file: '{prefix}_'")
    else:
        log_callback("🔍 Không nhận diện được tiền tố, dùng định dạng '_{file_no}.srt'")

    log_callback(f"🔎 Tìm thấy {len(replace_dict)} block cần thay thế, "
                 f"{len(delete_set)} block cần xóa")
    if delete_set:
        log_callback(f"   🗑️ Danh sách block sẽ xóa: {sorted(delete_set)}")

    # Lập bản đồ block_id -> filepath thực tế
    block_map = build_block_index_map(folder)

    # Group tất cả block_id (cả replace lẫn delete) theo file đích
    all_ids = set(replace_dict.keys()) | delete_set
    file_to_ids = defaultdict(set)
    for b_id in all_ids:
        filepath = get_target_file(b_id, folder, prefix, block_map)
        file_to_ids[filepath].add(b_id)

    # Xử lý từng file (sắp xếp theo thứ tự số tự nhiên: part_1, part_2 ... part_32)
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

    sorted_filepaths = sorted(file_to_ids.keys(), key=lambda x: natural_sort_key(os.path.basename(x)))

    for filepath in sorted_filepaths:
        ids_in_file = file_to_ids[filepath]
        filename = os.path.basename(filepath)
        log_callback(f"\n📄 Đang xử lý file: {filename}")

        if not os.path.exists(filepath):
            log_callback(f"   ⚠️ Không tìm thấy {filename}")
            continue

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().replace('\r\n', '\n').strip()
        except Exception as e:
            log_callback(f"   ⚠️ Lỗi đọc file {filename}: {e}")
            continue

        original_blocks = re.split(r'\n\s*\n', content)
        new_blocks      = []
        replaced_count  = 0
        deleted_count   = 0
        local_replace   = {k: v for k, v in replace_dict.items() if k in ids_in_file}
        local_delete    = delete_set & ids_in_file

        for ob in original_blocks:
            olines = ob.strip().split('\n')
            if not olines:
                continue
            try:
                ob_id = int(olines[0].strip())

                if ob_id in local_delete:
                    # XÓA block này (bị gộp bởi Repair Engine)
                    _log_delete(log_callback, ob_id)
                    local_delete.discard(ob_id)
                    deleted_count += 1

                elif ob_id in local_replace:
                    # REPLACE block này
                    old_text = ob.strip()
                    new_text = local_replace[ob_id]
                    _log_replace(log_callback, ob_id, old_text, new_text)
                    new_blocks.append(new_text)
                    del local_replace[ob_id]
                    replaced_count += 1

                else:
                    # Giữ nguyên
                    new_blocks.append(ob.strip())

            except ValueError:
                new_blocks.append(ob.strip())

        # Báo cáo block không tìm thấy
        for missed_id in local_replace:
            log_callback(f"   ⚠️ Block {missed_id} không tồn tại trong {filename}")
        for missed_id in local_delete:
            log_callback(f"   ⚠️ Block {missed_id} (cần xóa) không tồn tại trong {filename}")

        # Ghi đè nếu có thay đổi
        if replaced_count > 0 or deleted_count > 0:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write('\n\n'.join(new_blocks) + '\n\n')
                log_callback(
                    f"   ↳ ✅ {filename}: Thay thế {replaced_count} block, "
                    f"xóa {deleted_count} block."
                )
            except Exception as e:
                log_callback(f"   ⚠️ Lỗi lưu file {filename}: {e}")
        else:
            log_callback(f"   ↳ ℹ️ {filename}: Không có thay đổi.")

    log_callback("\n✅ Hoàn thành Batch Replace+Delete!")
    log_callback("⚠️  Nhớ chạy Reindex để đánh lại số thứ tự block sau khi xóa.")


# ==============================================================================
# CHẾ ĐỘ FILE ĐƠN LẺ
# ==============================================================================

def replace_blocks_in_file(filepath, patch_text, log_callback=print):
    """
    Quy trình Batch Replace + Delete cho MỘT FILE DUY NHẤT.

    Xử lý cả 2 loại thay đổi từ Repair Engine:
      - REPLACE: block đã được sửa nội dung / timestamp
      - DELETE : block đã bị gộp vào block khác (từ comment [MERGED])

    Sau khi chạy xong, cần chạy reindex.py để đánh lại số thứ tự.
    """
    replace_dict, delete_set = parse_patch_and_deletes(patch_text)

    if not replace_dict and not delete_set:
        log_callback("⚠️ Không tìm thấy block hợp lệ nào trong đoạn text đã dán.")
        return

    filename = os.path.basename(filepath)
    log_callback(f"\n📄 Đang xử lý file đơn lẻ: {filename}")
    log_callback(f"🔎 Tìm thấy {len(replace_dict)} block cần thay thế, "
                 f"{len(delete_set)} block cần xóa")
    if delete_set:
        log_callback(f"   🗑️ Danh sách block sẽ xóa: {sorted(delete_set)}")

    if not os.path.exists(filepath):
        log_callback(f"⚠️ Không tìm thấy file {filename}")
        return

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().replace('\r\n', '\n').strip()
    except Exception as e:
        log_callback(f"⚠️ Lỗi đọc file {filename}: {e}")
        return

    original_blocks = re.split(r'\n\s*\n', content)
    new_blocks      = []
    replaced_count  = 0
    deleted_count   = 0

    for ob in original_blocks:
        olines = ob.strip().split('\n')
        if not olines:
            continue
        try:
            ob_id = int(olines[0].strip())

            if ob_id in delete_set:
                # XÓA block này
                _log_delete(log_callback, ob_id)
                delete_set.discard(ob_id)
                deleted_count += 1

            elif ob_id in replace_dict:
                # REPLACE block này
                old_text = ob.strip()
                new_text = replace_dict[ob_id]
                _log_replace(log_callback, ob_id, old_text, new_text)
                new_blocks.append(new_text)
                del replace_dict[ob_id]
                replaced_count += 1

            else:
                # Giữ nguyên
                new_blocks.append(ob.strip())

        except ValueError:
            new_blocks.append(ob.strip())

    # Báo cáo block không tìm thấy
    for missed_id in replace_dict:
        log_callback(f"   ⚠️ Block {missed_id} không tồn tại trong file gốc để thay thế.")
    for missed_id in delete_set:
        log_callback(f"   ⚠️ Block {missed_id} (cần xóa) không tồn tại trong file gốc.")

    # Ghi đè file
    if replaced_count > 0 or deleted_count > 0:
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write('\n\n'.join(new_blocks) + '\n\n')
            log_callback(
                f"\n✅ Hoàn thành: Thay thế {replaced_count} block, "
                f"xóa {deleted_count} block trong file {filename}!"
            )
            log_callback("⚠️  Nhớ chạy Reindex để đánh lại số thứ tự block sau khi xóa.")
        except Exception as e:
            log_callback(f"⚠️ Lỗi lưu file {filename}: {e}")
    else:
        log_callback("\n⚠️ Không có block nào được thay thế hoặc xóa "
                     "(Có thể ID không khớp với file gốc).")