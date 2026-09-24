"""
Telegram -> Claude Code ko'prigi.

Telegram'da yozgan promtingiz kompyuteringizdagi PROJECTS_DIR ichidagi
tanlangan loyiha papkasida Claude Code (headless, `claude -p`) orqali bajariladi.
Har bir loyiha o'z sessiyasini eslab qoladi, shuning uchun suhbat davom etadi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

# ----------------------------------------------------------------- sozlamalar
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
PROJECTS_DIR = Path(os.getenv("PROJECTS_DIR", str(Path.home() / "projects"))).expanduser().resolve()
CLAUDE_CMD = os.getenv("CLAUDE_CMD") or shutil.which("claude") or "claude"
DEFAULT_MODE = os.getenv("PERMISSION_MODE", "acceptEdits")
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "")
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT_MIN", "30")) * 60
ALLOWED_TOOLS = os.getenv("CLAUDE_ALLOWED_TOOLS", "").split()
EXTRA_ARGS = os.getenv("CLAUDE_EXTRA_ARGS", "").split()

MODES = {
    "plan": ("plan", "📋 Faqat reja — hech narsani o'zgartirmaydi"),
    "edit": ("acceptEdits", "✏️ Fayllarni tahrirlaydi, terminal buyruqlari rad etiladi"),
    "auto": ("auto", "🧠 Xavfsiz amallarni o'zi tasdiqlaydi (hisobingizda mavjud bo'lsa)"),
    "full": ("bypassPermissions", "⚡ To'liq ruxsat — fayllar + terminal buyruqlari"),
}
MODE_LABEL = {v[0]: k for k, v in MODES.items()}

log = logging.getLogger("tgclaude")
_LOCK = None

# ------------------------------------------------------------------- holat
STATE_FILE = BASE / "state.json"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


STATE: dict = _load_state()


def ustate(uid: int) -> dict:
    s = STATE.setdefault(str(uid), {})
    s.setdefault("project", None)
    s.setdefault("sessions", {})
    s.setdefault("mode", DEFAULT_MODE)
    s.setdefault("model", DEFAULT_MODEL)
    return s


def save_state() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- loyihalar
PROJECT_DEPTH = int(os.getenv("PROJECT_DEPTH", "3"))
_SKIP_DIRS = {"node_modules", "venv", "env", "__pycache__", "target", "build", "dist", "out", "bin", "obj"}
_JUNK_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}


def _subdirs(p: Path) -> list[Path]:
    try:
        return [
            c for c in p.iterdir()
            if c.is_dir() and not c.name.startswith((".", "_")) and c.name.lower() not in _SKIP_DIRS
        ]
    except OSError:
        return []


def _is_group(p: Path) -> bool:
    """Guruh papka = ichida faqat papkalar bor (fayl yo'q, .git yo'q). Masalan: Projects\\Bots."""
    if (p / ".git").exists():
        return False
    try:
        has_files = any(c.is_file() and c.name.lower() not in _JUNK_FILES for c in p.iterdir())
    except OSError:
        return False
    return not has_files and bool(_subdirs(p))


def list_projects() -> list[str]:
    """PROJECTS_DIR ichidagi loyihalar. Guruh papkalar ichiga kiriladi: 'Bots/TelegramDavomat'."""
    if not PROJECTS_DIR.is_dir():
        return []
    found: list[str] = []

    def walk(p: Path, depth: int) -> None:
        for c in _subdirs(p):
            if depth < PROJECT_DEPTH and _is_group(c):
                walk(c, depth + 1)
            else:
                found.append(c.relative_to(PROJECTS_DIR).as_posix())

    walk(PROJECTS_DIR, 1)
    return sorted(found, key=str.lower)


def resolve_project(query: str) -> str | None:
    names = list_projects()
    q = query.strip().lower().replace("\\", "/")
    if not q:
        return None
    for key in (lambda n: n.lower(), lambda n: n.lower().rsplit("/", 1)[-1]):
        exact = [n for n in names if key(n) == q]
        if len(exact) == 1:
            return exact[0]
    matches = [n for n in names if n.lower().rsplit("/", 1)[-1].startswith(q)] or [n for n in names if q in n.lower()]
    return matches[0] if len(matches) == 1 else None


# ------------------------------------------------------ Claude sessiyalari
CLAUDE_HOME = Path(os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _sessions_dir(cwd: Path) -> Path | None:
    """Claude Code sessiyalarni ~/.claude/projects/<yo'l, belgilar '-' bilan> ichida saqlaydi."""
    root = CLAUDE_HOME / "projects"
    name = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    if (root / name).is_dir():
        return root / name
    try:  # Windows'da harf katta-kichikligi farq qilishi mumkin
        for d in root.iterdir():
            if d.name.lower() == name.lower():
                return d
    except OSError:
        pass
    return None


