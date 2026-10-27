# Operations and recovery guide

## Job states

Jobs normally move from `queued` to `running`, then to `ready` or `uploaded`.

| State | Meaning | Action |
| --- | --- | --- |
| queued | Waiting in the queue | Run `work` or `serve` |
| running | A processing step is running | Check the logs in the job folder |
| retry | Waiting after a temporary error or quota limit | The daemon retries after the wait period |
| auth_wait | Waiting for Instagram authentication without using a retry attempt | Automatically requeued after a successful local `instagram-login USERNAME` |
| ready | Local output is complete | Preview it or run `publish JOB_ID` |
| uploaded | The video ID has been saved | Check the YouTube link |
| blocked | Authentication, installation, disk space, or an uncertain upload result needs attention | Fix the cause, then run `retry JOB_ID` |
| failed | Failed three times | Check the logs and retry manually |
| skipped | An unfinished scheduled job from a previous day | Today's scheduled job runs instead |

`pause` lets the current job finish and stops before the next one. Pressing Ctrl+C while `serve` is running in a terminal also waits for the current job to finish before exiting.
In the CLI, `pause` and `resume` without arguments apply globally; add `--channel animals` to target that channel. Global and channel pauses are independent, and both must be cleared before jobs can run. A job error on one channel does not prevent the next job on another channel from running.
If a process is forcibly terminated, the supervisor stops any remaining generation processes and holds the job lock until they exit. The next run recovers unfinished jobs. Jobs forcibly interrupted three or more times are not retried automatically.
After an interruption lasting several days, unfinished scheduled jobs from previous days are skipped. Sessions and receipts for jobs that have already started uploading are preserved, and the app continues checking their results. Manually queued jobs and jobs explicitly requested with `retry` or `publish` are not skipped just because their date has passed.

## Common commands

Run `.venv/bin/shorts-local` from the project folder to use the optional dependencies already installed in the environment.

```bash
.venv/bin/shorts-local status
.venv/bin/shorts-local status --all
.venv/bin/shorts-local channel list
.venv/bin/shorts-local pause --channel animals
.venv/bin/shorts-local pause
.venv/bin/shorts-local resume
.venv/bin/shorts-local retry JOB_ID
.venv/bin/shorts-local work
.venv/bin/shorts-local cleanup
.venv/bin/shorts-local cleanup --days 30 --apply
.venv/bin/shorts-local --version
.venv/bin/shorts-local update
```

`cleanup` lists transient job artifacts (narration clips, generated images, segments, stage logs) for finished jobs older than `--days` (default 14). It keeps `video.mp4`, `story.json`, `timeline.json`, `validation.json`, captions, and covers, and skips any job that still has an upload session. Nothing is deleted without `--apply`.

`update` checks the latest GitHub release. It fetches the release tag and runs `uv sync` with the currently installed extras only when this repository's checkout is clean and `serve` is stopped. If there are local changes or a running service, it stops and reports the reason. Telegram's `/update` only checks for updates.

## Story completion and recovery

- A new story video must pass a full FFmpeg decode before it becomes final. Its `validation.json` binds the job's original settings to hashes of `video.mp4`, `story.json` (including publication metadata), and the timeline.
- Retain `story.json` and `validation.json` with the video. A matching completed video can be retried or published without narration clips, generated images, or model loading, even below the disk-space threshold for new generation. A retained timeline must still match its hash; an absent timeline does not prevent publishing.
- Existing videos with an older media-only report, or no report after an interruption, receive a full decode check before being adopted without rerendering. Any existing upload session or receipt must match the original video bytes first.
- Changed or missing files on a job that has started uploading are blocked instead of regenerated. A damaged local video without upload records may be rebuilt. If a sealed script changes, restore its original bytes or create a new job with `create --story new-story.json`; old narration is not silently paired with an edited script.

## Interrupted uploads

`upload-state.json` contains a secret session URL, the file hash, and the target channel. The app queries the server for the session's status and resumes the upload.
If `upload-receipt.json` exists, the same file is not uploaded again. Do not share the session URL.

If the session expires and the result cannot be confirmed, the job becomes `blocked`.

1. Check YouTube Studio for the video's title and creation time to determine whether it was uploaded.
2. If the video exists, copy its 11-character ID and associate it with the job using the command below.

