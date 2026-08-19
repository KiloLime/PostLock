"""PostLock — open-source publishing tools for short-form creators.

Two commands:
  captions      --script script.txt --out captions.json
                (one DeepSeek call -> platform-ready title/caption/hashtags)
  tiktok-draft  --video final.mp4 --captions captions.json [--dry-run]
                (official Content Posting API upload flow -> your TikTok
                 inbox, post_mode=MEDIA_UPLOAD, video.upload scope; nothing
                 is published — you tap Post in the TikTok app)

Credentials come from .env (see .env.example). The tool never sees your
TikTok password: the access token comes from the standard TikTok OAuth flow.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
PROMPT_PATH = ROOT / "prompts" / "caption_batch.txt"

TIKTOK_API = "https://open.tiktokapis.com/v2"
TIKTOK_TOKEN_URL = f"{TIKTOK_API}/oauth/token/"
TIKTOK_INBOX_INIT = f"{TIKTOK_API}/post/publish/inbox/video/init/"
TIKTOK_STATUS_FETCH = f"{TIKTOK_API}/post/publish/status/fetch/"
STATUS_POLL_S = 3.0
STATUS_MAX_WAIT_S = 600.0


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------


def generate_captions(script: str, title: str = "") -> dict:
    api_key = _env("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("DEEPSEEK_API_KEY missing in .env")
    template = PROMPT_PATH.read_text(encoding="utf-8")
    user = template.replace("{{title}}", title or "Short-form story") \
                   .replace("{{script}}", script)
    resp = requests.post(
        f"{_env('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": _env("DEEPSEEK_MODEL", "deepseek-chat"),
            "messages": [
                {"role": "system",
                 "content": "You write post copy for a short-form narration "
                            "channel. Follow the prompt's strategy exactly."},
                {"role": "user", "content": user},
            ],
            "max_tokens": 4000,
            "temperature": 0.3,
        },
        timeout=120,
    )
    if resp.status_code != 200:
        sys.exit(f"caption generation failed HTTP {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
        platforms = parsed.get("platforms", {})
        assert isinstance(platforms, dict) and platforms
    except (json.JSONDecodeError, AssertionError) as exc:
        sys.exit(f"caption output unparseable: {exc}\n{content[:400]}")
    return platforms


# ---------------------------------------------------------------------------
# TikTok inbox delivery (official API, upload-for-review)
# ---------------------------------------------------------------------------


def _tiktoken_with_env(log=None) -> str:
    token = _env("TIKTOK_ACCESS_TOKEN")
    if not token:
        sys.exit("TIKTOK_ACCESS_TOKEN missing in .env — run the OAuth flow "
                 "(see docs) to authorize your account")
    return token


def _tiktok_token() -> str:
    return _tiktoken_with_env()


def _inbox_init(token: str, video_path: str, title: str) -> dict:
    size = os.path.getsize(video_path)
    chunk_size = min(5 * 1024 * 1024, size) if size else 5 * 1024 * 1024
    total_chunk_count = max(1, size // chunk_size)
    resp = requests.post(
        TIKTOK_INBOX_INIT,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps({
            "post_mode": "MEDIA_UPLOAD",
            "post_info": {"title": title},
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunk_count,
            },
        }),
        timeout=60,
    )
    if resp.status_code != 200:
        sys.exit(f"inbox init failed HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json().get("data", {})
    publish_id = data.get("publish_id", "")
    upload_url = data.get("upload_url", "")
    chunk_size = int(data.get("chunk_size") or (5 * 1024 * 1024))
    if not publish_id or not upload_url:
        sys.exit(f"inbox init returned no publish_id/upload_url: {resp.text[:300]}")
    return {"publish_id": publish_id, "upload_url": upload_url,
            "chunk_size": chunk_size}


def _put_chunks(token: str, upload_url: str, video_path: str, chunk_size: int,
                log=None) -> None:
    size = os.path.getsize(video_path)
    total = max(1, size // chunk_size) if chunk_size else 1
    sent = 0
    with open(video_path, "rb") as fh:
        for i in range(total):
            chunk = fh.read() if i == total - 1 else fh.read(chunk_size)
            if not chunk:
                break
            end = sent + len(chunk)
            resp = requests.put(
                upload_url,
                data=chunk,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {sent}-{end - 1}/{size}",
                },
                timeout=(60, 600),
            )
            if resp.status_code not in (200, 201, 206):
                sys.exit(f"chunk upload HTTP {resp.status_code}: {resp.text[:300]}")
            sent = end
            if log and (i + 1) % 4 == 0:
                log(f"  chunk {i + 1}/{total} uploaded")


def _wait_delivered(token: str, publish_id: str, log=None) -> None:
    """Poll the status API until the draft is delivered.

    log: optional callable(msg) for progress reporting (used by the GUI).
    """
    deadline = time.time() + STATUS_MAX_WAIT_S
    last_report = 0.0
    while time.time() < deadline:
        resp = requests.post(
            TIKTOK_STATUS_FETCH,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            data=json.dumps({"publish_id": publish_id}),
            timeout=60,
        )
        if resp.status_code != 200:
            sys.exit(f"status HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json().get("data", {})
        status = data.get("status", "")
        fail = data.get("fail_reason")
        if status == "FINISH":
            return
        if status == "SEND_TO_USER_INBOX":
            return
        if status == "FAIL" or fail:
            sys.exit(f"inbox upload failed: {fail or status}")
        now = time.time()
        if log and now - last_report >= 10:
            elapsed = int(now - (deadline - STATUS_MAX_WAIT_S))
            log(f"TikTok is processing the video… ({elapsed}s, "
                f"status: {status or 'processing'})")
            last_report = now
        time.sleep(STATUS_POLL_S)
    sys.exit(f"status timeout after {STATUS_MAX_WAIT_S}s "
             f"(publish_id={publish_id} — re-check with the status API)")


def tiktok_draft(video_path: str, captions: dict, dry_run: bool = False,
                 log=None) -> None:
    """Upload a finished video to the creator's TikTok inbox as a draft.

    log: optional callable(msg) for progress reporting (used by the GUI).
    """
    if not os.path.exists(video_path):
        sys.exit(f"video not found: {video_path}")
    cap = captions.get("tiktok") or {}
    title = cap.get("title", "") or os.path.basename(video_path)
    token = _tiktoken_with_env(log=log)

    if dry_run:
        print(f"[dry-run] POST {TIKTOK_INBOX_INIT} (post_mode=MEDIA_UPLOAD, "
              f"title={title!r}, size={os.path.getsize(video_path)})")
        print("[dry-run] then PUT the returned upload_url in chunks, then "
              f"POST {TIKTOK_STATUS_FETCH} until delivered")
        print("[dry-run] nothing posted — the video would land in your "
              "TikTok inbox for review")
        return

    if log:
        log(f"Uploading {os.path.getsize(video_path) // (1024 * 1024)} MB "
            f"in chunks…")
    init = _inbox_init(token, video_path, title)
    if log:
        log("Upload URL issued. Sending chunks…")
    _put_chunks(token, init["upload_url"], video_path, init["chunk_size"],
                log=log)
    if log:
        log("All chunks accepted. Waiting for TikTok to process the "
            "draft…")
    _wait_delivered(token, init["publish_id"], log=log)
    handle = _env("TIKTOK_USERNAME", "").lstrip("@")
    where = f"https://www.tiktok.com/@{handle}" if handle else "your TikTok app"
    if log:
        log(f"Delivered to your TikTok inbox ({where}) — open the app and "
            "tap Post to publish")
    else:
        print(f"delivered to your TikTok inbox ({where}) — open the app and "
              "tap Post to publish")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_captions(args) -> int:
    script = Path(args.script).read_text(encoding="utf-8") if args.script else \
        sys.stdin.read()
    platforms = generate_captions(script, title=args.title or "")
    out = Path(args.out) if args.out else ROOT / "captions.json"
    out.write_text(json.dumps({"platforms": platforms}, indent=2,
                              ensure_ascii=False), encoding="utf-8")
    print(f"captions written to {out}")
    for name, cap in platforms.items():
        print(f"  {name:8} {cap.get('title', '')}")
    return 0


def _cmd_tiktok_draft(args) -> int:
    captions = json.loads(Path(args.captions).read_text(encoding="utf-8"))
    platforms = captions.get("platforms", captions)
    tiktok_draft(args.video, platforms, dry_run=args.dry_run)
    return 0


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(prog="postlock", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("captions", help="script -> platform titles/captions")
    c.add_argument("--script", help="narration script file (or stdin)")
    c.add_argument("--title", default="", help="video title for context")
    c.add_argument("--out", default="", help="output JSON path")
    c.set_defaults(fn=_cmd_captions)

    t = sub.add_parser("tiktok-draft", help="upload video to your TikTok inbox")
    t.add_argument("--video", required=True, help="finished MP4")
    t.add_argument("--captions", required=True, help="captions.json from 'captions'")
    t.add_argument("--dry-run", action="store_true", help="print the requests, post nothing")
    t.set_defaults(fn=_cmd_tiktok_draft)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
