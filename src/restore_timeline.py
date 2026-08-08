import os
import re

def parse_srt_blocks(content):
    blocks = []
    raw = re.split(r'\n\s*\n', content.replace('\r\n', '\n').strip())
    for b in raw:
        lines = b.strip().split('\n')
        if len(lines) >= 3:
            b_id = lines[0].strip()
            ts = lines[1].strip()
            text = '\n'.join(lines[2:]).strip()
            blocks.append({'id': b_id, 'ts': ts, 'text': text})
    return blocks

def sync_timestamps(cn_folder, vi_folder):
    """Đồng bộ lại mốc thời gian gốc từ thư mục temp_split_cn sang temp_split_vi."""
    if not os.path.exists(cn_folder) or not os.path.exists(vi_folder):
        print(f"⚠️ Thư mục không tồn tại: {cn_folder} hoặc {vi_folder}")
        return

    restored_files = 0
    for fname in os.listdir(vi_folder):
        if fname.endswith('.srt'):
            vi_path = os.path.join(vi_folder, fname)
            cn_path = os.path.join(cn_folder, fname)
            if not os.path.exists(cn_path):
                continue
            
            try:
                with open(cn_path, 'r', encoding='utf-8') as f:
                    cn_blocks = parse_srt_blocks(f.read())
                with open(vi_path, 'r', encoding='utf-8') as f:
                    vi_blocks = parse_srt_blocks(f.read())
                
                cn_ts_map = {b['id']: b['ts'] for b in cn_blocks}
                
                updated = False
                new_vi_blocks = []
                for b in vi_blocks:
                    b_id = b['id']
                    if b_id in cn_ts_map and b['ts'] != cn_ts_map[b_id]:
                        b['ts'] = cn_ts_map[b_id]
                        updated = True
                    new_vi_blocks.append(f"{b['id']}\n{b['ts']}\n{b['text']}")
                
                if updated:
                    with open(vi_path, 'w', encoding='utf-8') as f:
                        f.write('\n\n'.join(new_vi_blocks) + '\n\n')
                    print(f"✅ Đã khôi phục mốc thời gian chuẩn cho {fname}")
                    restored_files += 1
            except Exception as e:
                print(f"⚠️ Lỗi xử lý {fname}: {e}")
                
    if restored_files == 0:
        print("ℹ️ Tất cả các file đã có mốc thời gian khớp với file gốc.")

if __name__ == '__main__':
    base_dir = r"D:\Coder\Python\NewInject\downloads"
    cn_folder = os.path.join(base_dir, "temp_split_cn_双向奔赴！")
    vi_folder = os.path.join(base_dir, "temp_split_vi_双向奔赴!")
    sync_timestamps(cn_folder, vi_folder)
