import os
import re

def natural_sort_key(s):
    """
    Tách chuỗi thành các phần chữ và số để sắp xếp theo số học (Natural Sort).
    VD: '0609_2.srt' -> ['0609_', 2, '.srt']
    """
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

def parse_srt_structure(filepath):
    """
    Đọc file SRT và trích xuất cấu trúc thành dạng dict.
    Output: { "298": "00:08:35,066 --> 00:08:37,900", ... }
    """
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

def compare_srt_folders(folder_a, folder_b, log_callback=print):
    """
    So sánh cấu trúc Block ID và Timeline giữa 2 thư mục SRT.
    """
    if not os.path.exists(folder_a) or not os.path.exists(folder_b):
        log_callback("❌ Lỗi: Thư mục không tồn tại.")
        return

    files_a = {f for f in os.listdir(folder_a) if f.endswith('.srt')}
    files_b = {f for f in os.listdir(folder_b) if f.endswith('.srt')}

    # Sử dụng natural_sort_key để sắp xếp danh sách file theo số thứ tự
    common_files = sorted(list(files_a & files_b), key=natural_sort_key)
    only_a = sorted(list(files_a - files_b), key=natural_sort_key)
    only_b = sorted(list(files_b - files_a), key=natural_sort_key)

    if not common_files:
        log_callback("⚠️ Không tìm thấy file .srt nào có tên giống nhau giữa 2 thư mục để so sánh.")
        return

    log_callback(f"🔎 Đang so sánh {len(common_files)} file chung giữa 2 thư mục...\n")
    
    # Danh sách lưu trữ tên các file có sự khác biệt
    files_with_diffs = []

    for filename in common_files:
        path_a = os.path.join(folder_a, filename)
        path_b = os.path.join(folder_b, filename)

        struct_a = parse_srt_structure(path_a)
        struct_b = parse_srt_structure(path_b)

        diffs = []
        
        # Lấy tất cả các ID có trong cả 2 file để so sánh (Sắp xếp theo số học)
        all_keys = sorted(set(list(struct_a.keys()) + list(struct_b.keys())), key=lambda x: int(x) if x.isdigit() else 0)
        
        for key in all_keys:
            if key not in struct_a:
                diffs.append(f"   - Block {key}: Bị thiếu ở Thư mục A")
            elif key not in struct_b:
                diffs.append(f"   - Block {key}: Bị thiếu ở Thư mục B")
            else:
                if struct_a[key] != struct_b[key]:
                    diffs.append(f"   - Block {key}:\n      + [Thư mục A]: {struct_a[key]}\n      + [Thư mục B]: {struct_b[key]}")
        
        if diffs:
            files_with_diffs.append(filename) # Lưu lại tên file lỗi
            log_callback(f"⚠️ {filename}: Tìm thấy {len(diffs)} điểm khác biệt:")
            for d in diffs:
                log_callback(d)
            log_callback("-" * 45)
        else:
            log_callback(f"✅ {filename}: Trùng khớp 100% ID và Timeline.")

    log_callback("\n" + "=" * 50)
    
    if only_a:
        log_callback(f"ℹ️ Có {len(only_a)} file CHỈ CÓ ở Thư mục A: {', '.join(only_a)}")
    if only_b:
        log_callback(f"ℹ️ Có {len(only_b)} file CHỈ CÓ ở Thư mục B: {', '.join(only_b)}")

    # Xử lý log tổng kết ở cuối
    if not files_with_diffs and not only_a and not only_b:
        log_callback("\n🎉 TUYỆT VỜI! Tất cả các file đều trùng khớp 100% về Block ID và Timeline.")
    else:
        log_callback(f"\n✅ Hoàn thành phân tích. Có {len(files_with_diffs)} file bị sai lệch nội dung.")
        
        # Log danh sách file lỗi
        if files_with_diffs:
            log_callback("\n📋 DANH SÁCH CÁC FILE CÓ LỖI / SAI LỆCH:")
            for bad_file in files_with_diffs:
                log_callback(f"  ❌ {bad_file}")