The job's original channel profile must be connected. This command, along with `publish` and `retry`, uses the channel assigned to the job, regardless of the current Telegram selection.

```bash
.venv/bin/shorts-local reconcile JOB_ID VIDEO_ID
```

3. Only after you have personally confirmed that the video does not exist, stop the service, rename the job's `upload-state.json` to keep a backup, and retry. The app does not remove this file automatically.

Deleting the database or the entire job folder also removes the records used to prevent duplicate uploads. Keep the settings, database, and job folders together.

## Authentication

- YouTube: Reconnect the profile with `youtube-login /path/to/client_secret.json --channel animals`. Upload and read permissions are used to verify the actual channel ID, and reauthentication is allowed only for the same target.
- YouTube uploads are checked with `videos.list` (`part=status,processingDetails`) up to three times, two seconds apart, right after the resumable upload finishes. `upload-receipt.json` records `upload_status`, `privacy_status`, and `verified_at`. A `rejected` or `failed` upload status stops the job as `blocked` on every retry. When YouTube reports a different `privacyStatus` than requested, the upload is kept and one warning is sent.
- Publish the OAuth consent screen (`In production`). In `Testing`, refresh tokens expire after seven days; the app warns once a day from day six when the auth file has `granted_at`, but it cannot renew an expired token without a new login. Reconnecting resets `granted_at`.
- Since October 2026, unverified apps can upload `public` videos without review, so the consent screen does not need Google verification for this use (Google Issue Tracker 572863050).
- Temporary authentication server or network errors are retried automatically. Revoked or expired refresh credentials require you to reconnect.
- If a connected channel's authentication JSON is damaged, signing in again to the same YouTube channel recovers it. The damaged original is saved as `youtube.damaged-*.json` in the same authentication folder with 0600 permissions. Credentials are not automatically replaced for an unconnected profile whose previous channel ID cannot be verified, or for a different target channel.
- Instagram: `instagram-login USERNAME` prompts for the password and a 2FA code when needed. `instagram-login USERNAME --session-file PATH` imports a saved Instaloader session (`sessionid`, `csrftoken`, `ds_user_id`) without prompting. The file is checked against the account before it is saved, and a malformed or rejected file ends with an error instead of falling back to a password prompt. Reconnecting clears the authentication wait for every channel; a wait recorded for one channel does not stop another channel from collecting.
- Telegram: After replacing the token, run `telegram-setup` and restart the daemon.
- Codex: `codex login status` for the CLI that actually runs must show a ChatGPT login. A wrapper on PATH without the actual CLI is not sufficient.
- Do not share or commit `data/auth/`, `data/channels/*/auth/`, or `upload-state.json`. Credential files are saved with 0600 permissions.

## Migrating to multiple channels

1. Wait for any existing `serve` or `work` process to finish, then run `.venv/bin/shorts-local init`. Migration runs only when the app can acquire the existing service and worker locks.
2. The app backs up the existing database to `data/backups/pre-channels.sqlite3` and associates settings and jobs with the `default` profile. Job IDs, completed files, retry records, notifications, and Telegram receive history are preserved.
3. The existing OAuth file is copied to `data/channels/default/auth/youtube.json`, leaving the original intact. If migration is interrupted, run the same command again. Conflicting files are not overwritten automatically.
4. After migration, SQLite is the source of truth for settings. Use `config` or `/set`; editing the archived `config.json` does not apply changes.

If the existing upload-only OAuth credentials lack read permissions, sign in again for that profile. Legacy completion receipts without target information are read only for migrated jobs to prevent duplicate publishing. Legacy in-progress sessions resume only when the target can be verified using the preserved original credentials. A new login alone is not used to infer the previous target. If the original credentials cannot verify it, follow the Studio check and `reconcile` procedure above.
Using multiple channels does not increase the Google project's shared quota. When that quota is exhausted, uploads for several channels may wait together while other local production continues.

## Models, disk space, and thermal management

### Instagram collection, authentication, and migration