def _session_title(f: Path) -> str:
    title = first_prompt = ""
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"ai-title"' not in line and '"summary"' not in line and (first_prompt or '"type":"user"' not in line):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "ai-title" and d.get("aiTitle"):
                    title = d["aiTitle"]
                elif t == "summary" and d.get("summary") and not title:
                    title = d["summary"]
                elif t == "user" and not first_prompt and not d.get("isMeta"):
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = next((x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"), "")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        first_prompt = c
    except OSError:
        pass
    return _short(title or first_prompt or "(nomsiz)", 48)


def list_sessions(cwd: Path, limit: int = 10) -> list[tuple[str, str, float]]:
    """(session_id, sarlavha, mtime) — eng yangisi birinchi. Terminalda ochilganlari ham kiradi."""
    d = _sessions_dir(cwd)
    if not d:
        return []
    files = sorted(
        (f for f in d.glob("*.jsonl") if _UUID_RE.match(f.stem)),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:limit]
    return [(f.stem, _session_title(f), f.stat().st_mtime) for f in files]


def ago(ts: float) -> str:
    sec = max(0, time.time() - ts)
    if sec < 3600:
        return f"{int(sec // 60)} daq oldin"
    if sec < 86400:
        return f"{int(sec // 3600)} soat oldin"
    return f"{int(sec // 86400)} kun oldin"


# -------------------------------------------------------- Claude Code runner
@dataclass
class Job:
    project: str
    prompt: str
    started: float = field(default_factory=time.monotonic)
    proc: asyncio.subprocess.Process | None = None
    cancelled: bool = False


JOBS: dict[str, Job] = {}  # loyiha nomi -> ishlayotgan vazifa


def kill_tree(proc: asyncio.subprocess.Process | None) -> None:
    """Claude va u ishga tushirgan barcha bola jarayonlarni o'ldiradi."""
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


BRIDGE_PROMPT = os.getenv("CLAUDE_BRIDGE_PROMPT", "1") != "0"
OUTBOX = ".tg_outbox"


def bridge_prompt_file(project: str) -> Path:
    """Claude'ga Telegram ko'prigi haqida qo'shimcha ko'rsatma (fayl orqali — Windows qo'shtirnoq muammosisiz)."""
    r = RUNNERS.get(project)
    server = (f"The dev server for this project is ALREADY RUNNING at {r.url} (started by the bot with `{r.cmd}`); do not start another one."
              if r and r.alive and r.url else
              "No dev server is running. If you need one, ask the user to send /run in Telegram "
              "(the bot starts it in the background and keeps it alive); do not start long-running servers yourself.")
    text = f"""# Telegram bridge
The user talks to you through a Telegram bot, not a terminal. Keep replies concise; Markdown is rendered.
- To show the user an image or a file, save it into the `{OUTBOX}/` folder in the project root. After you finish, the bot sends every new file from there to the user in Telegram.
- To take a browser screenshot of a web page, run:
  `{Path(sys.executable).as_posix()} {(BASE / 'shot.py').as_posix()} <URL> {OUTBOX}/<name>.png` (optional flags: `--full` for the whole page, `--mobile` for a phone viewport, `--wait=5000` for slow pages)
- {server}
- Mention in your reply what each screenshot shows.
"""
    persona = BASE / "persona.md"  # agentning doimiy roli — shu faylni tahrirlab o'zgartiring
    if persona.is_file():
        text = persona.read_text(encoding="utf-8").strip() + "\n\n" + text
    f = BASE / "runs" / f"_bridge_{project.replace('/', '_')}.md"
    f.parent.mkdir(exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return f


async def run_claude(job: Job, cwd: Path, mode: str, model: str, session_id: str | None, on_event):
    """`claude -p` ni stream-json rejimida ishga tushiradi. (final, stderr, timed_out) qaytaradi."""
    args = [CLAUDE_CMD, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", mode]
    if BRIDGE_PROMPT:
        args += ["--append-system-prompt-file", str(bridge_prompt_file(job.project))]
    if session_id:
        args += ["--resume", session_id]
    if model:
        args += ["--model", model]
    args += EXTRA_ARGS
    if ALLOWED_TOOLS:
        args += ["--allowedTools", *ALLOWED_TOOLS]

    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=32 * 1024 * 1024,  # stream-json qatorlari katta bo'lishi mumkin
        **kw,
    )
    job.proc = proc

    # Promt stdin orqali beriladi — Windows'da qo'shtirnoq/maxsus belgi muammosi bo'lmaydi
    try:
        proc.stdin.write(job.prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

    stderr_task = asyncio.create_task(proc.stderr.read())
    final = None
    timed_out = False
    try:
        async with asyncio.timeout(TASK_TIMEOUT):
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    final = ev
                try:
                    await on_event(ev)
                except Exception:
                    log.exception("on_event xatosi")
    except TimeoutError:
        timed_out = True
        kill_tree(proc)
    await proc.wait()
    stderr = (await stderr_task).decode("utf-8", "replace")
    return final, stderr, timed_out


# ------------------------------------------------------- Telegram yordamchilar
def fmt_dur(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}s {m}d" if h else (f"{m}d {s}s" if m else f"{s}s")


def _short(s: str, n: int = 70) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, inp: dict | None) -> str:
    inp = inp or {}
    fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    fn = fp.replace("\\", "/").rsplit("/", 1)[-1] if fp else ""
    if name == "Bash":
        return "💻 " + _short(inp.get("command", ""))
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"✏️ {fn}"
    if name == "Write":
        return f"📝 {fn}"
    if name == "Read":
        return f"📖 {fn}"
    if name in ("Grep", "Glob"):
        return "🔎 " + _short(inp.get("pattern", ""), 50)
    if name == "TodoWrite":
        return "🗂 reja yangilandi"
    if name in ("WebSearch", "WebFetch"):
        return "🌐 " + _short(inp.get("query") or inp.get("url", ""), 60)
    if name in ("Task", "Agent"):
        return "🤖 " + _short(inp.get("description", "subagent"), 60)
    return f"🔧 {name}"


def split_text(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks or ["(bo'sh javob)"]


async def safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text[:4096])
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit xatosi: %s", e)
    except RetryAfter:
        pass
    except Exception as e:
        log.warning("edit xatosi: %s", e)


# ---------------------------------------------- Markdown -> Telegram HTML
def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    codes: list[str] = []

    def keep(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", keep, text)
    text = _esc(text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                  lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{_esc(codes[int(m.group(1))])}</code>", text)


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows
             if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", r)]
    if not cells:
        return ""
    n = max(len(r) for r in cells)
    cells = [r + [""] * (n - len(r)) for r in cells]
    w = [max(len(re.sub(r"[*`]", "", r[i])) for r in cells) for i in range(n)]
    lines = [" │ ".join(re.sub(r"[*`]", "", c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    if len(lines) > 1:
        lines.insert(1, "─┼─".join("─" * x for x in w))
    return "<pre>" + _esc("\n".join(lines)) + "</pre>"


def md_to_html(md: str) -> str:
    """Claude'ning Markdown javobini Telegram HTML formatiga o'giradi."""
    out: list[str] = []
    code: list[str] | None = None
    lang = ""
    table: list[str] = []

    def flush_table():
        if table:
            out.append(_table(table))
            table.clear()

    for line in md.split("\n"):
        fence = re.match(r"^\s*```\s*([\w+#.-]*)\s*$", line)
        if code is not None:
            if fence and not fence.group(1):
                cls = f' class="language-{lang}"' if lang else ""
                out.append(f"<pre><code{cls}>{_esc(chr(10).join(code))}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if fence:
            flush_table()
            code, lang = [], fence.group(1)
            continue
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            table.append(line)
            continue
        flush_table()
        if m := re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line):
            out.append(f"<b>{_inline(m.group(1))}</b>")
        elif re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            out.append("──────────")
        elif m := re.match(r"^\s*>\s?(.*)$", line):
            out.append(f"<blockquote>{_inline(m.group(1))}</blockquote>")
        elif m := re.match(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$", line):
            out.append(m.group(1) + ("✅ " if m.group(2).strip() else "⬜ ") + _inline(m.group(3)))
        elif m := re.match(r"^(\s*)[-*+]\s+(.*)$", line):
            out.append(m.group(1) + ("◦ " if len(m.group(1)) >= 2 else "• ") + _inline(m.group(2)))
        else:
            out.append(_inline(line))
    flush_table()
    if code is not None:
        out.append(f"<pre>{_esc(chr(10).join(code))}</pre>")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def split_markdown(text: str, limit: int = 3300) -> list[str]:
    """Kod bloklarini buzmasdan bo'laklarga ajratadi."""
    chunks, cur, size, fence = [], [], 0, None
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and cur:
            if fence is not None:
                cur.append("```")
            chunks.append("\n".join(cur))
            cur, size = ([f"```{fence}"] if fence is not None else []), 0
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        cur.append(line)
        size += len(line) + 1
        m = re.match(r"^\s*```\s*([\w+#.-]*)\s*$", line)
        if m:
            fence = None if fence is not None and not m.group(1) else (m.group(1) if fence is None else fence)
    if "\n".join(cur).strip():
        chunks.append("\n".join(cur))
    return chunks or ["(bo'sh javob)"]


async def send_md(bot, chat_id: int, md: str) -> None:
    for chunk in split_markdown(md):
        html = md_to_html(chunk)
        try:
            if not html or len(html) > 4096:
                raise BadRequest("too long")
            await bot.send_message(chat_id, html, parse_mode=ParseMode.HTML,
                                   link_preview_options=LinkPreviewOptions(is_disabled=True))
        except BadRequest as e:
            log.warning("HTML formatlash o'tmadi, oddiy matn yuborildi: %s", e)
            for part in split_text(chunk):
                await bot.send_message(chat_id, part)


async def send_long(bot, chat_id: int, text: str, project: str) -> None:
    if len(text) > 12000:
        await bot.send_document(
            chat_id,
            document=text.encode("utf-8"),
            filename=f"{project.replace('/', '_')}-javob.md",
            caption="Javob uzun — to'liq matn faylda",
        )
        text = text[:3500] + "\n\n… _(to'liq javob yuqoridagi faylda)_"
    await send_md(bot, chat_id, text)


class Progress:
    """Bitta Telegram xabarini jonli holat paneli sifatida yangilab turadi."""

    def __init__(self, msg, project: str):
        self.msg = msg
        self.project = project
        self.steps: list[str] = []
        self.started = time.monotonic()
        self.last = 0.0

    def render(self, head: str = "⏳ Ishlayapman") -> str:
        lines = [f"{head} · 📁 {self.project} · {fmt_dur(time.monotonic() - self.started)}"]
        if self.steps:
            extra = len(self.steps) - 12
            if extra > 0:
                lines.append(f"… +{extra} qadam")
            lines += self.steps[-12:]
        return "\n".join(lines)

    async def push(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < 2.5:
            return
        self.last = now
        await safe_edit(self.msg, self.render())


async def _ticker(prog: Progress) -> None:
    while True:
        await asyncio.sleep(10)
        await prog.push(force=True)


# ------------------------------------------------------------- asosiy ishchi
async def execute(update: Update, context: ContextTypes.DEFAULT_TYPE, project: str, prompt: str) -> None:
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    us = ustate(uid)
    bot = context.bot

    if project in JOBS:
        await bot.send_message(chat_id, f"⏳ «{project}» hozir band. Tugashini kuting yoki /stop")
        return
    cwd = PROJECTS_DIR / project
    if not cwd.is_dir():
        await bot.send_message(chat_id, f"❌ Papka topilmadi: {cwd}")
        return

    job = Job(project, prompt)
    JOBS[project] = job
    started_wall = time.time()
    log.info("JOB START [%s] mode=%s model=%s prompt=%r", project, us["mode"], us["model"] or "default", _short(prompt, 200))
    msg = await bot.send_message(chat_id, f"⏳ Boshladim · 📁 {project}")
    prog = Progress(msg, project)
    ticker = asyncio.create_task(_ticker(prog))

    async def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            us["sessions"][project] = ev["session_id"]
            save_state()
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    prog.steps.append(describe_tool(c.get("name", ""), c.get("input")))
            await prog.push()

    try:
        sid = us["sessions"].get(project)
        final, stderr, timed_out = await run_claude(job, cwd, us["mode"], us["model"], sid, on_event)
        # Eski sessiya o'chib ketgan bo'lsa — yangisini ochib qayta urinish
        if final is None and sid and not job.cancelled and "no conversation found" in stderr.lower():
            us["sessions"].pop(project, None)
            save_state()
            prog.steps.append("♻️ eski sessiya topilmadi — yangisi ochildi")
            final, stderr, timed_out = await run_claude(job, cwd, us["mode"], us["model"], None, on_event)
    except FileNotFoundError:
        await safe_edit(msg, f"❌ Claude Code topilmadi ({CLAUDE_CMD}). O'rnating yoki .env da CLAUDE_CMD ni ko'rsating.")
        return
    except Exception as e:
        log.exception("execute xatosi")
        await safe_edit(msg, f"❌ Xato: {e}")
        return
    finally:
        ticker.cancel()
        JOBS.pop(project, None)

    elapsed = time.monotonic() - job.started
    if job.cancelled:
        log.info("JOB STOP [%s] foydalanuvchi to'xtatdi (%.0fs)", project, elapsed)
        await safe_edit(msg, prog.render("⛔ To'xtatildi"))
        return
    if timed_out:
        log.warning("JOB TIMEOUT [%s] %.0fs", project, elapsed)
        await safe_edit(msg, prog.render(f"⌛ {TASK_TIMEOUT // 60} daqiqa limiti tugadi"))
        return
    if final is None:
        log.error("JOB FAIL [%s] claude natija qaytarmadi. stderr:\n%s", project, stderr.strip()[-3000:])
        await safe_edit(msg, prog.render("❌ Xato"))
        tail = stderr.strip()[-1500:] or "(stderr bo'sh)"
        await bot.send_message(chat_id, f"Claude Code xatosi:\n{tail}")
        return

    if final.get("session_id"):
        us["sessions"][project] = final["session_id"]
        save_state()
    is_err = final.get("is_error") or final.get("subtype") != "success"
    head = "⚠️ Xato bilan tugadi" if is_err else "✅ Tayyor"
    if is_err:
        log.error("JOB ERROR [%s] subtype=%s result=%r stderr=%r", project, final.get("subtype"),
                  _short(final.get("result") or "", 1000), stderr.strip()[-2000:])
    else:
        log.info("JOB OK [%s] %.0fs, %s tur, %d qadam", project, elapsed, final.get("num_turns", "?"), len(prog.steps))
    if final.get("permission_denials"):
        log.warning("JOB DENIED [%s] %s", project, [d.get("tool_name") for d in final["permission_denials"]])
    await safe_edit(msg, prog.render(f"{head} · {final.get('num_turns', '?')} tur"))

    text = final.get("result") or f"(matnli javob yo'q — {final.get('subtype')})"
    await send_long(bot, chat_id, text, project)
    await send_outbox(bot, chat_id, cwd, started_wall)

    denials = final.get("permission_denials") or []
    if denials:
        lines = [describe_tool(d.get("tool_name", ""), d.get("tool_input")) for d in denials[:10]]
        await bot.send_message(
            chat_id,
            "⚠️ Ruxsat berilmagan amallar:\n" + "\n".join(lines)
            + "\n\nRuxsat berish uchun: /mode full, so'ng «davom et» deb yozing.",
        )


# ------------------------------------------------ fayl yuborish (outbox)
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


async def send_file(bot, chat_id: int, path: Path, caption: str = "") -> None:
    caption = caption[:1000]
    try:
        if path.suffix.lower() in IMAGE_EXT and path.stat().st_size < 10 * 1024 * 1024:
            try:
                with path.open("rb") as fh:
                    await bot.send_photo(chat_id, fh, caption=caption or None)
                return
            except BadRequest:
                pass  # juda uzun/katta rasm — fayl sifatida yuboramiz
        with path.open("rb") as fh:
            await bot.send_document(chat_id, fh, filename=path.name, caption=caption or None)
    except Exception as e:
        log.warning("Fayl yuborilmadi %s: %s", path, e)
        await bot.send_message(chat_id, f"⚠️ {path.name} yuborilmadi: {e}")


async def send_outbox(bot, chat_id: int, cwd: Path, since: float) -> None:
    box = cwd / OUTBOX
    if not box.is_dir():
        return
    files = sorted((f for f in box.iterdir() if f.is_file() and f.stat().st_mtime >= since - 1),
                   key=lambda f: f.stat().st_mtime)
    for f in files[:10]:
        await send_file(bot, chat_id, f, f.name)
    if files:
        log.info("OUTBOX %s: %d fayl yuborildi", cwd.name, min(len(files), 10))


# ------------------------------------------ dev server (/run) va skrinshot (/shot)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|[\w.-]+\.local)(?::\d+)?[^\s'\"<>)]*")


@dataclass
class Runner:
    project: str
    cmd: str
    proc: asyncio.subprocess.Process
    lines: list = field(default_factory=list)
    url: str = ""
    started: float = field(default_factory=time.monotonic)

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


RUNNERS: dict[str, Runner] = {}


def detect_run_cmd(cwd: Path) -> str | None:
    pkg = cwd / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except Exception:
            scripts = {}
        for name in ("dev", "start", "serve"):
            if name in scripts:
                return "npm start" if name == "start" else f"npm run {name}"
        if (cwd / "angular.json").is_file():
            return "npx ng serve"
    if (cwd / "pom.xml").is_file():
        return ("mvnw.cmd" if os.name == "nt" and (cwd / "mvnw.cmd").is_file() else "mvn") + " spring-boot:run"
    if (cwd / "build.gradle").is_file() or (cwd / "build.gradle.kts").is_file():
        return ("gradlew.bat" if os.name == "nt" else "./gradlew") + " bootRun"
    if (cwd / "manage.py").is_file():
        return "python manage.py runserver"
    for f in ("main.py", "app.py"):
        if (cwd / f).is_file():
            return f"python {f}"
    return None


async def _pump(r: Runner) -> None:
    logf = BASE / "runs" / (r.project.replace("/", "_") + ".log")
    logf.parent.mkdir(exist_ok=True)
    with logf.open("w", encoding="utf-8") as fh:
        async for raw in r.proc.stdout:
            line = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
            fh.write(line + "\n")
            fh.flush()
            r.lines.append(line)
            del r.lines[:-300]
            if not r.url and (m := _URL_RE.search(line)):
                r.url = m.group(0).rstrip(".,;/").replace("0.0.0.0", "localhost")
    await r.proc.wait()
    log.info("RUN EXIT [%s] kod=%s", r.project, r.proc.returncode)


async def start_runner(project: str, cmd: str) -> Runner:
    cwd = PROJECTS_DIR / project
    env = {**os.environ, "BROWSER": "none", "FORCE_COLOR": "0", "CI": "false"}
    kw: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=4 * 1024 * 1024, **kw,
    )
    r = Runner(project, cmd, proc)
    RUNNERS[project] = r
    asyncio.get_running_loop().create_task(_pump(r))
    log.info("RUN START [%s] %s (pid %s)", project, cmd, proc.pid)
    return r


def _tail(r: Runner, n: int = 25) -> str:
    return "\n".join(r.lines[-n:]) or "(chiqish yo'q)"


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    p = us["project"]
    if not p:
        await update.message.reply_text("Avval loyiha tanlang: /projects")
        return
    old = RUNNERS.get(p)
    if old and old.alive:
        await update.message.reply_text(
            f"🟢 Allaqachon ishlayapti: {old.url or '(URL hali aniqlanmadi)'}\n/shot — skrinshot · /runlog — log · /stoprun — to'xtatish")
        return
    cmd = " ".join(context.args) or detect_run_cmd(PROJECTS_DIR / p)
    if not cmd:
        await update.message.reply_text("Qanday ishga tushirishni aniqlay olmadim. Buyruqni yozing, masalan:\n/run npm run dev")
        return
    msg = await update.message.reply_text(f"🚀 «{p}»: {cmd}\nIshga tushyapti…")
    r = await start_runner(p, cmd)
    for _ in range(180):  # 3 daqiqagacha URL kutamiz
        if r.url or not r.alive:
            break
        await asyncio.sleep(1)
    if not r.alive:
        await safe_edit(msg, f"❌ «{p}» ishga tushmadi (kod {r.proc.returncode}).")
        await send_md(update.get_bot(), update.effective_chat.id, f"```\n{_tail(r, 40)}\n```")
        return
    if r.url:
        await asyncio.sleep(3)  # birinchi kompilyatsiya tugashi uchun
        await safe_edit(msg, f"🟢 «{p}» ishlayapti: {r.url}\n\n/shot — skrinshot\n/shot /yo'l full mobile\n/runlog · /stoprun")
    else:
        await safe_edit(msg, f"🟡 «{p}» ishlayapti, lekin URL topilmadi.\n/runlog — chiqishni ko'rish · /shot http://localhost:PORT")


async def cmd_stoprun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    p = us["project"]
    r = RUNNERS.get(p) if p else None
    if (not r or not r.alive) and len([x for x in RUNNERS.values() if x.alive]) == 1:
        r = next(x for x in RUNNERS.values() if x.alive)
    if not r or not r.alive:
        await update.message.reply_text("Ishlayotgan server yo'q.")
        return
    kill_tree(r.proc)
    await update.message.reply_text(f"⏹ «{r.project}» serveri to'xtatildi.")


async def cmd_runlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    r = RUNNERS.get(us["project"] or "")
    if not r:
        await update.message.reply_text("Bu loyihada /run qilinmagan.")
        return
    state = "🟢 ishlayapti" if r.alive else f"🔴 to'xtagan (kod {r.proc.returncode})"
    await send_md(update.get_bot(), update.effective_chat.id,
                  f"**{r.project}** · {state} · {r.url or 'URL yo‘q'}\n```\n{_tail(r, 40)}\n```")


async def cmd_shot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    p = us["project"]
    args = [a for a in context.args]
    full = any(a.lower() == "full" for a in args)
    mobile = any(a.lower() in ("mobile", "mobil", "m") for a in args)
    rest = [a for a in args if a.lower() not in ("full", "mobile", "mobil", "m")]
    target = rest[0] if rest else ""
    r = RUNNERS.get(p or "")
    base = r.url.rstrip("/") if r and r.alive and r.url else ""
    if target.startswith(("http://", "https://")):
        url = target
    elif target.isdigit():
        url = f"http://localhost:{target}"
    elif base:
        url = base + ("/" + target.lstrip("/") if target else "")
    else:
        await update.message.reply_text("Server ishlamayapti. Avval /run qiling yoki URL bering:\n/shot http://localhost:4200")
        return
    msg = await update.message.reply_text(f"📸 {url} …")
    out = BASE / "runs" / f"shot_{int(time.time())}.png"
    try:
        from shot import take
        await take(url, out, full=full, mobile=mobile)
    except Exception as e:
        log.warning("SHOT xato %s: %s", url, e)
        await safe_edit(msg, f"❌ Skrinshot olinmadi: {_short(str(e), 300)}")
        return
    await send_file(update.get_bot(), update.effective_chat.id, out,
                    url + (" · full" if full else "") + (" · mobile" if mobile else ""))
    try:
        await msg.delete()
        out.unlink()
    except Exception:
        pass


# -------------------------------------------------------------- buyruqlar
HELP = (
    "🤖 Claude Code — Telegram orqali\n\n"
    "1) /projects — loyihani tanlang\n"
    "2) Shunchaki promt yozing — Claude shu loyiha papkasida ishlaydi\n\n"
    "Buyruqlar:\n"
    "/projects — loyihalar ro'yxati\n"
    "/use <nom> — loyihani nomi bilan tanlash\n"
    "/sessions — shu loyihaning sessiyalari (terminaldagilar ham), almashtirish\n"
    "/new — shu loyihada yangi suhbat boshlash\n"
    "/stop — ishlayotgan vazifani to'xtatish\n"
    "/status — joriy holat\n"
    "/mode — ruxsat rejimi (plan / edit / auto / full)\n"
    "/model — model (sonnet, opus, default)\n"
    "/diff — git o'zgarishlar\n\n"
    "🌐 Ishga tushirish va skrinshot:\n"
    "/run — loyihani fonda ishga tushirish (npm/mvn/gradle… o'zi aniqlaydi; yoki /run <buyruq>)\n"
    "/shot — brauzer skrinshoti (/shot /editor · /shot full · /shot mobile · /shot http://…)\n"
    "/runlog — server chiqishi · /stoprun — to'xtatish\n"
    "💡 Claude'dan ham so'rashingiz mumkin: «bosh sahifani ochib skrinshot yubor».\n\n"
    "💡 «@loyiha promt» — boshqa loyihaga almashtirmasdan bir martalik buyruq.\n"
    "📎 Rasm yoki fayl yuborsangiz, loyihaning .tg_uploads papkasiga saqlanib, Claude'ga beriladi."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    names = list_projects()
    if not names:
        await update.message.reply_text(f"Papka bo'sh yoki topilmadi: {PROJECTS_DIR}")
        return
    context.user_data["plist"] = names
    kb, row = [], []
    for i, n in enumerate(names):
        mark = "✅ " if n == us["project"] else ""
        row.append(InlineKeyboardButton(mark + n, callback_data=f"p:{i}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    await update.message.reply_text("📂 Loyihani tanlang:", reply_markup=InlineKeyboardMarkup(kb))


def _selected_text(us: dict, name: str) -> str:
    extra = "\n🔁 Oldingi suhbat davom etadi (/new — yangidan)." if name in us["sessions"] else ""
    return f"📁 Tanlandi: {name}{extra}\nEndi promt yozing."


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    name = resolve_project(" ".join(context.args))
    if not name:
        await update.message.reply_text("Topilmadi yoki bir nechta mos keldi. /projects dan tanlang.")
        return
    us["project"] = name
    save_state()
    await update.message.reply_text(_selected_text(us, name))


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    p = us["project"]
    if not p:
        await update.message.reply_text("Avval loyiha tanlang: /projects")
        return
    items = list_sessions(PROJECTS_DIR / p)
    cur = us["sessions"].get(p)
    context.user_data["slist"] = (p, [sid for sid, _, _ in items])
    kb = [[InlineKeyboardButton("🆕 Yangi sessiya", callback_data="s:new")]]
    for i, (sid, title, mt) in enumerate(items):
        mark = "✅ " if sid == cur else ""
        kb.append([InlineKeyboardButton(f"{mark}{title} · {ago(mt)}", callback_data=f"s:{i}")])
    head = f"💬 «{p}» sessiyalari" + ("" if items else " — hali yo'q")
    if cur and cur not in {sid for sid, _, _ in items}:
        head += f"\nJoriy: {cur[:8]}…"
    await update.message.reply_text(head + "\nDavom ettirmoqchi bo'lganini tanlang:", reply_markup=InlineKeyboardMarkup(kb))


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    if not us["project"]:
        await update.message.reply_text("Avval loyiha tanlang: /projects")
        return
    us["sessions"].pop(us["project"], None)
    save_state()
    await update.message.reply_text(f"🆕 «{us['project']}» uchun yangi suhbat. Promt yozing.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    target = resolve_project(" ".join(context.args)) if context.args else us["project"]
    if target not in JOBS and len(JOBS) == 1:
        target = next(iter(JOBS))
    job = JOBS.get(target)
    if not job:
        await update.message.reply_text("Hozir hech narsa ishlamayapti.")
        return
    job.cancelled = True
    kill_tree(job.proc)
    await update.message.reply_text(f"⛔ «{target}» to'xtatilmoqda…")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    p = us["project"]
    sid = us["sessions"].get(p) if p else None
    lines = [
        f"📁 Loyiha: {p or '— (tanlanmagan)'}",
        f"🔐 Rejim: {MODE_LABEL.get(us['mode'], us['mode'])}",
        f"🧠 Model: {us['model'] or 'default'}",
        f"💬 Sessiya: {sid[:8] + '…' if sid else 'yangi'}",
    ]
    if JOBS:
        lines.append("\n⏳ Ishlayapti:")
        lines += [f"• {j.project} — {fmt_dur(time.monotonic() - j.started)}" for j in JOBS.values()]
    await update.message.reply_text("\n".join(lines))


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    if context.args and context.args[0].lower() in MODES:
        us["mode"] = MODES[context.args[0].lower()][0]
        save_state()
        await update.message.reply_text(f"🔐 Rejim: {context.args[0].lower()}")
        return
    kb = [
        [InlineKeyboardButton(("✅ " if us["mode"] == v[0] else "") + f"{k} — {v[1]}", callback_data=f"m:{k}")]
        for k, v in MODES.items()
    ]
    await update.message.reply_text("🔐 Ruxsat rejimini tanlang:", reply_markup=InlineKeyboardMarkup(kb))


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    if not context.args:
        await update.message.reply_text(
            f"🧠 Hozirgi model: {us['model'] or 'default'}\nMisol: /model sonnet · /model opus · /model default"
        )
        return
    m = context.args[0].strip()
    us["model"] = "" if m.lower() == "default" else m
    save_state()
    await update.message.reply_text(f"🧠 Model: {us['model'] or 'default'}")


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    if not us["project"]:
        await update.message.reply_text("Avval loyiha tanlang: /projects")
        return
    cwd = PROJECTS_DIR / us["project"]
    out = []
    for args in (["git", "status", "--short", "--branch"], ["git", "diff", "--stat"]):
        try:
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            p = await asyncio.create_subprocess_exec(
                *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kw
            )
            data, _ = await p.communicate()
            out.append(data.decode("utf-8", "replace").strip())
        except FileNotFoundError:
            out.append("git topilmadi")
            break
    text = "\n\n".join(x for x in out if x) or "O'zgarish yo'q."
    for chunk in split_text(text):
        await update.message.reply_text(chunk)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id not in ALLOWED_IDS:
        await q.answer("⛔")
        return
    us = ustate(q.from_user.id)
    data = q.data or ""
    if data.startswith("p:"):
        names = context.user_data.get("plist") or list_projects()
        idx = int(data[2:])
        if idx >= len(names) or not (PROJECTS_DIR / names[idx]).is_dir():
            await q.answer("Ro'yxat eskirgan — /projects ni qayta bosing")
            return
        us["project"] = names[idx]
        save_state()
        await q.answer()
        await q.edit_message_text(_selected_text(us, names[idx]))
    elif data.startswith("s:"):
        proj, sids = context.user_data.get("slist") or (None, [])
        if not proj or proj != us["project"]:
            await q.answer("Ro'yxat eskirgan — /sessions ni qayta bosing")
            return
        if data == "s:new":
            us["sessions"].pop(proj, None)
            save_state()
            await q.answer()
            await q.edit_message_text(f"🆕 «{proj}»: keyingi promt yangi sessiyada boshlanadi.")
            return
        idx = int(data[2:])
        if idx >= len(sids):
            await q.answer("Ro'yxat eskirgan — /sessions ni qayta bosing")
            return
        us["sessions"][proj] = sids[idx]
        save_state()
        await q.answer()
        title = next((t for sid, t, _ in list_sessions(PROJECTS_DIR / proj) if sid == sids[idx]), sids[idx][:8])
        await q.edit_message_text(f"🔁 «{proj}»: «{title}» sessiyasi davom etadi. Promt yozing.")
    elif data.startswith("m:") and data[2:] in MODES:
        us["mode"] = MODES[data[2:]][0]
        save_state()
        await q.answer()
        await q.edit_message_text(f"🔐 Rejim: {data[2:]} — {MODES[data[2:]][1]}")


# -------------------------------------------------------- xabarlar
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    text = update.message.text.strip()
    project = us["project"]
    if text.startswith("@"):
        first, _, rest = text[1:].partition(" ")
        p = resolve_project(first)
        if p and rest.strip():
            project, text = p, rest.strip()
    if not project:
        await update.message.reply_text("Avval loyiha tanlang: /projects")
        return
    context.application.create_task(execute(update, context, project, text), update=update)


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    us = ustate(update.effective_user.id)
    msg = update.message
    project = us["project"]
    if not project:
        await msg.reply_text("Avval loyiha tanlang: /projects")
        return
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        name = f"photo_{msg.message_id}.jpg"
    else:
        tg_file = await msg.document.get_file()
        name = Path(msg.document.file_name or f"file_{msg.message_id}").name
    updir = PROJECTS_DIR / project / ".tg_uploads"
    updir.mkdir(exist_ok=True)
    await tg_file.download_to_drive(updir / name)
    prompt = (msg.caption or "Shu faylni ko'rib chiq.") + f"\n\n[Foydalanuvchi Telegram orqali fayl yubordi: .tg_uploads/{name}]"
    context.application.create_task(execute(update, context, project, prompt), update=update)


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    log.warning("Ruxsatsiz urinish: id=%s username=%s", u.id if u else "?", u.username if u else "?")
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Ruxsat yo'q.")


async def on_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ALLOWED_USER_IDS bo'sh bo'lsa — faqat ID'ingizni aytadi."""
    uid = update.effective_user.id
    log.warning("SETUP: Telegram ID = %s", uid)
    await update.effective_message.reply_text(
        f"Sizning Telegram ID: {uid}\n\n.env fayliga ALLOWED_USER_IDS={uid} deb yozing va botni qayta ishga tushiring."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        log.error("CONFLICT: shu token bilan boshqa bot nusxasi ham ishlayapti — bittasini yoping.")
    elif isinstance(err, NetworkError):
        log.warning("Tarmoq xatosi (qayta urinadi): %s", err)
    else:
        log.error("Kutilmagan xato", exc_info=err)


# ---------------------------------------------------------------- ishga tushirish
async def post_init(app: Application) -> None:
    if not ALLOWED_IDS:
        return
    await app.bot.set_my_commands([
        BotCommand("projects", "Loyihalar ro'yxati"),
        BotCommand("status", "Joriy holat"),
        BotCommand("sessions", "Sessiyalar ro'yxati"),
        BotCommand("new", "Yangi suhbat"),
        BotCommand("stop", "To'xtatish"),
        BotCommand("mode", "Ruxsat rejimi"),
        BotCommand("model", "Model tanlash"),
        BotCommand("diff", "Git o'zgarishlar"),
        BotCommand("run", "Loyihani ishga tushirish"),
        BotCommand("shot", "Brauzer skrinshoti"),
        BotCommand("runlog", "Server logi"),
        BotCommand("stoprun", "Serverni to'xtatish"),
        BotCommand("help", "Yordam"),
    ])
    for uid in ALLOWED_IDS:
        try:
            await app.bot.send_message(uid, f"🟢 Bot ishga tushdi · {len(list_projects())} ta loyiha")
        except Exception:
            pass


async def post_shutdown(app: Application) -> None:
    for job in list(JOBS.values()):
        kill_tree(job.proc)
    for r in list(RUNNERS.values()):
        kill_tree(r.proc)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(BASE / "bot.log", encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not TOKEN or TOKEN.startswith("123456789:"):
        log.error(".env faylida TELEGRAM_BOT_TOKEN to'ldirilmagan (%s)", BASE / ".env")
        sys.exit(1)
    if not PROJECTS_DIR.is_dir():
        log.error("PROJECTS_DIR topilmadi: %s  ->  .env dagi PROJECTS_DIR ni tekshiring", PROJECTS_DIR)
        sys.exit(1)
    # Bitta nusxa qoidasi: ikkinchi bot ishga tushsa, Telegram "Conflict" beradi
    global _LOCK
    import socket
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", int(os.getenv("LOCK_PORT", "47821"))))
    except OSError:
        log.error("Bot allaqachon ishlayapti (boshqa oynada yoki yashirin). Avval uni yoping.")
        sys.exit(1)
    log.info("Loyihalar: %s | claude: %s", PROJECTS_DIR, CLAUDE_CMD)

    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_error_handler(on_error)

    if not ALLOWED_IDS:
        log.warning("ALLOWED_USER_IDS bo'sh — SETUP rejimi: botga yozing, u ID'ingizni aytadi.")
        app.add_handler(MessageHandler(filters.ALL, on_setup))
    else:
        me = filters.User(user_id=ALLOWED_IDS)
        for name, fn in [
            ("start", cmd_start), ("help", cmd_start), ("projects", cmd_projects), ("use", cmd_use),
            ("new", cmd_new), ("sessions", cmd_sessions), ("stop", cmd_stop), ("status", cmd_status), ("mode", cmd_mode),
            ("model", cmd_model), ("diff", cmd_diff), ("run", cmd_run), ("shot", cmd_shot),
            ("runlog", cmd_runlog), ("stoprun", cmd_stoprun),
        ]:
            app.add_handler(CommandHandler(name, fn, filters=me))
        app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
        app.add_handler(MessageHandler(me & (filters.PHOTO | filters.Document.ALL), on_file))
        app.add_handler(CallbackQueryHandler(on_callback))
        app.add_handler(MessageHandler(~me, on_stranger))

    # Python 3.14+: asyncio.get_event_loop() endi avtomatik loop yaratmaydi — o'zimiz beramiz
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        log.exception("Bot to'xtadi")
        sys.exit(1)


if __name__ == "__main__":
    main()
