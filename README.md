# PostLock

Open-source publishing tools for short-form creators: turn a narration
script into platform-ready titles and captions, and deliver finished videos
to your TikTok inbox for review before publishing.

PostLock is built for creators who want an automated *preview step* — the
video and its copy land in your accounts for a final look, nothing goes
public without you. When you're ready, the same code posts directly.

## What it does

- **Captions** — one script in, platform-ready title + caption + hashtags
  out (TikTok, YouTube Shorts, and more), tuned to a clear, question-raising
  short-form style. `postlock.py captions --script script.txt`
- **Draft delivery (TikTok)** — uploads the finished MP4 to your TikTok
  inbox via the official Content Posting API upload flow
  (`post_mode: MEDIA_UPLOAD`, `video.upload` scope). You open the TikTok app
  and tap Post — nothing is published without you.
- **Draft delivery (YouTube)** — uploads as a private video via the Data
  API v3; publish or schedule it in YouTube Studio.

## Quick start

```bash
# 1. configure
cp .env.example .env        # add your DeepSeek + TikTok credentials

# 2. captions from a narration script
python postlock.py captions --script script.txt --out captions.json

# 3. preview what would be uploaded (posts nothing)
python postlock.py tiktok-draft --video final.mp4 --captions captions.json --dry-run

# 4. deliver to your TikTok inbox for review
python postlock.py tiktok-draft --video final.mp4 --captions captions.json
```

The TikTok flow uses the official **Content Posting API — Upload draft to
TikTok** endpoint (`/v2/post/publish/inbox/video/init/`). Your access token
comes from the standard TikTok OAuth flow; the tool never sees your TikTok
password.

## Requirements

- Python 3.10+
- A DeepSeek API key (or any OpenAI-compatible endpoint — set
  `DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL`)
- A TikTok developer app approved for the `video.upload` scope
  (see the app review guide)

## Documentation

- [PostLock site](https://kilolime.github.io/PostLock/)
- [Terms of Service](https://kilolime.github.io/PostLock/terms.html)
- [Privacy Policy](https://kilolime.github.io/PostLock/privacy.html)

## License

MIT — see [LICENSE](LICENSE).
