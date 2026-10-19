"""启动入口：自身负责日志落盘，避免依赖 PowerShell 的输出重定向（PS 5.1 在
Path/PATH 并存时会抛「字典中的关键字重复」）。"""
from __future__ import annotations

import pathlib
import sys

import uvicorn

LOG_DIR = pathlib.Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


class _Tee:
    """同时写文件与控制台。"""

    def __init__(self, path: pathlib.Path, stream):
        self._file = open(path, "a", encoding="utf-8", buffering=1)
        self._stream = stream

    def write(self, data):
        try:
            self._file.write(data)
        except Exception:
            pass
        try:
            self._stream.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        for target in (self._file, self._stream):
            try:
                target.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        # uvicorn 的日志格式器会访问 isatty / encoding / fileno 等属性
        if name in ("_stream", "_file"):
            raise AttributeError(name)
        return getattr(self._stream, name)


if __name__ == "__main__":
    from datetime import datetime

    log_path = LOG_DIR / "server.log"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")

    sys.stdout = _Tee(log_path, sys.stdout)
    sys.stderr = _Tee(log_path, sys.stderr)

    uvicorn.run("app.api:app", host="127.0.0.1", port=8003, log_level="info")
