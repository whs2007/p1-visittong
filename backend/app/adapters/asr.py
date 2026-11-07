"""ASR 适配器：mock / faster-whisper / 外部命令，多后端可切换。

- 默认 mock（离线演示，零依赖，返回预置转写）；
- `asr_backend=faster_whisper` 时尝试本地 faster-whisper（需 pip 安装与模型下载），
  失败自动回退 mock 并给出原因，绝不阻塞业务；
- `asr_backend=command` 时执行 `asr_command` 模板（{file} 占位），适用于接公司网关。

返回结构统一为 {text, backend, fallback_reason}，便于 API/前端展示"当前是否 mock"。
"""
from __future__ import annotations

from typing import Any

MOCK_TRANSCRIPT = (
    "今天和客户沟通了新产品方案，对方对数据看板很感兴趣，"
    "希望我们下周提供试用账号，并约了下周三上午再详细演示一次。"
)


def transcribe(
    file_path: str | None = None,
    *,
    backend: str = "mock",
    model: str = "small",
    language: str = "zh",
    command: str = "",
) -> dict[str, Any]:
    """返回 {text, backend, fallback_reason}；任何失败都回退 mock。"""
    if backend == "faster_whisper":
        return _faster_whisper(file_path, model, language)
    if backend == "command":
        return _command(file_path, command)
    return {"text": MOCK_TRANSCRIPT, "backend": "mock", "fallback_reason": ""}


def _faster_whisper(file_path: str | None, model: str, language: str) -> dict[str, Any]:
    if not file_path:
        return {"text": MOCK_TRANSCRIPT, "backend": "mock", "fallback_reason": "faster_whisper 需要音频文件"}
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"text": MOCK_TRANSCRIPT, "backend": "mock", "fallback_reason": f"未安装 faster-whisper：{exc}"}
    try:
        model_obj = WhisperModel(model, device="cpu", compute_type="int8")
        segments, _info = model_obj.transcribe(file_path, language=language)
        text = "".join(segment.text for segment in segments).strip()
        if not text:
            raise RuntimeError("转写结果为空")
        return {"text": text, "backend": "faster_whisper", "fallback_reason": ""}
    except Exception as exc:  # noqa: BLE001
        return {"text": MOCK_TRANSCRIPT, "backend": "mock", "fallback_reason": f"faster-whisper 失败：{exc}"}


def _command(file_path: str | None, command: str) -> dict[str, Any]:
    if not file_path or not command:
        return {"text": MOCK_TRANSCRIPT, "backend": "mock", "fallback_reason": "command 需要文件与 asr_command"}
    import shlex
    import subprocess

    try:
        argv = [part.replace("{file}", file_path) for part in shlex.split(command)]
        output = subprocess.run(argv, capture_output=True, timeout=60, check=True)
        text = output.stdout.decode("utf-8", "ignore").strip()
        if not text:
            raise RuntimeError("命令输出为空")
        return {"text": text, "backend": "command", "fallback_reason": ""}
    except Exception as exc:  # noqa: BLE001
        return {"text": MOCK_TRANSCRIPT, "backend": "mock", "fallback_reason": f"ASR 命令失败：{exc}"}