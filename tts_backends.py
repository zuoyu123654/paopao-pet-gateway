# -*- coding: utf-8 -*-
"""语音合成后端：GPT-SoVITS / 豆包 2.0 / edge-tts / 小米 MiMo / Windows 本地语音。

设计：网关不自己发明配置，**直接读桌宠自己的 config.json**
（`%APPDATA%\\dsh-pet-standalone-webm-chat\\config.json`）里的 `voice_chime_*`，
API Key 走同一个系统钥匙串（keyring service = `dsh-pet-standalone`）。
用户在桌宠设置里配好语音，这边 `backend: "pet"` 就能直接用。

约定：
- 所有非 GPT-SoVITS 的后端都产出**一段 wav 文件**（mp3 先过 ffmpeg），交给
  后面那条「整句合成 → 顺序播放」的流程；只有 GPT-SoVITS 支持真正的流式。
- 缺 Key / 缺依赖 / 网络失败一律抛 `TtsUnavailable`，由网关退回 GPT-SoVITS。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.request
import uuid

KEYRING_SERVICE = "dsh-pet-standalone"
SECRET_REFS = {"mimo": "tts/mimo", "doubao": "tts/doubao_api_key"}
PET_CONFIG_DEFAULT = os.path.expandvars(
    r"%APPDATA%\dsh-pet-standalone-webm-chat\config.json"
)
KNOWN = ("gpt-sovits", "doubao", "edge", "mimo", "sapi")


class TtsUnavailable(RuntimeError):
    """这个后端现在用不了（缺 Key / 缺依赖 / 网络）。"""


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def load_pet_voice_cfg(path: str = "") -> dict:
    """读桌宠 config.json 里的 `voice_chime_*`；读不到就返回空字典。"""
    target = path or PET_CONFIG_DEFAULT
    try:
        with open(target, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if k.startswith("voice_chime")}


def read_secret(ref: str) -> str:
    try:
        import keyring

        return str(keyring.get_password(KEYRING_SERVICE, ref) or "")
    except Exception:
        return ""


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for guess in (r"C:\QQBot\Python312\Scripts\ffmpeg.exe", os.path.join(os.getcwd(), "ffmpeg.exe")):
        if os.path.exists(guess):
            return guess
    raise TtsUnavailable("找不到 ffmpeg，转不了 wav")


def _to_wav(src: str, dst: str, rate: int = 32000) -> None:
    """把任意音频转成单声道 16bit wav（和 GPT-SoVITS 那条一致，音量/播放都能吃）。"""
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-i", src,
         "-ac", "1", "-ar", str(rate), "-sample_fmt", "s16", dst],
        check=True, timeout=120, creationflags=_no_window(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class TtsBackend:
    """选一个后端，把一句话合成成 wav。`backend` 写 `pet` 就跟着桌宠的设置走。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pet = load_pet_voice_cfg(str(cfg.get("pet_config_path") or ""))
        self.name = self._pick()

    # ---------------------------------------------------------------- 选择
    def _pick(self) -> str:
        want = str(self.cfg.get("backend") or "pet").strip().lower()
        if want in ("", "pet", "auto", "follow"):
            want = str(self.pet.get("voice_chime_tts_backend") or "gpt-sovits").strip().lower()
        return want if want in KNOWN else "gpt-sovits"

    @property
    def streams(self) -> bool:
        return self.name == "gpt-sovits"

    def flavor(self) -> str:
        """进缓存键：换后端/换音色要换键，换 Key 不换。"""
        p = self.pet
        if self.name == "edge":
            return "edge|%s|%s" % (p.get("voice_chime_voice"), p.get("voice_chime_rate"))
        if self.name == "mimo":
            return "mimo|%s|%s|%s" % (
                p.get("voice_chime_mimo_model"), p.get("voice_chime_mimo_voice"),
                p.get("voice_chime_mimo_style"))
        if self.name == "doubao":
            return "doubao|%s|%s" % (
                p.get("voice_chime_doubao_speaker"), p.get("voice_chime_doubao_pitch"))
        if self.name == "sapi":
            return "sapi|%s|%s" % (p.get("voice_chime_sapi_voice"), p.get("voice_chime_sapi_rate"))
        return ""

    def describe(self) -> str:
        return {
            "gpt-sovits": "GPT-SoVITS 本地（流式）",
            "edge": "edge-tts（%s）" % self.pet.get("voice_chime_voice", ""),
            "mimo": "小米 MiMo（%s）" % self.pet.get("voice_chime_mimo_voice", ""),
            "doubao": "豆包 2.0（%s）" % self.pet.get("voice_chime_doubao_speaker", ""),
            "sapi": "Windows 本地语音（%s）" % (self.pet.get("voice_chime_sapi_voice") or "自动"),
        }.get(self.name, self.name)

    # ---------------------------------------------------------------- 合成
    def render(self, text: str, out_path: str) -> str:
        """合成一句话到 wav，返回路径。失败抛 TtsUnavailable。"""
        handler = {
            "edge": self._edge,
            "mimo": self._mimo,
            "doubao": self._doubao,
            "sapi": self._sapi,
        }.get(self.name)
        if handler is None:
            raise TtsUnavailable("后端 %s 不支持整句渲染" % self.name)
        tmp = out_path + ".raw"
        try:
            handler(text, tmp)
            _to_wav(tmp, out_path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
            raise TtsUnavailable("%s 没产出音频" % self.name)
        return out_path

    # ------------------------------------------------------------ 各家实现
    def _edge(self, text: str, out_path: str) -> None:
        try:
            import asyncio

            import edge_tts
        except Exception as exc:  # noqa: BLE001
            raise TtsUnavailable("edge-tts 没装：pip install edge-tts") from exc
        voice = str(self.pet.get("voice_chime_voice") or "zh-CN-XiaoxiaoNeural")
        rate = int(self.pet.get("voice_chime_rate") or 0)
        vol = int(self.pet.get("voice_chime_volume") or 80)
        kw = {"rate": "%+d%%" % rate, "volume": "%+d%%" % (vol - 100)}
        pitch = int(self.pet.get("voice_chime_pitch") or 0)
        if pitch:
            kw["pitch"] = "%+dHz" % pitch

        async def _run():
            await edge_tts.Communicate(text, voice, **kw).save(out_path)

        asyncio.run(_run())

    def _mimo(self, text: str, out_path: str) -> None:
        key = read_secret(SECRET_REFS["mimo"])
        if not key:
            raise TtsUnavailable("MiMo 没有 API Key（桌宠钥匙串里没读到）")
        model = str(self.pet.get("voice_chime_mimo_model") or "mimo-v2.5-tts")
        voice = str(self.pet.get("voice_chime_mimo_voice") or "冰糖")
        style = str(self.pet.get("voice_chime_mimo_style") or "")
        messages = []
        if style:
            messages.append({"role": "user", "content": style})
        messages.append({"role": "assistant", "content": text})
        audio = {"format": "wav"}
        if model != "mimo-v2.5-tts-voicedesign":
            audio["voice"] = voice
        body = {"model": model, "messages": messages, "audio": audio, "stream": False}
        req = urllib.request.Request(
            "https://api.xiaomimimo.com/v1/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "api-key": key, "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        try:
            data = payload["choices"][0]["message"]["audio"]["data"]
        except Exception as exc:  # noqa: BLE001
            raise TtsUnavailable("MiMo 返回里没有音频：%s" % str(payload)[:120]) from exc
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(data))

    def _doubao(self, text: str, out_path: str) -> None:
        key = read_secret(SECRET_REFS["doubao"])
        if not key:
            raise TtsUnavailable("豆包没有 API Key（桌宠钥匙串里没读到）")
        speaker = str(self.pet.get("voice_chime_doubao_speaker") or "zh_female_vv_uranus_bigtts")
        body = {"user": {"uid": "dsh-pet"},
                "req_params": {"text": text, "speaker": speaker,
                               "audio_params": {"format": "mp3", "sample_rate": 24000}}}
        req = urllib.request.Request(
            "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Api-Key": key,
                     "X-Api-Resource-Id": "seed-tts-2.0",
                     "X-Api-Request-Id": str(uuid.uuid4())},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
        chunks = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("event:"):
                continue
            payload = line[5:].strip() if line.startswith("data:") else line
            try:
                frame = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            data = frame.get("data")
            if isinstance(data, str) and data:
                try:
                    chunks.append(base64.b64decode(data))
                except Exception:  # noqa: BLE001
                    continue
                continue
            code = frame.get("code")
            if code not in (None, 0, 20000000):
                raise TtsUnavailable("豆包返回 %s：%s" % (code, frame.get("message", "")))
        if not chunks:
            raise TtsUnavailable("豆包没吐音频")
        with open(out_path, "wb") as f:
            f.write(b"".join(chunks))

    def _sapi(self, text: str, out_path: str) -> None:
        try:
            import comtypes
            import comtypes.client
        except Exception as exc:  # noqa: BLE001
            raise TtsUnavailable("comtypes 没装：pip install comtypes") from exc
        comtypes.CoInitialize()
        try:
            voice = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
            wanted = str(self.pet.get("voice_chime_sapi_voice") or "").strip()
            voices = voice.GetVoices()
            for i in range(voices.Count):
                item = voices.Item(i)
                desc = str(item.GetDescription())
                if not wanted or wanted.lower() in desc.lower() or desc == wanted:
                    voice.Voice = item
                    break
            voice.Rate = max(-10, min(10, int(self.pet.get("voice_chime_sapi_rate") or 0)))
            stream = comtypes.client.CreateObject("SAPI.SpFileStream", dynamic=True)
            stream.Open(str(out_path), 3)   # SSFM_CREATE
            voice.AudioOutputStream = stream
            try:
                voice.Speak(str(text))
            finally:
                stream.Close()
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
