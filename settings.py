"""
VOID-WALKER v2.0 — Gestionnaire de settings persistant
"""
import json, os, copy
from config import DEFAULT_SETTINGS

SETTINGS_FILE = "voidwalker_config.json"

class Settings:
    def __init__(self):
        self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self._last_mtime = 0.0
        self.load()

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as f:
                    saved = json.load(f)
                self._data = copy.deepcopy(DEFAULT_SETTINGS)
                self._merge(self._data, saved)
                self._last_mtime = os.path.getmtime(SETTINGS_FILE)
            except Exception as e:
                print(f"[Settings] Erreur chargement: {e}")

    def _merge(self, base, update):
        for k, v in update.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge(base[k], v)
            else:
                base[k] = v

    def save(self):
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, *keys):
        try:
            if os.path.exists(SETTINGS_FILE):
                mtime = os.path.getmtime(SETTINGS_FILE)
                if mtime > self._last_mtime:
                    self.load()
        except Exception:
            pass

        d = self._data
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
            else:
                return None
        return d

    def set(self, *keys_and_value):
        *keys, value = keys_and_value
        d = self._data
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        self.save()

    def get_active_blockchains(self):
        return [bc for bc, on in self._data["blockchains"].items() if on]

    def all(self):
        return self._data

SETTINGS = Settings()