- Collection runs separately from the shared AI worker, with limits of 120 seconds and 200 items per account. Incomplete pages are checked again after five minutes by default. Temporary errors use exponential backoff from five minutes to six hours. Rate limits wait for the specified duration, or at least one hour.
- After an initial full scan, routine scans stop independently for posts and reels after 20 consecutive posts older than the last full scan's start (or source registration). One old or pinned post does not stop collection. A full rescan is due one day after the previous full scan finishes; deeply out-of-order or delayed entries may wait for that rescan. Recent scans do not advance the full-scan watermark.
- When a scan resumes a saved tail, it first probes up to 20 fresh items from each feed within the same 200-item budget. This checks for new head posts without discarding historical progress, including when one feed has already been exhausted. Checkpoint metadata and the full-rescan completion time survive restarts.
- `source scan` and Telegram's `/source_scan` respect global, channel, and source pauses, as well as shared server backoff. The Telegram command schedules collection and responds immediately.
- `auth_wait` does not use a retry attempt. It is recorded per channel, so one broken source does not pause collection for the other channels that share the service. Reconnecting with `instagram-login USERNAME` automatically requeues affected jobs under the existing date and pause rules. A manual `retry` overrides date-based skipping but does not resolve the authentication problem itself.
- If the database update is interrupted after a new session file is saved, the next service or worker run recovers it. Existing jobs retain the mode, style, and source values set when they were created, rather than taking the current channel settings.
- On days without available source material, `status` and `materials` show that the app is waiting for material. Repeated alerts for the same problem are suppressed while it persists. Unsupported photo or carousel posts and inaccessible material are recorded with a reason and are not automatically selected again.
- The job steps are `fetch`, `analyze`, `edit-plan`, `speech` (narrated videos only), `render`, and `upload`, in that order. Partial files are not reused as completed output. The final `validation.json` ties the inputs and settings to hashes of the video, metadata, and timeline. If completed output with an upload record changes, the job stops as `blocked` instead of rendering again.
- Narration timing is adjusted at most once. The original edit plan, sentences, and measured speech durations are kept together in `timing-feedback.json`. The app first calculates non-overlapping cuts within the existing segments and adjacent gaps, reusing the measured speech. If that cannot fit, it replans the sentences using the measured durations. If speed or duration is still out of range after the adjustment, the job becomes `blocked`.
- For original-audio videos, total duration is validated to the frame during planning. Invalid cached plans are regenerated. Audio for intermediate cuts is kept as PCM to avoid accumulating AAC encoder delay. Only the final output is encoded as AAC, and the maximum duration still applies.
- Instagram support was introduced in schema 2. The current app migrates to schema 3, which includes news storage. For schema 1 installations, stop the service and workers, then run `init` to migrate. The app first creates `backups/pre-instagram.sqlite3` with 0600 permissions and keeps the existing `pre-channels.sqlite3`. Job IDs, settings JSON, completed output, YouTube targets, and Telegram history are preserved.
- If migration fails, run `init` again. Before restoring a backup, stop the service and save separate copies of the current database and job folders. Restoring only an older database can lose duplicate-prevention and publishing records created since that backup.

### News articles, RSS, and migration

```bash
.venv/bin/shorts-local channel add news --name "News Snapshot"
.venv/bin/shorts-local config --channel news production_type news
.venv/bin/shorts-local news-source add 'https://example.com/feed.xml' --channel news
.venv/bin/shorts-local news-source scan --channel news
.venv/bin/shorts-local import-article 'https://example.com/story' --channel news --local-only
.venv/bin/shorts-local create --channel news --type news --local-only
.venv/bin/shorts-local work
```

