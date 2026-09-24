"""密钥读取与日志脱敏。

两条硬约束（来自《模型配置与调用规范》）：
  1. 密钥绝不写入任何日志、报告、错误栈
  2. 服务只绑 127.0.0.1，密钥不出本机

实践中最容易漏的一处：模型调用失败时，很多 SDK 会把请求头打进错误栈，
而错误栈会被写进日志。所以不能只靠「我们没打印它」，
必须在日志出口做一次统一脱敏。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

_MASK = "sk-****"
_MIN_SECRET_LEN = 8

# 常见密钥形态的兜底正则，用于捕获「没见过但看起来像密钥」的串
_KEY_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-\.]{12,})"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)([A-Za-z0-9_\-\.]{12,})"),
]


class SecretStore:
    """按引用名取密钥。

    取值优先级：环境变量 > .env 文件 > config/secrets.json
    环境变量优先，因为它不出现在磁盘上。

    `.env` 会依次在 `config/.env` 与 `config/../.env`（即项目根）找，
    这是最常见的两种摆法。
    """

    def __init__(self, config_dir: Path | str = "config") -> None:
        self.config_dir = Path(config_dir)
        self._file_cache: dict[str, str] | None = None
        self._env_cache: dict[str, str] | None = None
        self._loaded: set[str] = set()

    def _load_env_file(self) -> dict[str, str]:
        """解析 .env。支持 `KEY=VALUE`、`export KEY=VALUE`、`#` 注释与引号包裹。

        也容错 `$env:KEY=VALUE`（Windows 环境变量写法）与 `SET KEY=VALUE`（cmd 写法）——
        实测中用户很可能把 shell 命令原样粘进 .env，键名带上 `$env:` 前缀就读不到了。
        这三种前缀在语义上都等价于「设置这个变量」，剥掉比报错更有用。
        """
        if self._env_cache is not None:
            return self._env_cache
        found: dict[str, str] = {}
        for candidate in (self.config_dir / ".env", self.config_dir.parent / ".env"):
            if not candidate.exists():
                continue
            try:
                text = candidate.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                key = _normalize_env_key(key)
                value = value.strip().strip("'\"")
                if key and value:
                    found.setdefault(key, value)
        self._env_cache = found
        return found

    def _load_file(self) -> dict[str, str]:
        if self._file_cache is not None:
            return self._file_cache
        path = self.config_dir / "secrets.json"
        if not path.exists():
            self._file_cache = {}
            return self._file_cache
        try:
            # 用 utf-8-sig 读：部分编辑器与 Set-Content 的 utf8 会写入 BOM，
            # 带 BOM 的文本用 utf-8 解码后首字符是 \ufeff，json.loads 会直接报错。
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            self._file_cache = {str(k): str(v) for k, v in raw.items()}
        except Exception:
            logging.getLogger(__name__).warning(
                "secrets.json 解析失败，按空处理（密钥仍可从环境变量读取）"
            )
            self._file_cache = {}
        return self._file_cache

    def get(self, ref: str | None) -> str | None:
        if not ref:
            return None
        value = os.environ.get(ref)
        if value:
            self._loaded.add(value.strip())
            return value.strip()
        value = self._load_env_file().get(ref)
        if value:
            self._loaded.add(value)
            return value
        value = self._load_file().get(ref)
        if value:
            self._loaded.add(value.strip())
            return value.strip()
        return None

    @property
    def known_values(self) -> list[str]:
        """所有已加载过的密钥明文，用于脱敏。"""
        values = set(self._loaded)
        values.update(self._load_env_file().values())
        values.update(self._load_file().values())
        return sorted(v for v in values if v and len(v) >= _MIN_SECRET_LEN)

    def source_of(self, ref: str | None) -> str | None:
        """密钥来源，便于排查，不返回明文。"""
        if not ref:
            return None
        if os.environ.get(ref):
            return "env"
        if self._load_env_file().get(ref):
            return "dotenv"
        if self._load_file().get(ref):
            return "file"
        return None

    # ── 写入 ────────────────────────────────────────────
    #
    # 写入路径最重要的一条：**不能写出「改了却不生效」**。
    #
    # 优先级是 环境变量 > .env > secrets.json。若把密钥写进 secrets.json，
    # 而 .env 里已经有一条同名且更旧的，读的时候仍然是 .env 那条——
    # 用户看到「保存成功」却依然报 401，且没有任何提示。这正是本项目
    # 反复防的那类静默失效。
    #
    # 所以写入策略是「写回当前真正生效的那一个来源」，而不是固定写某个文件。

    @property
    def env_file(self) -> Path | None:
        """当前存在的 .env 路径（优先 config/.env，其次项目根）。"""
        for candidate in (self.config_dir / ".env", self.config_dir.parent / ".env"):
            if candidate.exists():
                return candidate
        return None

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.json"

    def set(self, ref: str, value: str) -> dict[str, Any]:
        """保存密钥。返回落点与提示，**绝不回显密钥本身**。"""
        ref = (ref or "").strip()
        value = (value or "").strip()
        if not ref:
            return {"ok": False, "message": "密钥引用名为空，无法保存"}
        if not value:
            return {"ok": False, "message": "密钥为空，未做任何改动"}
        if len(value) < _MIN_SECRET_LEN:
            return {"ok": False, "message": f"密钥看起来太短（{len(value)} 个字符），已拒绝保存"}

        source = self.source_of(ref)
        if source == "env":
            return {
                "ok": False,
                "message": (
                    f"这个密钥来自环境变量 {ref}，它的优先级最高，从界面上改不了它。"
                    "请先取消该环境变量，或改用别的引用名。"
                ),
                "source": "env",
            }

        env_path = self.env_file
        in_env = bool(env_path and self._load_env_file().get(ref))
        if in_env:
            _write_env_key(env_path, ref, value)
            stored_to = "dotenv"
            path = str(env_path)
        else:
            data: dict[str, str] = {}
            if self.secrets_file.exists():
                try:
                    raw = json.loads(self.secrets_file.read_text(encoding="utf-8-sig"))
                    data = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
                except (ValueError, OSError):
                    data = {}
            data[ref] = value
            self.secrets_file.parent.mkdir(parents=True, exist_ok=True)
            self.secrets_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            stored_to = "secrets.json"
            path = str(self.secrets_file)

        self.reset_cache()
        # 写完立刻回读一次。写不进去却报成功，比不写还糟。
        actual = self.source_of(ref)
        if actual is None:
            return {"ok": False, "message": "写入后读不回来，请检查文件权限", "path": path}
        return {
            "ok": True,
            "stored_to": stored_to,
            "path": path,
            "source": actual,
            "message": f"已保存到 {stored_to}",
        }

    def clear(self, ref: str) -> dict[str, Any]:
        """删除密钥。环境变量里的删不掉，会明确说明。"""
        ref = (ref or "").strip()
        if not ref:
            return {"ok": False, "message": "密钥引用名为空"}
        source = self.source_of(ref)
        if source is None:
            return {"ok": True, "message": "本来就没有配置", "source": None}
        if source == "env":
            return {
                "ok": False,
                "message": f"密钥来自环境变量 {ref}，删不掉，请去系统里取消",
                "source": "env",
            }

        removed = False
        env_path = self.env_file
        if env_path and self._load_env_file().get(ref):
            _remove_env_key(env_path, ref)
            removed = True
        data = self._load_file()
        if ref in data:
            data.pop(ref)
            self.secrets_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            removed = True
        self.reset_cache()
        if self.source_of(ref) is not None:
            # 本该删掉的还在，说明还有别的地方藏着它
            return {"ok": False, "message": "删除后仍然读得到，请检查是否还有别的来源"}
        return {"ok": True, "message": "已清除" if removed else "本来就没有配置", "source": None}

    def reset_cache(self) -> None:
        """清掉读取缓存。写完文件必须调，否则本进程还会用旧值。"""
        self._env_cache = None
        self._file_cache = None

    def masked(self, ref: str | None) -> str | None:
        """掩码形态，只留后 4 位。用于界面显示。"""
        value = self.get(ref)
        if not value:
            return None
        return mask_value(value)


def mask_value(value: str) -> str:
    """只留后 4 位。够用来确认「是不是我以为的那一个」，又不足以泄漏。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}…{value[-4:]}"


