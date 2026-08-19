"""PostLock GUI — a small desktop app wrapping the PostLock flow.

Pick a finished video, click "Deliver to TikTok inbox": the app finds the
project's narration script (or uses the bundled example), generates captions,
connects your TikTok account once via the official OAuth flow, and delivers
the video to your TikTok inbox as a draft for review. Nothing is published
automatically — you tap Post in the TikTok app.

Run:  python postlock_gui.py
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

from tkinter import Tk, ttk, filedialog, scrolledtext

from postlock import (
    ENV_PATH,
    TIKTOK_TOKEN_URL,
    generate_captions,
    tiktok_draft,
)
import postlock as _postlock

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
REDIRECT_PORT = 8766
# Must match the redirect URI registered in the TikTok developer console.
# TikTok redirects to this HTTPS page, which forwards the code to the local
# helper server below (localhost is a secure context, so the handoff works).
REDIRECT_URI = "https://kilolime.github.io/PostLock/callback.html"
SCOPES = "user.info.basic,video.upload"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"

BG = "#0f1115"
CARD = "#161a26"
FG = "#e8eaf0"
MUTED = "#8a93a6"
ACCENT = "#4f8cff"
GOOD = "#4fd08a"
BAD = "#ff6b6b"


def _load_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    return result


def _save_env_key(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() \
        if ENV_PATH.exists() else []
    kept = [line for line in lines if not line.startswith(key + "=")]
    kept.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.environ[key] = value


def _make_pkce() -> tuple[str, str]:
    """TikTok requires the challenge as HEX(SHA256(verifier)) (their docs)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    codes: list[str] = []
    errors: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 -- HTTP API
        parsed = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        code = parsed.get("code", [""])[0]
        error = parsed.get("error", [""])[0]
        state = parsed.get("state", [""])[0]
        if state and state != "postlock":
            self.errors.append(f"unexpected state: {state}")
            body = b"<h2>Authorization failed: state mismatch.</h2>"
            self.send_response(400)
        elif code:
            self.codes.append(code)
            body = b"<h2>PostLock connected!</h2><p>Return to the app.</p>"
            self.send_response(200)
        elif error:
            self.errors.append(error)
            body = f"<h2>Authorization failed: {error}</h2>".encode()
            self.send_response(400)
        else:
            body = b"<h2>No code received.</h2>"
            self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D401
        pass