- Replace the example URLs with real article or RSS URLs. Article links are registered immediately. RSS 2.0 and Atom feeds automatically save only items published after the feed was registered as source material. Each canonical article URL is associated with one job per channel. Failed or skipped material is not automatically made available again.
- At the scheduled time, the app selects only the oldest unused article. If no material is available, it waits. `import-article` and an explicit `create --type news` do not change the channel defaults or the scheduling key for that day.
- `news_max_age_hours` (default 48, 0 disables the limit, maximum 720) drops feed items older than the window when the feed is saved and when the next article is selected. `import-article` and an explicit `create --type news` are not filtered, because the user chose that article.
- Manage feeds with `news-source list/pause/resume`. The default polling interval is 60 minutes, and `news_poll_minutes` accepts values from 15 to 1440 minutes. Each collection run is limited to 120 seconds and 200 items. Failures trigger a wait of five minutes to six hours. The same failure alert is sent only once until recovery.
- Telegram supports `/article URL`, `/news_add FEED_URL`, `/news_sources`, `/news_pause ID`, `/news_resume ID`, `/news_scan`, and `/set type news`. `/news_scan` only schedules a check. DNS lookups and downloads run in a worker outside the command transaction, so `/pause` can still be processed during a slow feed request.
- HTML is limited to 5 MiB and the featured image to 12 MiB. The article-fetching process has a 30-second limit. If image retrieval fails after text extraction succeeds, the app creates text cards from the saved article body. Private addresses and invalid redirects are rejected before connecting.
- If the body has fewer than 400 characters, or neither the page nor RSS provides a publication date, the material remains `unsupported` and the job stays `blocked`. The page date takes precedence over the RSS date; today's date is never used as a substitute. Dynamic pages, paywalls, and publisher-specific HTML can prevent extraction. JSON-LD (`application/ld+json`, including arrays and `@graph`) supplies a fallback body, date, headline, and image when the visible markup lacks them, but it never overrides Open Graph or article metadata.
- Site-wide clocks, modification dates, and event dates in the article body are not treated as publication dates. If publication metadata or an article-header timestamp cannot be verified, the RSS date is used. Temporary DNS errors use the existing network retry policy of up to three attempts rather than rejecting the material.
- RSS publication times are evaluated against the time the fetch completes. If any items are still dated in the future, ETag and Last-Modified values are not saved, allowing those items to be checked again on the next fetch. RSS downloads are also deferred when free disk space is below 5 GiB.
- `news_style` provides tone guidance. After checking quotations and numbers against the article body, a separate local planning call cross-checks people, outcomes, and negations. If the script is rejected after one correction, the item becomes `unsupported`, the job becomes `blocked`, and a scheduled job enqueues the next unused article as a replacement (at most three replacements per channel and date). Manual `/article` and `create` jobs are not replaced. Running `retry` with the same inputs and settings does not grant unlimited validation attempts. Input validation does not establish whether the article itself is true or whether multiple sources agree.
- The job folder's `article.json` and `news-plan.json` are caches keyed by their inputs. If the body hash or settings change, the plan is validated again. Speech is reused by sentence, model, and voice. The final `validation.json` links the video, metadata, and timeline. When they match, publishing can recover without downloading the article again or running a model. If a file changes after its upload has started, the job is put on hold rather than rendered again.
- Before migrating from schema 2 to schema 3, the app creates `backups/pre-news.sqlite3` with 0600 permissions. Existing story and Instagram job inputs, settings, files, YouTube targets, notifications, Telegram receive history, and earlier backups are preserved. Stop `serve` and `work`, then run `init` to migrate the operational database.
- News-specific defaults are excluded from the input keys of existing Instagram completion records. Instagram videos completed or already uploading under an earlier version can also recover using their original files.
- Collection, card rendering, and recovery are tested with mock HTTP and real SQLite and FFmpeg. Runs combining real articles with local models require separate verification. Passing fixtures does not establish support for every publisher.

### Memory and storage

The cached Gemma 3 12B 4-bit and TTS models total about 9.35 GiB. The optional image model needs another 5.5 GiB. The virtual environment, package cache, and intermediate videos require additional space.
Instagram analysis adds about 1.50 GiB for Whisper and 2.90 GiB for Qwen3-VL.
Models are stored in `~/.cache/huggingface/hub` by default, and the cache may be shared with other apps. Three unused comparison models downloaded during development were removed, recovering about 12 GiB. The app does not automatically delete models or completed output.

Generation tasks run sequentially on 24 GB of unified memory. Running other large-model apps during video generation can increase memory pressure and slow processing.
The MacBook Air has no fan, so performance can vary under sustained load. Start with one video per day and check actual processing times.

Existing jobs keep the settings they were created with, even after you change the configuration. Create a new job to use a different model or style.
Manually changing a script, audio, or images before a job finishes can leave its processing steps out of sync. For script changes, create a new job with `create --story new-story.json`.

### Local measurements (2026-10-11)

Measurements were taken on an M5 with 24 GB of memory, using a copy of an existing 65.21-second Korean sample as external video input. The run used the actual generation models and FFmpeg. Instagram downloading requires a separate connectivity check.

