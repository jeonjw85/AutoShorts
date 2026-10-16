# AutoShorts

Make Korean Shorts on an Apple Silicon Mac. The app writes scripts, records narration, edits vertical video, and can upload to YouTube. Use the CLI or a private Telegram bot.

Models run on the Mac. The default workflow does not need a paid inference API or a hosted server.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Version: 0.1.3](https://img.shields.io/badge/Version-0.1.3-0F766E)](pyproject.toml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Apple Silicon](https://img.shields.io/badge/macOS-Apple_Silicon-181717?logo=apple&logoColor=white)](#requirements)

[![MLX inference](https://img.shields.io/badge/Inference-Local_MLX-536DFE)](https://github.com/ml-explore/mlx)
[![FFmpeg](https://img.shields.io/badge/Video-FFmpeg-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![uv](https://img.shields.io/badge/Package_manager-uv-DE5FE9?logo=uv&logoColor=white)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/badge/Lint-Ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)

[Quickstart](#quickstart) | [Workflows](#workflows) | [Channels](#channels) | [Integrations](#integrations) | [Operations](#operations) | [Development](#development) | [License](#license)

## Highlights

- Planning, speech, transcription, and visual analysis run locally with MLX.
- Each channel has its own schedule, style, upload target, and pause control.
- Instagram edits can add Korean narration or keep the original audio with Korean subtitles.
- Article links and RSS feeds can supply news cards with Korean narration, sources, and dates.
- Completed stages are kept. Retries are limited, and an interrupted upload can resume.
- Telegram can queue jobs, change settings, and send a preview.
- Default output is 9:16, 1080 x 1920, 30 fps, H.264, and AAC.

## Workflows

| Workflow | Input → output | Status |
| --- | --- | --- |
| **Stories** | Topic or script → Korean narration, illustrated or generated images, and captions | Implemented; local end-to-end generation verified |
| **Instagram edits** | Reel or single-video post → narrated edit or original-audio subtitled edit | Implemented; local editing verified, live collection validation pending |
| **News cards** | Article link or RSS item → large two-line cards, Korean narration, source, and date | Implemented; ingestion and rendering tested with fixtures, live article/model acceptance pending |

The production CLI supports `story`, `instagram`, and `news`. Story visuals use still-image motion, Instagram edits use source footage, and news uses article images or text cards. Captions are displayed per scene or speech segment.

## Quickstart

### Requirements

- macOS on Apple Silicon; development and local acceptance runs use an **M5 MacBook Air with 24 GB unified memory**.
- Python **3.11+**, [uv](https://docs.astral.sh/uv/), and FFmpeg with `ffprobe`.
- Model storage: approximately **9.35 GiB** for the default planner and speech model; optional image generation adds **5.5 GiB**, and Instagram analysis adds **4.4 GiB**. Allow additional space for dependencies and media.

### 1. Install and initialize

Clone the repository and install the local toolchain. This example uses [Homebrew](https://brew.sh/) for system tools:

```bash
git clone https://github.com/jeonjw85/AutoShorts.git
cd AutoShorts

brew install uv ffmpeg
uv sync --extra mlx --group dev

.venv/bin/shorts-local init
.venv/bin/shorts-local doctor
.venv/bin/shorts-local models
```

`models` downloads the configured public model weights and reuses cached files. After installation, use `.venv/bin/shorts-local` or `uv run --no-sync shorts-local` to preserve the installed optional dependencies.

### 2. Create your first video

```bash
.venv/bin/shorts-local create \
  --topic "A warm fictional story about a letter arriving from tomorrow" \
  --local-only
.venv/bin/shorts-local work
.venv/bin/shorts-local status
```

`create` queues a job. `work` processes one eligible job and prints the finished video path. The default planner writes Korean narration. Local-only jobs do not require Telegram or YouTube credentials.

To start from the included script instead of generating a new one:

```bash
.venv/bin/shorts-local create --story examples/letter.json --local-only
.venv/bin/shorts-local work
```

### 3. Inspect the output

Each job keeps its inputs, intermediate artifacts, and validation results:

```text
data/jobs/<job-id>/
├── settings.json       Channel settings captured when queued
├── story.json          Script, headings, and image prompts
├── voice/              Korean narration clips
├── images/             Optional generated images
├── art/                Backgrounds and caption overlays
├── segments/           Rendered video segments
├── video.mp4           Final video
├── cover.png           Story preview image
├── captions.srt        Captions
├── timeline.json       Measured scene timing
└── validation.json     Media checks and hashes of completed artifacts
```

Instagram jobs additionally retain source media, transcripts, visual observations, edit plans, and publication metadata. Stage logs are stored alongside the artifacts.
News jobs keep `input.json`, `article.json`, optional `image.jpg`, `news-plan.json`, narration, captions, and publication metadata. Article and plan caches include their input keys.

## Channels

Each profile has its own content settings, daily schedule, and YouTube account. Profiles share one bot, one SQLite queue, and one worker. The worker runs one heavy job at a time.

```bash
.venv/bin/shorts-local channel add animals --name "Animal Stories"
.venv/bin/shorts-local config --channel animals topic "Short fictional stories about animals"
.venv/bin/shorts-local config --channel animals schedule_time "21:00"
.venv/bin/shorts-local config --channel animals --values '{"width":720,"height":1280}'

.venv/bin/shorts-local create --channel animals --local-only
.venv/bin/shorts-local channel list
.venv/bin/shorts-local status --all
```

### Defaults

| Setting | Default |
| --- | --- |
| Production type | `story` |
| Planner / visuals | `mlx` / `cards` |
| Speech voice | `Sohee` |
| Schedule | Enabled; daily at `09:00`, `Asia/Seoul` |
| Automatic YouTube publishing | Disabled until YouTube login |
| Upload visibility | `public` |
| Instagram collection interval | 60 minutes |
| News RSS collection interval | 60 minutes |

- Settings are stored in `data/state.sqlite3`. Inspect them with `config`; use `--values` for an atomic update of related fields.
- A job keeps the channel, settings, and inputs it had when queued. Later setting changes apply to new jobs, and publishing stays tied to the job's original profile.
- Creation, configuration, and login commands default to the `default` profile when `--channel` is omitted.
- CLI `pause` / `resume` without a channel apply globally. `--channel animals` controls that profile independently. Running work finishes before a pause takes effect.
- `schedule_time` starts production; publishing follows completion. Missed days do not accumulate a production backlog.

Use `--home /path/to/data` before the command, or set `SHORTS_HOME`, to select a different data directory. Use the same directory for every command and service instance. Legacy `config.json` files are kept only for migration. Current settings are stored in SQLite.

## Instagram editing

Install the complete local media toolchain and download the analysis models:

```bash
uv sync --extra mlx --extra images --extra instagram --group dev
.venv/bin/shorts-local models --instagram
.venv/bin/shorts-local instagram-login YOUR_USERNAME
```

Password and two-factor prompts use hidden terminal input. Successful login restores jobs waiting for Instagram authentication.

### Import a direct link

Replace `SHORTCODE` with a real Reel or single-video post identifier:

```bash
.venv/bin/shorts-local import-video \
  'https://www.instagram.com/reel/SHORTCODE/' \
  --mode narrated --local-only
.venv/bin/shorts-local work
```

| Mode | Audio | Captions and timing |
| --- | --- | --- |
| `narrated` | New Korean narration; source audio muted | Based on observed content; playback speed stays between 0.8x and 1.25x to fit the measured narration |
| `original` | Original audio at 1x speed | Korean subtitles aligned to source speech; title-only overlays for silent clips |

The full source frame fits within the vertical canvas, with blurred fill where needed. Output descriptions retain the original account and link. Supported source media is 1 second to 10 minutes long, up to 300 MiB.

### Collect from designated accounts

Using the `animals` profile created above:

```bash
.venv/bin/shorts-local config --channel animals production_type instagram
.venv/bin/shorts-local config --channel animals instagram_edit_mode narrated
.venv/bin/shorts-local config --channel animals instagram_style "Bright, concise Korean commentary"
.venv/bin/shorts-local source add SOURCE_USERNAME --channel animals
.venv/bin/shorts-local source list --channel animals
.venv/bin/shorts-local materials --channel animals
.venv/bin/shorts-local serve
```

Collection starts with posts published after source registration. At the daily schedule, the oldest unused item supplies one job per channel. Empty days wait for new material. Repeated links within a channel return the existing job.

Use `source scan --channel animals` for a bounded collection check, or `source pause SOURCE_ID` / `source resume SOURCE_ID` to control a source. Direct imports and per-job `--type` / `--mode` overrides preserve the channel defaults.

## News cards

News uses the existing planner and speech models. No news API subscription or additional model is required.

Import one public HTTPS article immediately:

```bash
.venv/bin/shorts-local import-article 'https://example.com/story' --local-only
.venv/bin/shorts-local work
```

For daily production from RSS 2.0 or Atom, replace the example URL with a real feed:

```bash
.venv/bin/shorts-local channel add news --name "Daily News"
.venv/bin/shorts-local config --channel news production_type news
.venv/bin/shorts-local news-source add 'https://example.com/feed.xml' --channel news
.venv/bin/shorts-local news-source list --channel news
.venv/bin/shorts-local news-source scan --channel news
.venv/bin/shorts-local serve
```

Only items published after feed registration enter the automatic queue. At the scheduled time, the oldest unused article supplies one job. An empty feed leaves the day open for a later article. Tracking parameters are removed for deduplication within each channel. Direct imports can use older articles and preserve channel defaults.

Use `create --type news --channel news --local-only` to queue an unused article manually. Control a feed with `news-source pause SOURCE_ID` and `news-source resume SOURCE_ID`. Set the tone with `news_style`, the polling interval with `news_poll_minutes` (15–1440), and the accent with `instagram_accent_color`.

Each video has 4–6 cards. Every card includes the source and publication date in the channel timezone; the description includes the canonical article link. Failed or missing representative images fall back to text cards.

Articles need at least 400 characters of extractable text and a publication date from the page or feed. Login-only, paywalled, and script-rendered pages may not work. A date is never invented. Public-address checks apply to every redirect and image request.

Before narration, the app checks quotations and numbers, then asks the configured planner to compare every claim with the article. A rejected plan gets one correction before the job becomes `blocked`. These checks compare the script with one source; they do not independently establish that source's accuracy. Fixture tests do not establish coverage of every outlet or replace a live article/model acceptance run.

## Integrations

### Telegram

Create a bot through **@BotFather**, open its private chat, and send `/start`. Configure the bot token and your numeric Telegram user ID locally:

```bash
.venv/bin/shorts-local telegram-setup
.venv/bin/shorts-local serve
```

The bot accepts private messages from the configured owner. It uses long polling and requires no inbound port or hosted server. Environment-based configuration is available through `SHORTS_TELEGRAM_TOKEN` and `SHORTS_TELEGRAM_USER_ID`.

| Command | Action |
| --- | --- |
| `/channels`, `/channel animals` | List profiles or select the active profile |
| `/create [topic]`, `/create_story [topic]` | Queue content using the profile default or explicitly create a story |
| `/import URL [narrated\|original]` | Queue an Instagram video; a direct link also works |
| `/article URL` | Queue a news article; a standalone non-Instagram HTTPS link also works |
| `/status`, `/queue`, `/settings` | Inspect the selected profile; append `all` to status or queue for all profiles |
| `/preview JOB_ID`, `/upload JOB_ID` | Receive a finished video or queue it for publishing |
| `/pause`, `/resume`, `/retry JOB_ID` | Control production and retry jobs |
| `/update` | Check for a newer release without changing files |
| `/pause all`, `/resume all` | Control the global queue |

<details>
<summary>More Telegram commands</summary>

| Command | Action |
| --- | --- |
| `/help` | Show available commands |
| `/channel_add animals Animal Stories` | Create a profile |
| `/source_add USERNAME`, `/sources` | Register or list collection sources |
| `/source_pause ID`, `/source_resume ID` | Pause or resume a source |
| `/source_scan`, `/materials` | Request collection or inspect available material |
| `/news_add FEED_URL`, `/news_sources` | Register or list RSS feeds |
| `/news_pause ID`, `/news_resume ID`, `/news_scan` | Control feeds or request a background check |
| `/set type news`, `/set news_style TEXT`, `/set news_poll_minutes 60` | Configure news production |
| `/set type instagram`, `/set edit_mode original` | Set production type and editing mode |
| `/set edit_style TEXT`, `/set accent #E8B257` | Set commentary style and accent color |
| `/set topic TEXT`, `/set name TEXT` | Set the topic and display name |
| `/set schedule 09:00`, `/set timezone Asia/Seoul` | Configure daily production timing |
| `/set schedule_enabled off` | Disable scheduled production |
| `/set planner mlx`, `/set visuals cards`, `/set voice Sohee` | Select generation settings |
| `/set privacy private`, `/set autopublish off` | Configure publishing |

</details>

`serve` must be running for the bot to respond. Channel selection survives restarts; job-ID commands always use the job's original profile. Previews over 49 MiB remain available locally.

### YouTube

Enable **YouTube Data API v3** in a Google Cloud project, configure the OAuth consent screen, and download a **Desktop app** OAuth client JSON. Add your account as a test user if the OAuth app is in testing mode.

```bash
.venv/bin/shorts-local youtube-login '/path/to/client_secret.json' --channel animals
```

**Successful login enables automatic publishing for new jobs in that profile.** Uploads follow its `privacy` setting, which defaults to `public`. Set a different visibility with `config --channel animals privacy private`, or disable automatic publishing with `config --channel animals youtube_enabled false`.

Each profile binds to one verified YouTube channel. Reauthentication preserves that identity; another destination requires another profile. A YouTube channel can belong to only one profile.

To publish an existing completed job:

```bash
.venv/bin/shorts-local publish JOB_ID
.venv/bin/shorts-local work
```

Credentials are stored under `data/channels/<channel-id>/auth/` with `0600` permissions. API quotas, channel upload limits, and OAuth expiration apply. External OAuth apps in **Testing** typically issue refresh tokens that expire after seven days; account setup affects long-running operation. Set `contains_synthetic_media` and `made_for_kids` to match the content. The [operations guide](docs/operations.md) covers reauthentication and interrupted uploads.

## Operations

### Run continuously

```bash
caffeinate -i .venv/bin/shorts-local serve
```

The service schedules jobs, collects sources, processes the queue, and handles Telegram messages. `caffeinate` prevents idle sleep; closing the lid can still suspend operation.

<details>
<summary>Start automatically at macOS login</summary>

```bash
.venv/bin/shorts-local service-file
mkdir -p ~/Library/LaunchAgents
cp data/dev.shorts-local.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/dev.shorts-local.plist
```

To stop the agent:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/dev.shorts-local.plist
```

`service-file` generates the configuration; installation is explicit. Regenerate and reinstall it after moving the project or virtual environment. Service output is written to `data/service.log`.

</details>

### Recovery behavior

- Completed stages are reused after interruption; invalid or partial artifacts are rejected.
- Global and channel pauses are independent. A blocked channel does not prevent another channel's eligible job from running.
- Ordinary failures have bounded retries. Authentication failures wait for login; quota and rate-limit failures back off.
- Upload sessions and receipts support resumption and deduplication. If the upload result is uncertain, the job waits instead of starting another upload.
- Generation is deferred below 5 GiB of free disk space. Models, source media, and completed outputs are retained.
- Database migrations preserve existing jobs and create backups under `data/backups/`.

For recovery procedures, job states, and migration details, see the [operations guide](docs/operations.md).

### Updates

```bash
.venv/bin/shorts-local --version
.venv/bin/shorts-local update
```

`update` checks the latest GitHub release. It changes the checkout only when this repository is clean and `serve` is not running. Telegram `/update` reports the release and does not change files.

### Cost model

The default MLX workflow uses local compute and public model weights, with no recurring inference or hosting subscription. Hardware, electricity, internet access, and any optional existing subscription are outside that service-cost budget. Downloads and external integrations require internet access; core story generation runs locally once its models are cached.

## Architecture

```text
CLI / Telegram
      │
      ▼
Channel profiles ── Scheduler / source collector
      │
      ▼
SQLite job queue
      │
      ▼
Production worker
      ├── Story:     plan → speech → artwork → render
      ├── Instagram: fetch → analyze → edit plan → speech* → render
      └── News:      fetch → extract → grounded plan → speech → cards
      │
      ▼
Media validation → local output → optional YouTube upload → notification

* New speech is generated only in narrated mode.
```

Model stages run in short-lived child processes and load one model at a time. Collection and Telegram handling continue while a job is running.

### Default engines

| Stage | Engine / model |
| --- | --- |
| Planning | MLX-LM, Gemma 3 12B, 4-bit |
| Korean narration | MLX-Audio, Qwen3-TTS 0.6B, 8-bit, Sohee |
| Transcription | MLX Whisper, Whisper large-v3-turbo |
| Visual analysis | MLX-VLM, Qwen3-VL 4B, 4-bit |
| Optional images | MFLUX, Z-Image Turbo, 4-bit |
| Composition | Pillow + FFmpeg |
| Persistence | SQLite + per-job artifacts |

<details>
<summary>Enable generated images</summary>

```bash
uv sync --extra mlx --extra images --extra instagram --group dev
.venv/bin/shorts-local models --images
.venv/bin/shorts-local config visuals mflux
.venv/bin/shorts-local create --local-only
.venv/bin/shorts-local work
```

Images are generated at 576 x 1024 and composed into the final vertical video. Set `visuals cards` to use deterministic local illustrations. Character consistency and scene accuracy depend on the image model.

</details>

<details>
<summary>Use an existing ChatGPT subscription for planning</summary>

Install the Codex CLI separately, then authenticate with ChatGPT:

```bash
codex login
.venv/bin/shorts-local config planner codex
```

This integration is limited to script planning. The app requires subscription login, removes API-key environment variables from that subprocess, and waits when subscription limits are reached. Account-side credit purchases follow the account's billing settings. Keep `planner mlx` for the fully local default.

</details>

## Validation

Local generation was checked on **October 11, 2026**, on an M5 MacBook Air with 24 GB unified memory. News integration checks were updated on **October 12, 2026**; regression and recovery checks were updated on **October 16, 2026**.

| Check | Result |
| --- | --- |
| Automated regression suite | **301 passed** |
| Static checks and dependency lock | Ruff and `uv lock --check` passed |
| Packaging | v0.1.3 wheel and source distribution built |
| Story pipeline | Local planning, Korean narration, and generated images; 65.21-second output |
| Instagram editing | 59.0-second original-audio edit and 39.7-second narrated edit from a local fixture |
| News-card prototype | 41.17-second, five-card video; encoded narration matched the script after text normalization |
| News integration | Article/RSS fixtures, channel scheduling, factual-validation rejection, 360 x 640 card rendering, and sealed recovery passed |
| Media output | Verified 1080 x 1920, 30 fps, H.264/AAC, including full decode checks |

The suite covers scheduling, channel isolation, recovery, duplicate commands, access control, real FFmpeg rendering, and simulated upload failures. Live-account acceptance for Instagram collection, Telegram messaging, and YouTube publishing remains pending. Media checks validate the file and timing; factual accuracy requires content review.

Detailed timings and memory measurements are in the [operations guide](docs/operations.md). Acceptance media and experiment records are local artifacts and are not included in the distribution.

## Development

```bash
uv sync --extra mlx --extra images --extra instagram --group dev
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
uv lock --check
uv build
```

```text
src/shorts_local/   CLI, queue, pipelines, model workers, and integrations
tests/             Unit, integration, recovery, and media tests
examples/          Hand-written sample scripts
docs/              Operations and recovery reference
data/              Local runtime state and generated media; gitignored
```

Use `.venv/bin/shorts-local --help` for the full CLI reference. Credentials, runtime media, and local configuration belong outside version control.

## Documentation

The operations guide is currently written in Korean.

- [Operations and recovery](docs/operations.md)

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

Third-party dependencies, model weights, and source media are covered by their respective licenses.
