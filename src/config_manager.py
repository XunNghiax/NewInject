import os
import json

class ConfigManager:
    CONFIG_PATH = "./user_data/config/user_config.json"

    @staticmethod
    def load_config() -> dict:
        if os.path.exists(ConfigManager.CONFIG_PATH):
            try:
                with open(ConfigManager.CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def save_config(cfg: dict):
        os.makedirs(os.path.dirname(ConfigManager.CONFIG_PATH), exist_ok=True)
        existing = ConfigManager.load_config()
        existing.update(cfg)
        
        # Map chéo về chuẩn V1 để UI cũ cũng đọc được
        existing['CAPCUT_JSON_PATH'] = cfg.get('last_draft', '')
        existing['SERVER_URL'] = cfg.get('last_gradio_url', '')
        existing['REF_AUDIO_PATH'] = cfg.get('last_ref_audio', '')
        existing['REF_TEXT'] = cfg.get('last_ref_text', '')
        existing['INJECT_ONLY'] = not cfg.get('enable_tts', True)

        try:
            with open(ConfigManager.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=4)
        except Exception:
            pass