def authorize(log) -> bool:
    """Run the one-time OAuth consent in the browser; persist tokens."""
    env = _load_env()
    client_key = env.get("TIKTOK_CLIENT_KEY", "")
    client_secret = env.get("TIKTOK_CLIENT_SECRET", "")
    if not client_key or not client_secret:
        log("Missing TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env")
        return False

    _CallbackHandler.codes = []
    _CallbackHandler.errors = []
    try:
        server = HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    except OSError:
        log(f"Could not open the local callback port {REDIRECT_PORT} — "
            "close any other PostLock window and try again.")
        return False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    verifier, challenge = _make_pkce()
    params = urlencode({
        "client_key": client_key,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": "postlock",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    auth_page = f"{AUTH_URL}?{params}"
    log("Opening the TikTok authorize page in your browser…")
    webbrowser.open(auth_page)

    deadline = time.time() + 300
    while time.time() < deadline:
        if _CallbackHandler.codes or _CallbackHandler.errors:
            break
        time.sleep(0.2)
    server.shutdown()
    server.server_close()
    if _CallbackHandler.errors:
        log(f"Authorization failed: {_CallbackHandler.errors[0]}")
        return False
    if not _CallbackHandler.codes:
        log("Timed out waiting for authorization.")
        return False

    payload = urlencode({
        "client_key": client_key,
        "client_secret": client_secret,
        "code": _CallbackHandler.codes[0],
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    req = Request(TIKTOK_TOKEN_URL, data=payload, method="POST")
    with urlopen(req, timeout=60) as resp:  # noqa: S310 -- https endpoint
        tokens = json.loads(resp.read().decode("utf-8"))
    data = tokens.get("data")
    if not isinstance(data, dict) or not data.get("access_token"):
        data = tokens
    access = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    if not access:
        log(f"Token exchange failed: {tokens.get('error', 'unknown')}")
        return False
    _save_env_key("TIKTOK_ACCESS_TOKEN", access)
    _save_env_key("TIKTOK_REFRESH_TOKEN", refresh)
    log("Connected. Tokens saved to .env (local only).")
    return True


def find_script(video_path: str) -> Path:
    """Locate the narration script for a video.

    Walks up from the video looking for the project's writing-lab script
    (Scroll Lock layout: <project>/assets/writing_lab/FINAL_SCRIPT*.txt),
    then falls back to the bundled example in assets/.
    """
    video = Path(video_path)
    for parent in [video] + list(video.parents[:6]):
        for name in ("FINAL_SCRIPT.txt", "FINAL_SCRIPT_TTS.txt"):
            candidate = parent / "assets" / "writing_lab" / name
            if candidate.exists():
                return candidate
    bundled = ASSETS / "example_script.txt"
    return bundled if bundled.exists() else Path("")


class PostLockApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        root.title("PostLock")
        root.geometry("780x600")
        root.configure(bg=BG)
        root.minsize(700, 520)

        self.video_path = ""
        self._busy = False

        header = ttk.Frame(root, style="Card.TFrame")
        header.pack(fill="x", padx=14, pady=(14, 4))
        ttk.Label(header, text="PostLock", style="Title.TLabel").pack(
            side="left", padx=10, pady=8)
        ttk.Label(header, text="deliver finished videos to your TikTok inbox "
                               "as drafts for review", style="Muted.TLabel"
                  ).pack(side="left", padx=6)

        body = ttk.Frame(root, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=14, pady=6)

        row1 = ttk.Frame(body, style="Card.TFrame")
        row1.pack(fill="x", padx=10, pady=(12, 4))
        self.video_label = ttk.Label(row1, text="Video: none selected",
                                     style="Muted.TLabel")
        self.video_label.pack(side="left", padx=4)
        ttk.Button(row1, text="Select video…",
                   command=self._pick_video).pack(side="right", padx=4)

        self.deliver_btn = ttk.Button(
            body, text="Deliver to TikTok inbox",
            command=self._deliver)
        self.deliver_btn.pack(fill="x", padx=10, pady=(10, 6))
        self.connect_btn = ttk.Button(
            body, text="Connect TikTok (re-authorize)",
            command=self._connect)
        self.connect_btn.pack(fill="x", padx=10, pady=(0, 6))

        self.log = scrolledtext.ScrolledText(
            body, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
            font=("Consolas", 10), height=14)
        self.log.pack(fill="both", expand=True, padx=10, pady=(8, 12))
        self.log.configure(state="disabled")

        self._ui_queue: queue.Queue = queue.Queue()
        root.after(50, self._drain_ui)

        self._log("PostLock ready. Pick a video and press "
                  "\"Deliver to TikTok inbox\".")
        self._log("The app finds the script, generates captions, connects "
                  "your account once, and uploads.")
        self._log("Your video lands in the TikTok app as a draft — you tap "
                  "Post to publish.")

    # -- helpers ---------------------------------------------------------

    def _log(self, msg: str, color: str = FG) -> None:
        self._ui_queue.put(("log", msg, color))

    def _set_busy(self, busy: bool) -> None:
        self._ui_queue.put(("busy", busy))

    def _drain_ui(self) -> None:
        """Apply queued UI updates on the main thread (tkinter is not
        thread-safe; workers only push to the queue)."""
        try:
            while True:
                kind, *rest = self._ui_queue.get_nowait()
                if kind == "log":
                    msg, color = rest
                    self.log.configure(state="normal")
                    self.log.insert("end", f"{msg}\n", (color,))
                    self.log.tag_configure(color, foreground=color)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "busy":
                    busy = rest[0]
                    self._busy = busy
                    state = "disabled" if busy else "normal"
                    self.deliver_btn.configure(state=state)
                    self.connect_btn.configure(state=state)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_ui)

    def _thread(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _pick_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Select finished video", filetypes=[("MP4", "*.mp4")],
            initialdir=ASSETS)
        if path:
            self.video_path = path
            self.video_label.configure(text=f"Video: {Path(path).name}")
            self._log(f"Video selected: {path}")

    # -- actions ---------------------------------------------------------

    def _connect(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._thread(self._connect_worker)

    def _connect_worker(self) -> None:
        try:
            if authorize(self._log):
                self._log("TikTok connected.", GOOD)
        finally:
            self._set_busy(False)

    def _deliver(self) -> None:
        if self._busy:
            return
        if not self.video_path:
            self._log("Select a video first — or I'll open the picker:",
                      MUTED)
            self._pick_video()
            if not self.video_path:
                return
        self._set_busy(True)
        self._thread(self._deliver_worker)

    def _deliver_worker(self) -> None:
        try:
            self._log("--- Deliver flow ---", ACCENT)

            script = find_script(self.video_path)
            if script.exists():
                self._log(f"Script found: {script}")
                self._log("Generating captions (DeepSeek)…")
                platforms = generate_captions(
                    script.read_text(encoding="utf-8"))
                self.captions = {"platforms": platforms}
                for name, cap in platforms.items():
                    self._log(f"  {name:8} {cap.get('title', '')}", GOOD)
                self._log("Captions ready.", GOOD)
            else:
                self._log("No script found — uploading with the file name "
                          "as the title.", MUTED)
                self.captions = {"platforms": {}}

            env = _load_env()
            if not env.get("TIKTOK_ACCESS_TOKEN"):
                self._log("No saved connection — starting one-time OAuth…")
                if not authorize(self._log):
                    self._log("Connect cancelled — nothing was uploaded.", BAD)
                    return

            self._log("Uploading to your TikTok inbox…")
            tiktok_draft(self.video_path, self.captions, log=self._log)
            self._log("Delivered. Open the TikTok app — the video is in your "
                      "inbox as a draft (tap Post to publish).", GOOD)
        except SystemExit as exc:
            if "access_token_invalid" in str(exc) or "401" in str(exc):
                self._log("Connection expired — re-authorizing once…", MUTED)
                if authorize(self._log):
                    try:
                        tiktok_draft(self.video_path, self.captions,
                                     log=self._log)
                        self._log("Delivered. Open the TikTok app — the video "
                                  "is in your inbox as a draft.", GOOD)
                        return
                    except SystemExit as retry_exc:
                        self._log(f"Delivery failed after reconnect: "
                                  f"{retry_exc}", BAD)
                        return
            self._log(f"Delivery failed: {exc}", BAD)
        except Exception as exc:  # noqa: BLE001 -- GUI boundary
            self._log(f"Delivery failed: {exc}", BAD)
        finally:
            self._set_busy(False)


def main() -> int:
    _postlock._load_env()
    env = _load_env()
    if not env.get("TIKTOK_CLIENT_KEY") or not env.get("TIKTOK_CLIENT_SECRET"):
        print("Missing TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env "
              f"({ENV_PATH})")
        return 1

    root = Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:  # noqa: BLE001
        pass
    style.configure("TButton", background="#1d2433", foreground=FG,
                    bordercolor="#2a3348", focusthickness=0)
    style.map("TButton", background=[("active", "#283248")])
    style.configure("Card.TFrame", background=BG)
    style.configure("Title.TLabel", background=BG, foreground=FG,
                    font=("Segoe UI", 16, "bold"))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)

    PostLockApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
