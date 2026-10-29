"""Machine translation into English on the reader's request (ADR-0013).

One ``llama-server`` (llama.cpp) runs MiLMMT-46-12B (GGUF Q8_0) on the GPU beside the API's search
models, started when a reader first asks for a translation and stopped after ``idle_seconds`` without
one. It runs in its own transient scope with a 4 GB host-memory cap (systemd-run), never inside the
API's cgroup: loading 12.5 GB of weights through the page cache would press on the API's memory limit.
It is started only when the GPU has ``min_free_vram_mb`` free (the API's models move between about 4
and 4.5 GB; the translator needs about 14.5 GB). A GPU index build that holds the memory makes a
request answer "busy", never fail over to the CPU.

Each segment is translated at temperature 0 with the bake-off's prompt
(docs/research/MT-BAKEOFF-2026-10-24.md), checked (:mod:`wsp_oracle.mtcheck`) and cached in SQLite by
model, prompt version, language and text, so a page read twice is translated once. A timeout or a
server error is not cached.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from wsp_core.errors import TranslationUnavailableError as TranslationUnavailable
from wsp_oracle.config import TranslationConfig
from wsp_oracle.mtcheck import CJK, PLACEHOLDER, check

log = logging.getLogger("wsp.oracle.translation")

PROMPT_VERSION = "milmmt-tags-1"
UNIT = "whisipedia-translate"

# The languages the reader may ask for: the model's languages that the library holds most of. The
# name is the one MiLMMT's prompt expects.
LANGUAGES = {
    "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "cs": "Czech", "sv": "Swedish", "da": "Danish",
    "no": "Norwegian", "nb": "Norwegian", "fi": "Finnish", "ro": "Romanian", "hu": "Hungarian",
    "el": "Greek", "tr": "Turkish", "ar": "Arabic", "fa": "Persian", "he": "Hebrew", "hi": "Hindi",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "vi": "Vietnamese", "id": "Indonesian",
    "th": "Thai", "ceb": "Cebuano",
}  # fmt: skip
_TRAD = set("這們說國會來時個為與學發後實對經點過還當開關體從無機長間問題東區於歐電動們麼現進種內")
_SIMP = set("这们说国会来时个为与学发后实对经点过还当开关体从无机长间问题东区于欧电动们么现进种内")


@dataclass(frozen=True, slots=True)
class Segment:
    text: str | None  # the English text, or None when it did not pass its checks
    ok: bool
    reason: str | None = None
    cached: bool = False
    rejected: str | None = None  # what the model wrote when it failed a check: kept to diagnose, never shown


def language_name(lang: str, text: str) -> str:
    if lang == "zh":  # MiLMMT names the script
        trad = sum(ch in _TRAD for ch in text)
        return "Chinese (Traditional)" if trad > sum(ch in _SIMP for ch in text) else "Chinese (Simplified)"
    return LANGUAGES[lang]


def prompt(lang: str, text: str) -> str:
    name = language_name(lang, text)
    rule = (
        " Keep every <xN>…</xN> tag pair and ⟦n⟧ token exactly as in the source."
        if PLACEHOLDER.search(text)
        else ""
    )
    return f"Translate this from {name} to English.{rule}\n{name}: {text}\nEnglish:"


def max_tokens(lang: str, text: str) -> int:
    return min(3072, (len(text) * 2 if lang in CJK else len(text) // 2) + 64)


class TranslationCache:
    """Checked translations by (model, prompt version, language, text): one SQLite file on local disk."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS segments"
            " (key TEXT PRIMARY KEY, text TEXT, ok INTEGER, reason TEXT, at REAL)"
        )
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(segments)")}
        if "rejected" not in cols:  # what a failed check saw (2026-10-24): for diagnosis only
            self._db.execute("ALTER TABLE segments ADD COLUMN rejected TEXT")
        self._lock = threading.Lock()

    @staticmethod
    def key(model: str, lang: str, text: str) -> str:
        return hashlib.sha256(f"{model}\x1f{PROMPT_VERSION}\x1f{lang}\x1f{text}".encode()).hexdigest()

    def get(self, key: str) -> Segment | None:
        with self._lock:
            row = self._db.execute(
                "SELECT text, ok, reason, rejected FROM segments WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else Segment(row[0], bool(row[1]), row[2], cached=True, rejected=row[3])

    def put(self, key: str, seg: Segment) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO segments (key, text, ok, reason, at, rejected)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, seg.text, int(seg.ok), seg.reason, time.time(), seg.rejected),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def release_torch_cache() -> None:
    """Give the GPU memory PyTorch keeps cached in this process back to the driver. The translator
    runs inside the API, whose search models' allocator held 8.6 GB after 13 hours where they need
    about 4.5 (2026-10-24): without this, the server would never find room to start."""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def free_vram_mb() -> int | None:
    """Free memory of the first GPU (nvidia-smi), or None when it cannot be read."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - the driver's own tool, fixed arguments
            [exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout  # fmt: skip
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _run_command(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode  # noqa: S603 - systemctl, fixed arguments


class LlamaServer:
    """The translation server's life: started on demand, stopped when idle or when the API stops."""

    def __init__(
        self,
        cfg: TranslationConfig,
        *,
        log_path: Path,
        run: Callable[[Sequence[str]], int] | None = None,
        vram: Callable[[], int | None] = free_vram_mb,
        release: Callable[[], None] = release_torch_cache,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.log_path = log_path
        self._run = run or _run_command
        self._vram = vram
        self._release = release
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "stopped"  # stopped | starting | ready | failed
        self._error: str | None = None
        self._last_used = clock()
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port}"

    @property
    def state(self) -> str:
        return self._state

    def argv(self) -> list[str]:
        c = self.cfg
        server = Path(c.server).expanduser()
        return [
            "systemd-run", "--user", "--scope", "-q", f"--unit={UNIT}",
            "-p", f"MemoryMax={c.memory_max}", "-p", "MemorySwapMax=0",
            "env", f"LD_LIBRARY_PATH={server.parent}", str(server),
            "-m", str(Path(c.model).expanduser()), "--host", "127.0.0.1", "--port", str(c.port),
            "-ngl", "999", "-fa", "on", "-np", str(c.slots), "-c", str(c.slots * c.context_per_slot),
            "-b", "2048", "-ub", "512", "--cache-ram", "0", "--no-webui",
            "-kvu", "-ctk", "q8_0", "-ctv", "q8_0", "--temp", "0", "--top-k", "1", "--repeat-penalty", "1.0",
        ]  # fmt: skip

    def missing(self) -> str | None:
        """Why the server cannot be started at all (no model or binary), or None."""
        if not Path(self.cfg.model).expanduser().is_file():
            return f"the translation model is not installed ({self.cfg.model})"
        if not Path(self.cfg.server).expanduser().is_file():
            return f"llama.cpp's llama-server is not installed ({self.cfg.server})"
        return None

    def ensure(self) -> None:
        """Ready, or :class:`TranslationUnavailable` (with when to ask again while it starts)."""
        with self._lock:
            self._last_used = self._clock()
            if self._state == "ready":
                if self._proc is not None and self._proc.poll() is None:
                    return
                # The server died after it was ready (a CUDA error, its memory ceiling): every request
                # failed as "did not answer" and kept it looking busy (2026-10-26 review #26).
                code = None if self._proc is None else self._proc.returncode
                log.warning("translation server exited (%s) while ready: starting it again", code)
                self._state, self._proc = "stopped", None
            if self._state == "starting":
                raise TranslationUnavailable("the translator is starting", retry_after=10)
            if (why := self.missing()) is not None:
                raise TranslationUnavailable(why)
            free = self._vram()
            if free is not None and free < self.cfg.min_free_vram_mb:
                # Not ours while stopped: a server an API that crashed left behind holds the card.
                # Stopped before anything else is asked to give memory back (2026-10-26 review #24).
                self._run(["systemctl", "--user", "stop", f"{UNIT}.scope"])
                free = self._vram()
            if free is not None and free < self.cfg.min_free_vram_mb:
                self._release()
                free = self._vram()
            if free is not None and free < self.cfg.min_free_vram_mb:
                raise TranslationUnavailable(
                    f"the GPU is busy ({free} MB free; translation needs {self.cfg.min_free_vram_mb} MB)",
                    retry_after=120,
                )
            self._state, self._error = "starting", None
        threading.Thread(target=self._start, name="translate-start", daemon=True).start()
        raise TranslationUnavailable("the translator is starting", retry_after=10)

    def _start(self) -> None:
        self._run(["systemctl", "--user", "stop", f"{UNIT}.scope"])  # one a crash or a restart left
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as out:
            self._proc = subprocess.Popen(self.argv(), stdout=out, stderr=subprocess.STDOUT)  # noqa: S603 - fixed argv
        deadline = self._clock() + 300
        while self._clock() < deadline:
            if self._proc.poll() is not None:
                return self._failed(f"llama-server exited ({self._proc.returncode}); see {self.log_path}")
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=5) as r:  # noqa: S310 - loopback
                    if r.status == 200:
                        with self._lock:
                            self._state = "ready"
                            self._last_used = self._clock()
                        log.info("translation server ready (%s)", self.cfg.model_label)
                        return None
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(2)
        self.stop()
        return self._failed(f"llama-server did not become ready in 300 s; see {self.log_path}")

    def _failed(self, why: str) -> None:
        log.warning("translation server: %s", why)
        with self._lock:
            self._state, self._error = "stopped", why

    def stop_if_idle(self) -> bool:
        with self._lock:
            idle = self._state == "ready" and self._clock() - self._last_used > self.cfg.idle_seconds
        if idle:
            log.info("translation server idle for %.0f s: stopped", self.cfg.idle_seconds)
            self.stop()
        return idle

    def stop(self) -> None:
        with self._lock:
            self._state = "stopped"
            proc, self._proc = self._proc, None
        self._run(["systemctl", "--user", "stop", f"{UNIT}.scope"])
        if proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=30)

    def touch(self) -> None:
        with self._lock:
            self._last_used = self._clock()

    @property
    def error(self) -> str | None:
        return self._error