def _write_env_key(path: Path, ref: str, value: str) -> None:
    """更新 .env 里的一行；没有就追加。

    整行重写成 `KEY=value`——顺便把用户可能粘进来的 `$env:KEY=`／`export KEY=`
    规范掉（实测真的会有人把 PowerShell 命令原样粘进 .env）。
    其余行原样保留：注释和别的键都可能有用。
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _value = stripped.partition("=")
            if _normalize_env_key(key) == ref:
                out.append(f"{ref}={value}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{ref}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def _remove_env_key(path: Path, ref: str) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _value = stripped.partition("=")
            if _normalize_env_key(key) == ref:
                continue
        out.append(line)
    path.write_text("\n".join(out).rstrip("\n") + ("\n" if out else ""), encoding="utf-8")


_ENV_KEY_PREFIXES = ("export ", "$env:", "set ", "setx ")


def _normalize_env_key(key: str) -> str:
    """把 `$env:KEY` / `export KEY` / `SET KEY` 统一成 `KEY`。"""
    stripped = key.strip()
    lowered = stripped.lower()
    for prefix in _ENV_KEY_PREFIXES:
        if lowered.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def redact(text: str, secrets: list[str] | None = None) -> str:
    """把文本里的密钥替换成掩码。用于所有日志与报告的出口。"""
    if not text:
        return text
    out = text
    for value in secrets or []:
        if value and len(value) >= _MIN_SECRET_LEN:
            out = out.replace(value, _MASK)
    for pattern in _KEY_PATTERNS:
        if pattern.groups == 1:
            out = pattern.sub(_MASK, out)
        else:
            out = pattern.sub(lambda m: m.group(1) + _MASK, out)
    return out


class RedactingFormatter(logging.Formatter):
    """日志格式化器：格式化后统一做一次脱敏。

    挂在 handler 上而不是各个调用点，保证「不可能忘记脱敏」。
    """

    def __init__(self, fmt: str | None = None, secrets: list[str] | None = None) -> None:
        super().__init__(fmt)
        self._secrets = secrets or []

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def setup_logging(secrets: list[str] | None = None, level: int = logging.INFO) -> None:
    """配置带脱敏的日志。全程只用这一个出口。"""
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s | %(message)s", secrets)
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
