# meshy-youtube-mcp

**An MCP server that takes a text prompt all the way to a published YouTube video:
[Meshy.ai](https://www.meshy.ai/) 3D generation → Blender turntable → YouTube upload.**

```
prompt ──▶ Meshy text-to-3D ──▶ Blender 360° turntable ──▶ ffmpeg ──▶ YouTube videos.insert
            (.glb model)          (PNG frames)             (.mp4)        (published video)
```

Any MCP-capable agent — Claude, or anything that speaks [MCP](https://modelcontextprotocol.io)
— can call it to generate rotating 3D content and publish it straight to YouTube.

> **Sibling project:** [`meshy-bottube-mcp`](https://github.com/Scottcjn/meshy-bottube-mcp)
> publishes the same pipeline to **BoTTube** (a video network *for AI agents*).
> **BoTTube is the agent-native channel; YouTube is the human-reach channel** — same
> Meshy generation, two audiences. Pick the publisher that fits who's watching.

## Tools

| Tool | Input | Output |
|------|-------|--------|
| `generate_3d_model` | prompt, art_style | `.glb` path + Meshy task ids (preview→refine) |
| `get_meshy_task_status` | task_id | status / `.glb` path on success |
| `render_turntable` | `.glb` | PNG frames (needs Blender) |
| `frames_to_video` | frames dir | `.mp4` |
| `upload_to_youtube` | `.mp4`, title | `video_id`, `watch_url` (OAuth) |
| `meshy_to_youtube` | prompt, title | **one-shot full pipeline** → `watch_url` |

## Requirements

- Python 3.10+
- [`ffmpeg`](https://ffmpeg.org/) and [Blender](https://www.blender.org/) on `PATH`
- A [Meshy.ai](https://www.meshy.ai/) API key
- A Google account + a YouTube OAuth client (one-time setup, below)

## Install

```bash
git clone https://github.com/Scottcjn/meshy-youtube-mcp
cd meshy-youtube-mcp
pip install -r requirements.txt
cp .env.example .env   # add your MESHY_API_KEY
```

## One-time YouTube authorization

YouTube uploads use OAuth2 (not a simple API key). Set it up once:

1. [Google Cloud Console](https://console.cloud.google.com/) → create/select a project
2. Enable **YouTube Data API v3**
3. **Credentials → Create OAuth client ID → Desktop app** → download the JSON
4. Save it as `~/.config/meshy-youtube-mcp/client_secret.json`
   (or set `YOUTUBE_CLIENT_SECRET_FILE`)
5. Authorize once — opens a browser, mints a reusable token:

```bash
python -m meshy_youtube.authorize
```

After that, uploads run unattended via the stored refresh token. You only
re-authorize if the token is revoked or deleted.

> **Quota:** YouTube's default free quota is 10,000 units/day and a
> `videos.insert` costs 1,600 — about **6 uploads/day**. Request more in the
> Cloud Console if you need it.

## Run as an MCP server

```json
{
  "mcpServers": {
    "meshy-youtube": {
      "command": "python3",
      "args": ["/path/to/meshy-youtube-mcp/meshy_youtube/server.py"],
      "env": {
        "MESHY_API_KEY": "your_meshy_key",
        "YOUTUBE_TOKEN_FILE": "/home/you/.config/meshy-youtube-mcp/token.json"
      }
    }
  }
}
```

Then ask your agent: *"Generate a 3D crystal dragon and publish it to YouTube as
an unlisted turntable."* It calls `meshy_to_youtube` and hands back a watch URL.

You can also `pip install -e .` and run `meshy-youtube-mcp`, or
`python -m meshy_youtube.server`.

## Use as a library

```python
from meshy_youtube import meshy, turntable, video, youtube

info  = meshy.generate("a steampunk robot", "model.glb", art_style="realistic")
tt    = turntable.render(info["glb_path"], "frames/", resolution=1080)
mp4   = video.frames_to_video(tt["frames_dir"], "turntable.mp4")
res   = youtube.upload(mp4, title="Steampunk Robot — 3D Turntable",
                       tags=["3d", "meshy"], privacy="unlisted")
print(res["watch_url"])
```

## Privacy & categories

- `privacy`: `public` | `unlisted` | `private` (default **unlisted** — shareable
  by link, not surfaced publicly until you choose to).
- `category_id`: YouTube category. Defaults to `22` (People & Blogs). Common ones:
  `1` Film & Animation, `20` Gaming, `23` Comedy, `24` Entertainment,
  `28` Science & Technology.

## Behavior notes

- The one-shot `meshy_to_youtube` **always returns a dict** (`ok` + `watch_url`,
  or `ok=False` + `error`/`failed_stage` + partial paths). Granular tools raise.
- Secrets (`client_secret.json`, `token.json`, `.env`) are gitignored and the
  token is written `0600`. Never commit them.
- The Meshy/Blender/ffmpeg stages are shared, hardened code from the
  [BoTTube edition](https://github.com/Scottcjn/meshy-bottube-mcp) (two-stage
  preview→refine, atomic GLB download, subprocess isolation, numeric frame
  normalization, bounds + preflight).

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT © 2026 Scott Boudreaux / [Elyan Labs](https://github.com/Scottcjn).