Complete = Callable[[str, int], str]  # (prompt, max tokens) -> completion text


class Translator:
    """Translate segments into English: cached, checked, through the on-demand server."""

    def __init__(
        self,
        cfg: TranslationConfig,
        cache: TranslationCache,
        server: LlamaServer,
        *,
        complete: Complete | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache = cache
        self.server = server
        self._complete = complete or self._http_complete
        self._pool = ThreadPoolExecutor(cfg.slots, thread_name_prefix="translate")
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, name="translate-idle", daemon=True)
        self._reaper.start()

    @property
    def model(self) -> str:
        return self.cfg.model_label

    def status(self) -> dict[str, object]:
        missing = self.server.missing()
        return {
            "available": self.cfg.enabled and missing is None,
            "reason": None if self.cfg.enabled else "translation is switched off ([oracle.translation])",
            "model": self.model,
            "state": self.server.state,
            "error": missing or self.server.error,
            "languages": sorted(LANGUAGES),
            "prompt_version": PROMPT_VERSION,
        }

    def translate(self, lang: str, segments: Sequence[str]) -> list[Segment]:
        if not self.cfg.enabled:
            raise TranslationUnavailable("translation is switched off ([oracle.translation])")
        if lang not in LANGUAGES:
            raise ValueError(f"no translation from {lang!r}")
        keys = [TranslationCache.key(self.model, lang, s) for s in segments]
        out: list[Segment | None] = [
            self._recheck(lang, s, self.cache.get(k)) for s, k in zip(segments, keys, strict=True)
        ]
        todo = [i for i, seg in enumerate(out) if seg is None and segments[i].strip()]
        for i, text in enumerate(segments):
            if not text.strip():
                out[i] = Segment("", True)
        if todo:
            self.server.ensure()  # TranslationUnavailable while it starts or the GPU is busy

            def one(i: int) -> tuple[int, Segment]:
                return i, self._one(lang, segments[i])

            done = self._pool.map(one, todo)
            for i, seg in done:
                out[i] = seg
                if seg.reason != "the translator did not answer":
                    self.cache.put(keys[i], seg)
        self.server.touch()
        return [s if s is not None else Segment(None, False, "no translation came back") for s in out]

    @staticmethod
    def _recheck(lang: str, text: str, seg: Segment | None) -> Segment | None:
        """A cached answer judged again by today's checks (they change: "both" may be "two" since
        2026-10-24). The model's text is kept either way; an old failure without it is asked again."""
        if seg is None:
            return None
        raw = seg.text if seg.ok else seg.rejected
        if raw is None:
            return None
        verdict = check(text, raw, lang)
        return Segment(
            raw if verdict.ok else None,
            verdict.ok,
            verdict.reason,
            cached=True,
            rejected=None if verdict.ok else raw,
        )

    def _one(self, lang: str, text: str) -> Segment:
        try:
            raw = self._complete(prompt(lang, text), max_tokens(lang, text))
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            log.warning("translation request failed: %s", exc)
            return Segment(None, False, "the translator did not answer")
        target = raw.strip()
        for lead in ("English:", "Translation:"):
            if target.startswith(lead):
                target = target[len(lead) :].strip()
        verdict = check(text, target, lang)
        return Segment(
            target if verdict.ok else None,
            verdict.ok,
            verdict.reason,
            rejected=None if verdict.ok else target,
        )

    def _http_complete(self, text: str, limit: int) -> str:
        body = {
            "prompt": text,
            "max_tokens": limit,
            "temperature": 0,
            "stop": ["<end_of_turn>", "<eos>", "\nTranslate this from"],
        }
        req = urllib.request.Request(  # noqa: S310 - loopback
            f"{self.server.url}/v1/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.cfg.request_seconds) as r:  # noqa: S310 - loopback
            data = json.loads(r.read())
        return str(data["choices"][0]["text"])

    def _reap(self) -> None:
        while not self._stop.wait(30):
            self.server.stop_if_idle()

    def close(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.server.state != "stopped":
            self.server.stop()
        self.cache.close()


def build_translator(cfg: TranslationConfig, state_dir: Path) -> Translator | None:
    """The translator from ``[oracle.translation]``, or None when it is switched off."""
    if not cfg.enabled:
        return None
    root = state_dir / "translations"
    cache = TranslationCache(Path(cfg.cache).expanduser() if cfg.cache else root / "cache.sqlite")
    return Translator(cfg, cache, LlamaServer(cfg, log_path=root / "llama-server.log"))


__all__ = [
    "LANGUAGES",
    "PROMPT_VERSION",
    "LlamaServer",
    "Segment",
    "TranslationCache",
    "TranslationUnavailable",
    "Translator",
    "build_translator",
    "free_vram_mb",
]
