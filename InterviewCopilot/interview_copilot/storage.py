"""Atomic local persistence; API keys use Windows DPAPI or macOS Keychain."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import hashlib
import os
from pathlib import Path
import re
import sys
import threading
from datetime import datetime, timezone
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_SETTINGS = {
    "deepseek_model": "deepseek-flash", "asr_provider": "local",
    "whisper_model": "base", "asr_language": "auto",
    "asr_quality": "balanced",
    "cloud_asr_url": "", "cloud_asr_model": "",
    "silence_ms": 700, "min_speech_ms": 300, "max_segment_s": 12,
    "energy_threshold": 0.008, "auto_answer": False,
    "answer_language": "auto", "answer_style": "concise", "device_id": None,
}
PROFILE_FIELDS = ("name", "company", "role", "resume", "jd", "company_notes", "materials", "source_notes", "custom_prompt")
KEYCHAIN_SERVICE = "com.interviewcopilot.credentials"


def _mac_keychain():
    # Explicitly choose the native backend: no config-selected/plaintext fallback.
    # The native Security framework receives secrets in process, never argv.
    try:
        from keyring.backends.macOS import Keyring
        return Keyring()
    except Exception:
        raise RuntimeError("无法连接 macOS 钥匙串。请确认程序依赖已完整安装。") from None


def _keychain_account(root: Path) -> str:
    # Isolated test/custom data directories never read another installation's keys.
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def _read_mac_secrets(root: Path) -> dict:
    raw = _mac_keychain().get_password(KEYCHAIN_SERVICE, _keychain_account(root))
    if raw is None:
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict) or any(not isinstance(v, str) for v in loaded.values()):
        raise ValueError("Invalid credential record")
    return loaded


def _dpapi(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("API 密钥保存仅支持 Windows DPAPI；请在 Windows 上运行。")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        func = crypt.CryptUnprotectData
        func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        func = crypt.CryptProtectData
        func.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = func(ctypes.byref(source), "InterviewCompanion", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError("Windows 无法加密或解密密钥，请使用保存密钥时的 Windows 账户。")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            self.root.chmod(0o700)
        (self.root / "sessions").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        initial_settings = DEFAULT_SETTINGS.copy()
        if getattr(sys, "_interview_cloud_edition", False):
            initial_settings["asr_provider"] = "qwen"
        initial = {"settings": initial_settings, "profiles": [], "active_profile_id": None}
        self.state = self._read("state.json", initial)
        self.state["settings"] = {**initial_settings, **self.state.get("settings", {})}
        for profile in self.state["profiles"]:
            profile.setdefault("custom_prompt", "")
        if not self.state["profiles"]:
            self.save_profile({"name": "我的第一场面试"})
        self.secrets = {}
        self.secret_error = None
        if sys.platform == "darwin":
            try:
                self.secrets = _read_mac_secrets(self.root)
            except Exception:
                self.secret_error = "无法读取 macOS 钥匙串中的 API 密钥。请允许本应用访问钥匙串，然后重新打开工具。"
        elif (self.root / "secrets.dpapi").exists():
            try:
                raw = base64.b64decode((self.root / "secrets.dpapi").read_bytes(), validate=True)
                self.secrets = json.loads(_dpapi(raw, decrypt=True).decode("utf-8"))
            except Exception:
                self.secret_error = "已保存的 API 密钥无法解密，请在设置中重新填写并保存。"

    def _read(self, filename, fallback):
        path = self.root / filename
        if not path.exists():
            return fallback
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"本地资料 {filename} 无法读取。请保留原文件后修复，程序不会覆盖它。") from exc

    def _write(self, filename, value):
        path = self.root / filename
        temp = path.with_name(path.name + ".tmp")
        with self.lock:
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)

    def public_settings(self):
        return {**self.state["settings"], "deepseek_key_set": bool(self.secrets.get("deepseek_key")),
                "cloud_asr_key_set": bool(self.secrets.get("cloud_asr_key"))}

    def update_settings(self, patch):
        with self.lock:
            secrets = self.secrets.copy()
            if sys.platform == "darwin" and self.secret_error and any(
                    patch.get(name) for name in ("deepseek_key", "cloud_asr_key", "delete_deepseek_key", "delete_cloud_asr_key")):
                # Retry access before replacing an unreadable combined record;
                # editing one key must never discard the other stored key.
                try:
                    secrets = _read_mac_secrets(self.root)
                except Exception:
                    raise RuntimeError("请先解锁 macOS 钥匙串并允许本应用访问，原有 API 密钥尚未修改。") from None
            for key in ("deepseek_key", "cloud_asr_key"):
                if patch.get("delete_" + key):
                    secrets.pop(key, None)
                elif patch.get(key, "").strip():
                    secrets[key] = patch[key].strip()
            if secrets != self.secrets:
                if sys.platform == "darwin":
                    try:
                        _mac_keychain().set_password(KEYCHAIN_SERVICE, _keychain_account(self.root), json.dumps(secrets))
                    except Exception:
                        raise RuntimeError("API 密钥未能保存到 macOS 钥匙串。请解锁钥匙串并允许本应用访问后重试。") from None
                else:
                    encrypted = base64.b64encode(_dpapi(json.dumps(secrets).encode("utf-8")))
                    secret_path = self.root / "secrets.dpapi"
                    temp = secret_path.with_suffix(".tmp")
                    temp.write_bytes(encrypted)
                    os.replace(temp, secret_path)
                self.secrets = secrets
                self.secret_error = None
            self.state["settings"].update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
            self._write("state.json", self.state)
        return self.public_settings()

    def save_profile(self, patch):
        with self.lock:
            profile = next((p for p in self.state["profiles"] if p["id"] == patch.get("id")), None)
            if profile is None:
                if patch.get("id"):
                    raise KeyError("资料档案不存在")
                if len(self.state["profiles"]) >= 100:
                    raise ValueError("最多保存 100 个面试档案，请先删除不再使用的档案。")
                profile = {"id": uuid4().hex, **{key: "" for key in PROFILE_FIELDS}}
                self.state["profiles"].append(profile)
            profile.update({k: v for k, v in patch.items() if k in PROFILE_FIELDS})
            profile["name"] = profile["name"].strip() or "未命名面试"
            profile["updated_at"] = now()
            if not self.state["active_profile_id"]:
                self.state["active_profile_id"] = profile["id"]
            self._write("state.json", self.state)
            return dict(profile)

    def active_profile(self):
        return next((dict(p) for p in self.state["profiles"] if p["id"] == self.state["active_profile_id"]), {})

    def activate(self, profile_id):
        if not any(p["id"] == profile_id for p in self.state["profiles"]):
            raise KeyError("资料档案不存在")
        self.state["active_profile_id"] = profile_id
        self._write("state.json", self.state)

    def delete_profile(self, profile_id):
        if len(self.state["profiles"]) == 1:
            raise ValueError("请至少保留一个面试档案。可以清空其中的资料。")
        if not any(p["id"] == profile_id for p in self.state["profiles"]):
            raise KeyError("资料档案不存在")
        self.state["profiles"] = [p for p in self.state["profiles"] if p["id"] != profile_id]
        if self.state["active_profile_id"] == profile_id:
            self.state["active_profile_id"] = self.state["profiles"][0]["id"]
        self._write("state.json", self.state)

    def save_session(self, session, archive=False):
        clean = {**session, "listening": False}
        self._write(f"sessions/{session['id']}.json" if archive else "current-session.json", clean)

    def load_session(self):
        session = self._read("current-session.json", None)
        if session:
            session["listening"] = False
            for answer in session.get("answers", []):
                if answer.get("status") == "streaming":
                    answer["status"] = "cancelled"
        return session

    def list_sessions(self):
        result = []
        for path in sorted((self.root / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
                result.append({"id": s["id"], "at": s.get("at"), "name": s.get("name", "面试记录"),
                               "answer_count": len(s.get("answers", [])), "transcript_count": len(s.get("transcripts", []))})
            except (ValueError, KeyError, OSError):
                continue
        return result

    def get_session(self, session_id):
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise KeyError("记录不存在")
        session = self._read(f"sessions/{session_id}.json", None)
        if session is None:
            raise KeyError("记录不存在")
        return session