| Step | Measured time | Child process peak RSS / memory footprint |
| --- | --- | --- |
| Korean transcription | 4.65 to 11.39 seconds | About 1.95 GB / 3.25 GB |
| Scene analysis of 28 frames | 45.30 seconds | About 4.08 GB / 6.12 GB; MLX peak about 4.0 GB |
| Successful edit planning | 36.40 to 39.38 seconds | About 6.02 GB / 10.37 GB |
| Generation of six Korean narration segments | 16.47 seconds | About 2.46 GB / 8.19 GB |
| Translation of two English sentences into Korean | 8.91 seconds | ID preservation verified with separate local input |

RSS and macOS memory footprint are different metrics. The values above are for individual processes run sequentially, not combined memory usage or guaranteed processing times for every video. Model revisions and raw timing and memory records are in `.omo/sdd/instagram-import-editing/model-metrics.json` and `model-storage.json`.

- Final output: 59.0 seconds for the original-audio version and 39.7 seconds for the narrated version. Both were 1080x1920 at 30 fps with H.264 video and AAC audio, and both passed full decoding checks.
- After fixes, rendering and validating both videos took 58.31 seconds total, reusing cached analysis, planning, and speech. Some regression tests ran concurrently. This time excludes the initial model runs.
- Comparing the original or generated audio against output waveforms across 12 cuts gave a measured delay of 0 ms and correlation coefficients from 0.918 to 1.000. The narrated video's playback speed ranged from about 0.914x to 1.000x.
- The Korean source transcription matched all 241 script characters after excluding spaces and punctuation. The vision model misread some text already embedded in the source and split some sentences. Success on this sample does not establish the accuracy of scene descriptions in general.
- Results and synchronization measurements are in `.omo/sdd/instagram-import-editing/measurement-accepted/acceptance-results.json` and `audio-sync.json`. Initial failures and the fixes are also preserved in the same run directory.

The regression run at that time passed all 218 tests. Ruff, lockfile checks, and wheel and sdist builds also passed. One `utcfromtimestamp` deprecation warning remained from the pinned Instaloader version on Python 3.13.

## Korean captions and speech

- The default font is Apple SD Gothic Neo on macOS. On other systems, set `font_path` to a Korean font.
- Speech quality depends on the model's Korean pronunciation. Use a sample video to check the voice and sentence length.
- If a long script or repetitive TTS output exceeds the duration limit, the job fails before uploading.
- Videos use static captions for each scene with background motion. Word-by-word caption highlighting, fully AI-generated video, and automatic background music selection are not currently supported.

## Service liveness and alert fallback

- While `serve` runs, it rewrites `data/heartbeat.json` every 15 seconds. `status` shows the last write and marks the service as stale after 5 minutes. A stale heartbeat means the process stopped or the Mac was asleep; scheduled jobs are not backfilled.
- Stage logs rotate at 20 MiB and `data/service.log` rotates the same way at startup. Rotation uses copytruncate because the running service holds the file descriptor.
- When Telegram is not configured, pending notifications are appended to `data/alerts.log` instead of being sent. **They are not retried later**; the file is the delivery. Read it on the next machine wake-up.
- If Telegram is configured but a send keeps failing, an alert is retried five times, then written to `data/alerts.log`. Every alert line includes the timestamp and job ID when one exists.
- `status` reports the queue depth, pending notifications, and per-source collection health. `doctor` reports blocking configuration such as channels waiting for Instagram login.

## When the bot does not respond

Check that `serve` is running, the Mac is awake, the conversation is private, and the user ID is correct.
Do not use the same bot token in two long-polling apps at once. Service logs are in `data/service.log`.
Use `/channels` to list channels and `/channel animals` to select one. The selection persists across restarts. In Telegram, `/pause` applies to the selected channel and `/pause all` applies globally. In the CLI, `pause` without arguments applies globally.
`/set schedule_enabled off` disables scheduling for the selected channel, and `/set autopublish off` disables its automatic uploads. Commands that take a job ID apply to the job's original channel and include that channel in the response.
You can continue local production with `create --local-only` and `work` even without a YouTube or Telegram connection.
Command results and video preview deliveries use separate queues. If a video delivery fails, subsequent commands such as `/pause` are still processed. Receiving an already processed command again does not change the job a second time.
