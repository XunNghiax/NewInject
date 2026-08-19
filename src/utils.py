import os
import json
import shutil

def get_default_capcut_path() -> str:
    """Tự động định vị đường dẫn thư mục lưu dự án của phần mềm CapCut PC trên Windows."""
    appdata = os.getenv('LOCALAPPDATA', '')
    if appdata:
        p = os.path.join(appdata, 'CapCut', 'User Data', 'Projects', 'com.lveditor.draft')
        if os.path.exists(p):
            return p
    return ""

def clean_capcut_ai_draft(draft_path: str) -> tuple[int, int]:
    """
    Xóa bỏ toàn bộ âm thanh AI khỏi dự án CapCut.
    Trả về số track và số audio đã xóa.
    """
    if not draft_path or not os.path.exists(draft_path):
        raise FileNotFoundError("Thư mục CapCut Draft không tồn tại.")
        
    json_path = draft_path if draft_path.lower().endswith(".json") else os.path.join(draft_path, "draft_content.json")
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Không tìm thấy draft_content.json tại: {json_path}")
        
    shutil.copy(json_path, json_path + ".clean.backup")
    with open(json_path, "r", encoding="utf-8") as f:
        draft = json.load(f)
        
    audio_materials_to_delete = set()
    new_tracks = []
    deleted_tracks = 0
    
    for track in draft.get("tracks", []):
        if track.get("type") == "audio" and track.get("name", "").startswith("AI_Auto_Layer_"):
            deleted_tracks += 1
            for seg in track.get("segments", []):
                mat_id = seg.get("material_id")
                if mat_id: audio_materials_to_delete.add(mat_id)
        else:
            new_tracks.append(track)
            
    draft["tracks"] = new_tracks
    
    deleted_audios = 0
    if "materials" in draft and "audios" in draft["materials"]:
        old_audios = draft["materials"]["audios"]
        new_audios = [a for a in old_audios if a.get("id") not in audio_materials_to_delete]
        draft["materials"]["audios"] = new_audios
        deleted_audios = len(old_audios) - len(new_audios)
        
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False)
        
    return deleted_tracks, deleted_audios
