import os
import re


def time_to_ms(time_str):
    """Chuyển đổi chuỗi thời gian SRT (HH:MM:SS,ms) thành mili-giây."""
    h, m, s_ms = time_str.split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def process_single_srt(input_file, output_file, log_callback=print):
    """
    Hàm xử lý Re-index và Check Timeline cho 1 file duy nhất.

    Thực hiện:
      1. Đánh lại số thứ tự block từ 1.
      2. Kiểm tra tính tuần tự timestamp (phát hiện ngược thời gian).
      3. Kiểm tra chồng chéo timestamp giữa 2 block liền kề.
      4. Lọc bỏ các dòng comment [MERGED] do Repair Engine để lại.
      5. CHỈNH SỬA: Kiểm tra và thay thế dấu "!" thành " !" trong nội dung text.
    """
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # Tách block theo dòng trống
        raw_blocks = content.strip().split('\n\n')
        new_blocks = []

        previous_start_time = -1
        previous_end_time   = -1
        overlap_count       = 0
        out_of_order_count  = 0
        new_index           = 1  # Counter re-index

        for block in raw_blocks:
            lines = block.split('\n')
            if not lines:
                continue

            # ── Lọc bỏ dòng comment [MERGED] do Repair Engine để lại ──
            clean_lines = [
                line for line in lines
                if not line.strip().startswith('; [MERGED:')
                and not line.strip().startswith(';[MERGED:')
            ]

            # Bỏ qua block rỗng sau khi lọc comment
            if len(clean_lines) < 2:
                continue

            # Bỏ qua block không có dòng timestamp hợp lệ
            timeline_match = None
            for cl in clean_lines[1:]:
                timeline_match = re.search(
                    r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})',
                    cl
                )
                if timeline_match:
                    break

            if not timeline_match:
                continue

            old_index    = clean_lines[0].strip()
            # Gán lại số thứ tự
            clean_lines[0] = str(new_index)

            start_str = timeline_match.group(1)
            end_str   = timeline_match.group(2)
            start_ms  = time_to_ms(start_str)
            end_ms    = time_to_ms(end_str)

            # ── Kiểm tra tính tuần tự (ngược thời gian) ──
            if start_ms < previous_start_time:
                log_callback(
                    f"   🚨 Lỗi: Block {new_index} (cũ: {old_index}) "
                    f"ngược thời gian (Bắt đầu: {start_str})"
                )
                out_of_order_count += 1

            # ── Kiểm tra chồng chéo timestamp ──
            elif start_ms < previous_end_time:
                log_callback(
                    f"   ⚠️ Cảnh báo: Block {new_index} (cũ: {old_index}) "
                    f"đè timeline lên block trước"
                )
                overlap_count += 1

            previous_start_time = start_ms
            previous_end_time   = end_ms

            # ── Xử lý thêm khoảng trắng trước dấu chấm than "!" ──
            # Lặp qua các dòng (bỏ qua dòng số thứ tự 0)
            for i in range(1, len(clean_lines)):
                # Nếu dòng không chứa ký hiệu timeline '-->' thì đó là dòng text phụ đề
                if '-->' not in clean_lines[i]:
                    # Chèn 1 khoảng trắng trước nhóm dấu ! nếu trước đó chưa có khoảng trắng
                    clean_lines[i] = re.sub(r'(?<! )(!+)', r' \1', clean_lines[i])

            new_blocks.append('\n'.join(clean_lines))
            new_index += 1

        # Lưu file output
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(new_blocks) + '\n')

        # Báo cáo tóm tắt
        total = new_index - 1
        if overlap_count > 0 or out_of_order_count > 0:
            log_callback(
                f"   ↳ ⚠️ Xong {os.path.basename(input_file)}: "
                f"{total} block | {out_of_order_count} lỗi NGƯỢC | "
                f"{overlap_count} lỗi ĐÈ."
            )
        else:
            log_callback(
                f"   ↳ ✅ Xong {os.path.basename(input_file)}: "
                f"Timeline chuẩn ({total} block)."
            )

    except Exception as e:
        log_callback(
            f"   ❌ Lỗi khi xử lý file {os.path.basename(input_file)}: {e}"
        )


def process_and_renumber_srt(in_path, out_path, log_callback=print):
    """
    Hàm chính kết nối với GUI.
    Tự động nhận diện in_path là FILE hay THƯ MỤC để xử lý tương ứng.
    """
    if not os.path.exists(in_path):
        log_callback(f"❌ Lỗi: Không tìm thấy đường dẫn gốc '{in_path}'.")
        return

    # ── CHẾ ĐỘ 1: XỬ LÝ 1 FILE ──────────────────────────────────────
    if os.path.isfile(in_path):
        log_callback("📄 CHẾ ĐỘ: Reindex & Kiểm tra timeline 1 file đơn lẻ...\n")
        process_single_srt(in_path, out_path, log_callback)

        log_callback("=" * 50)
        log_callback(f"📁 File đã xử lý được lưu tại: {out_path}")

    # ── CHẾ ĐỘ 2: XỬ LÝ THƯ MỤC ─────────────────────────────────────
    elif os.path.isdir(in_path):
        log_callback("📁 CHẾ ĐỘ: Reindex & Kiểm tra timeline hàng loạt...\n")

        out_dir = (
            os.path.dirname(out_path)
            if out_path.lower().endswith('.srt')
            else out_path
        )
        os.makedirs(out_dir, exist_ok=True)

        srt_files = sorted(
            f for f in os.listdir(in_path) if f.lower().endswith('.srt')
        )
        if not srt_files:
            log_callback(f"⚠️ Không tìm thấy file .srt nào trong thư mục: {in_path}")
            return

        log_callback(f"🔍 Tìm thấy {len(srt_files)} file SRT. Bắt đầu chạy:\n")

        for filename in srt_files:
            input_file = os.path.join(in_path, filename)

            # Tránh tự ghi đè nếu IN và OUT cùng thư mục
            if in_path == out_dir:
                name, ext = os.path.splitext(filename)
                out_name  = f"{name}_reindexed{ext}"
            else:
                out_name = filename

            output_file = os.path.join(out_dir, out_name)
            process_single_srt(input_file, output_file, log_callback)

        log_callback("\n" + "=" * 50)
        log_callback(f"🎉 HOÀN TẤT! Đã xử lý {len(srt_files)} file.")
        log_callback(f"📁 Thư mục lưu kết quả: {out_dir}")

    else:
        log_callback(f"❌ Đường dẫn không hợp lệ: {in_path}")


if __name__ == "__main__":
    file_dau_vao = './source/srt/0609/merged_output.srt'
    file_dau_ra  = './source/srt/0609/merged_output_fixed.srt'
    process_and_renumber_srt(file_dau_vao, file_dau_ra)