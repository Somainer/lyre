"""Config loading.

MVP scope: read from env vars + sensible defaults. lyre.toml support later.

Env discovery order (highest priority first; first match wins per variable):
  1. Real OS environment (already set in the shell)
  2. `.env` in current working directory (where you invoked `lyre`)
  3. `.env` in repo root (where pyproject.toml lives)

This way you can keep a personal `.env` in the repo while overriding any value
ad-hoc with `FOO=bar lyre serve`. The .env files are NEVER committed (see
.gitignore).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _default_db_path() -> Path:
    """~/.lyre/lyre.db (cross-platform fallback to project cwd if HOME unavailable)."""
    home = Path.home()
    base = home / ".lyre"
    return base / "lyre.db"


def _default_object_store_path() -> Path:
    home = Path.home()
    return home / ".lyre" / "object_store"


def _default_memory_path() -> Path:
    """`~/.lyre/memory/` — durable knowledge (facts + persona Souls).

    Layout (auto-created by `lyre init`):
        ~/.lyre/memory/
        ├── facts/      project / domain facts agents accumulate
        └── personas/   Soul files (preferences, style) per persona

    Skills moved to top-level `~/.lyre/skills/` in B1 (PI Agent Skills
    standard); they're behavior modifiers, not memory. See
    `lyre.runtime.skills` for that layout.
    """
    home = Path.home()
    return home / ".lyre" / "memory"


def _find_repo_root_with_pyproject() -> Path | None:
    """Walk up from this file to find pyproject.toml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


_DOTENV_LOADED = False


def load_dotenv_chain() -> list[Path]:
    """Load .env files from CWD and repo root (in that priority).

    `override=False` everywhere — existing env vars and earlier-loaded files
    take precedence over later-loaded ones. Returns the list of paths actually
    loaded, for logging.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return []
    loaded: list[Path] = []
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        load_dotenv(cwd_env, override=False)
        loaded.append(cwd_env.resolve())

    root = _find_repo_root_with_pyproject()
    if root is not None:
        root_env = root / ".env"
        if root_env.is_file() and root_env.resolve() not in loaded:
            load_dotenv(root_env, override=False)
            loaded.append(root_env.resolve())
    _DOTENV_LOADED = True
    return loaded


def _parse_compact_threshold(raw: str | None) -> float:
    """Validate LYRE_COMPACT_THRESHOLD env var (defaults to 0.7).

    Must be a real number in (0, 1). Invalid values fall back to 0.7
    with no error — we don't want a malformed env var to bring the
    server down.
    """
    if raw is None:
        return 0.7
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return 0.7
    if not (0.0 < v < 1.0):
        return 0.7
    return v


@dataclass
class Config:
    db_path: Path
    object_store_path: Path
    memory_path: Path
    anthropic_api_key: str | None
    anthropic_base_url: str | None
    default_model: str
    # When set, force every wakeup to use this model, ignoring persona routing.
    # Use for dev/testing against a cheap provider (e.g. DeepSeek's Anthropic-
    # compatible endpoint). Set LYRE_MODEL_OVERRIDE=deepseek-v4-pro in env.
    model_override: str | None = None
    # Fraction of the model's context_window above which the agent loop
    # auto-compacts mid-wakeup. 0.7 = compact when input_tokens crosses
    # 70% of the window, leaving room for the next turn's output +
    # tool_results. Set LYRE_COMPACT_THRESHOLD in env to override
    # (must be 0 < x < 1).
    compact_threshold: float = 0.7

    @classmethod
    def from_env(cls) -> Config:
        # Load .env files first (no-op if already loaded once in this process).
        load_dotenv_chain()
        db_path = Path(os.getenv("LYRE_DB_PATH") or _default_db_path())
        os_root = Path(os.getenv("LYRE_OBJECT_STORE") or _default_object_store_path())
        mem_root = Path(os.getenv("LYRE_MEMORY_PATH") or _default_memory_path())
        # Ensure parent directories exist
        db_path.parent.mkdir(parents=True, exist_ok=True)
        os_root.mkdir(parents=True, exist_ok=True)
        mem_root.mkdir(parents=True, exist_ok=True)
        return cls(
            db_path=db_path,
            object_store_path=os_root,
            memory_path=mem_root,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL"),
            default_model=os.getenv("LYRE_DEFAULT_MODEL", "claude-sonnet-4-6"),
            model_override=os.getenv("LYRE_MODEL_OVERRIDE") or None,
            compact_threshold=_parse_compact_threshold(
                os.getenv("LYRE_COMPACT_THRESHOLD")
            ),
        )
