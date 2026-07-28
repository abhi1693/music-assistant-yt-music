# YouTube Music for Music Assistant

An experimental Music Assistant music provider for YouTube and YouTube Music.
It supports anonymous catalogue playback, optional account-backed library
features, Last.fm-powered discovery, and persistent local playback of completed
background downloads for bandwidth-constrained installations.

This is an unofficial community integration. It is not affiliated with Google,
YouTube, YouTube Music, or the Music Assistant project.

## What it provides

- Search and browse for tracks, albums, artists, and playlists
- Direct playback without a YouTube Premium subscription
- Optional saved library, likes, subscriptions, playlists, and recommendations
  through a browser cookie
- Optional Last.fm recommendation folders resolved back to playable YouTube
  Music tracks
- PostgreSQL account mirroring for saved tracks, likes, albums, artists,
  subscriptions, playlists and membership, history, uploads, podcasts,
  channels, episodes, and account metadata
- Multiple Music Assistant provider instances for separate accounts
- Direct resolution of pasted YouTube and YouTube Music links
- Optional start/end trimming for individual videos
- Persistent local playback of atomically completed background downloads
- Standalone Docker images for the latest and beta Music Assistant channels

Podcast support is not implemented.

## How it works

| Component | Responsibility |
| --- | --- |
| `ytmusicapi` | Catalogue metadata and authenticated account features |
| `yt-dlp` | Format resolution and resumable background downloads |
| Music Assistant | Library management, queueing, decoding, normalization, and players |
| Last.fm API | Optional artist/track/tag/profile discovery seeds |
| Persistent cache | Serving atomically published background downloads locally |

An uncached foreground play starts immediately from YouTube and never writes a
cache file. Independently, the authenticated native background task uses yt-dlp
to download into persistent local staging, where interrupted transfers can
resume. A complete stage is copied, flushed, and atomically promoted on the
cache filesystem. Interrupted transfers and publication copies are never
accepted as cache hits.

Each background claim performs one authenticated extraction and passes that
already-resolved format directly to yt-dlp. The provider loads its configured
browser session into yt-dlp's in-memory cookie jar and reuses the Google account
selector for extraction and media requests; no plaintext cookie file is
created, and the downloader does not repeat an anonymous watch-page extraction.
Requests are paced, and a YouTube bot-verification response creates a durable
PostgreSQL cooldown before further queue claims.

Before any custom stream begins, the provider checks the cache again. This
matters because Music Assistant may preload stream details while the background
task is downloading. If the file has since been published, the preloaded request
reads it locally instead of opening another YouTube stream.

## Important limitations

- The provider depends on unofficial YouTube interfaces that can change without
  notice.
- Some tracks can be unavailable because of region, age, account, or rights
  restrictions.
- The highest-quality option means the best format yt-dlp can access for that
  request. Anonymous playback commonly resolves to Opus around 128–160 kbps; it
  does not imply lossless audio.
- The authenticated extractor can only select formats YouTube exposes to the
  configured browser session. A Premium cookie may expose a higher tier, but
  this integration does not guarantee a particular bitrate.
- YouTube Music does not provide a lossless source through this integration.
  Use a dedicated library manager and a lawful lossless source if archival
  quality is required.
- Trimmed tracks are intentionally not cached because playback stops before the
  upstream file is complete.

## Support boundary

Report provider problems in this repository's
[issue tracker](https://github.com/abhi1693/music-assistant-yt-music/issues).

Do not request support for this provider from Music Assistant's repositories,
Discord, or forum. When reporting an unrelated Music Assistant problem,
reproduce it with this custom provider removed first.

## Installation

### Standalone Docker or Docker Compose

Use this repository's image in place of the upstream Music Assistant server
image:

```yaml
services:
  music-assistant:
    image: ghcr.io/abhi1693/music-assistant-yt-music:latest
    container_name: music-assistant
    restart: unless-stopped
    volumes:
      - ./data:/data
      - ./ytmusic-cache:/data/ytmusic-cache
    # Keep your existing network, device, and port configuration.
```

Available rolling channels:

| Tag | Music Assistant channel |
| --- | --- |
| `latest` | Latest stable server |
| `beta` | Latest beta server |

Both tags publish `linux/amd64` and `linux/arm64` images. For reproducible
deployments, pin the image digest:

```yaml
image: ghcr.io/abhi1693/music-assistant-yt-music@sha256:...
```

The image is built through the shared
[`abhi1693/actions`](https://github.com/abhi1693/actions) Docker workflow.

### Home Assistant OS or Supervised

Music Assistant runs inside an add-on container. Copying the provider only to
`/config` does not load it; the provider must be injected into the container's
Python environment.

Run the installer from a shell with host Docker access. On Home Assistant OS,
use the Advanced SSH & Web Terminal community add-on with Protection mode
disabled:

```sh
curl -fsSL \
  https://raw.githubusercontent.com/abhi1693/music-assistant-yt-music/master/scripts/install_provider.sh \
  | sh -s -- --repo-owner abhi1693
```

Upgrade an existing installation with:

```sh
curl -fsSL \
  https://raw.githubusercontent.com/abhi1693/music-assistant-yt-music/master/scripts/install_provider.sh \
  | sh -s -- --repo-owner abhi1693 --force
```

The `sh -s --` separator is required. It passes the remaining arguments to the
downloaded script instead of treating them as shell options.

The installer:

1. Detects the Music Assistant container.
2. Detects its active Python version and configuration path.
3. Stages a persistent recovery copy under the Music Assistant
   custom-components path.
4. Copies it into the running container.
5. Restarts Music Assistant unless instructed otherwise.

Use `sh ... --help` or inspect
[`scripts/install_provider.sh`](scripts/install_provider.sh) for all supported
flags.

### Surviving Home Assistant container recreation

A normal container restart preserves injected files, but an add-on update or
Home Assistant recreation can replace the container. The optional MA Provider
Watcher local add-on reinjects the provider when that happens:

```sh
curl -fsSL \
  https://raw.githubusercontent.com/abhi1693/music-assistant-yt-music/master/scripts/install_watcher_addon.sh \
  | sh -s -- --repo-owner abhi1693
```

For installation paths, update behavior, and troubleshooting, see
[WATCHER_ADDON.md](WATCHER_ADDON.md).

### Manual container installation

Detect the Python directory and copy the provider into Music Assistant:

```sh
MA_CONTAINER=addon_d5369777_music_assistant
PYTHON_DIR=$(
  docker exec "$MA_CONTAINER" sh -c 'ls /app/venv/lib' |
    grep -m1 '^python3'
)

docker cp ./ytmusic \
  "$MA_CONTAINER:/app/venv/lib/$PYTHON_DIR/site-packages/music_assistant/providers/"
docker restart "$MA_CONTAINER"
```

Replace the container name if your installation uses a different one.

## Adding the provider

After installation:

1. Open Music Assistant.
2. Go to **Settings → Music sources → Add music source**.
3. Select **YouTube Music**.
4. Configure authentication, quality, and caching.
5. Save the provider.

The provider can be renamed to **YouTube Music** or any other local display
name without changing its internal `ytmusic` domain.

### Upgrading across the provider-domain rename

The current package directory and provider domain are both `ytmusic`. The
installers remove the retired package directory before copying this one so
Music Assistant cannot load two implementations.

Music Assistant may retain the previous provider configuration as an
unavailable source because its stored domain is different. Remove that
unavailable entry, add **YouTube Music**, and reuse the same cookie and cache
directory. Completed cache files are content-addressed and are discovered again
from disk; the account mirror and durable queue rebuild under the new provider
instance. The home-lab deployment in this repository's companion GitOps project
performs its PostgreSQL, SQLite, settings, and retained-path migration
automatically before Music Assistant starts.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| Authentication | None | Enables account-backed features when set to Browser cookie |
| Cookie header | Empty | Browser session used by `ytmusicapi` |
| Brand account ID | Empty | Selects a YouTube brand account |
| Account index | `0` | Selects an account from a multi-account Google session |
| Prefer highest audio quality | Enabled | Selects yt-dlp's highest-ranked accessible audio format |
| Enable persistent local cache | Enabled | Reuses completed background downloads; foreground playback never writes cache files |
| Cache directory | `/data/ytmusic-cache` | Writable persistent cache location |
| Cache staging directory | `/data/ytmusic-cache-staging` | Persistent local workspace for resumable yt-dlp downloads |
| PostgreSQL catalog DSN | Empty | Enables the account mirror plus durable queue, leases, retries, and cache metadata |
| Mirror complete account | Enabled | Persists supported account collections and ordered relationships when PostgreSQL is configured |
| Account mirror interval | 6 hours | Refresh cadence for the account snapshot |
| Prefetch library to cache | Disabled | Registers an app-native scheduled cache task |
| Include playlists in prefetch | Disabled | Adds authenticated library playlists to the prefetch scope |
| Prefetch interval | 6 hours | Recurring Music Assistant task schedule |
| Maximum tracks per run | 100 | Bounds download and quality-upgrade jobs processed by one task run |
| Parallel prefetch downloads | `1` | Simultaneous yt-dlp track downloads; configurable from 1 to 8 |
| Maximum cache size | 50 GB | Stops prefetch without evicting completed files; `0` disables the limit |
| Pause prefetch while players are active | Disabled | Optional bandwidth protection; downloads continue during playback by default |
| Delay between prefetch requests | 15 seconds | Protects the authenticated session from bulk-request bot challenges |
| Upgrade lower-quality cached files | Enabled | Replaces an old cache file only after a strictly better accessible format is complete |
| Cached quality target | 256 kbps | Checks files below this bitrate, including legacy entries with unknown bitrate |
| Quality recheck interval | 30 days | Prevents repeated YouTube probes when the current file is already the best accessible format |
| Last.fm API key | Empty | Enables optional Last.fm recommendation lookups |
| Last.fm username | Empty | Uses a public Last.fm profile's top tracks as similar-track seeds |
| Last.fm seed tracks | Empty | Semicolon- or newline-separated `Artist - Track` seeds |
| Last.fm seed artists | Empty | Artist names used to find similar artists and their top tracks |
| Last.fm tags | Empty | Tags whose top tracks should be resolved as discovery candidates |
| Last.fm maximum tracks | 25 | Bounds Last.fm tracks resolved per recommendation or prefetch pass |
| Fetch Last.fm recommendations to cache | Disabled | Adds resolved Last.fm tracks to the native cache prefetch task |

## Optional account authentication

Authentication is not required for basic search, browse, and playback. A
browser cookie enables:

- Saved songs, albums, playlists, and subscribed artists
- Likes and library editing
- Personalized recommendations
- Account-specific library synchronization

### Capturing a cookie

1. Open a new private/incognito browser window.
2. Sign in at [music.youtube.com](https://music.youtube.com).
3. Open browser developer tools and select the **Network** tab.
4. Reload the page.
5. Select a `music.youtube.com` or `youtubei/v1/...` request.
6. Copy the complete `Cookie` request-header value.
7. Paste it into the provider's **Cookie header** field.
8. Close the private window without explicitly signing out.

The cookie must include `__Secure-3PAPISID`. A complete capture should also
include `__Secure-1PSID`, `__Secure-3PSID`, and `SAPISID`; the provider warns
when those recommended values are absent because partial cookies can validate
initially and fail on later library calls. Treat the complete header like a
password. Never put it in an issue, log excerpt, image, or unencrypted
configuration file.

Music Assistant stores the configured secret. The provider builds authentication
headers in memory and does not keep a separate plaintext cookie file. Releases
that previously created `/data/ytmusic_browser_auth.json` are migrated by
removing that legacy file.

### Brand and multiple Google accounts

- For a brand account, configure its ID from
  [Google Brand Accounts](https://myaccount.google.com/brandaccounts) or the
  `X-Goog-PageId` request header.
- A browser signed into several Google accounts can send the same cookie for
  all of them. Configure **Account index** using the request's
  `X-Goog-AuthUser` value.
- The least ambiguous setup is one private browser session per Google account,
  each with account index `0`.

### Multiple provider instances

Music Assistant can run several instances of this provider. Each instance has
its own:

- Cookie and account selection
- Library synchronization state
- Quality preference
- Cache configuration
- Display name

Use separate cache directories if you need strict account-level isolation.

## Audio quality

Leave **Prefer highest audio quality** enabled unless a player cannot handle
Opus. The selector uses:

```text
bestaudio/best
```

Disabling the setting prefers an M4A audio stream for compatibility:

```text
bestaudio[ext=m4a]/bestaudio/best
```

Compatibility can substantially reduce quality when YouTube offers only a
low-bitrate AAC format. The provider caches the selected upstream bytes without
transcoding them. Music Assistant may decode that input to PCM, apply processing
such as volume normalization, and encode or transmit a player-compatible output;
that processing view does not mean the cached WebM file was converted to PCM.

With PostgreSQL and background prefetch enabled, the provider records the
selected bitrate. Cached tracks below the configurable target, and legacy rows
whose bitrate is unknown, are periodically checked against the best format the
current authenticated account can access. A replacement is published only when
its bitrate is strictly higher. The old completed file remains available during
the download and is removed only after the replacement has been atomically
published.

## Persistent stream cache

Local-cache lookup is enabled by default. Authenticated background prefetch must
also be enabled to populate new files. The cache directory must be writable and
backed by persistent storage if it should survive container replacement.

### Lifecycle

1. Foreground cache misses remain ordinary YouTube streams and never write
   cache files.
2. The native background task downloads to the persistent staging directory
   using yt-dlp's resumable downloader and bounded HTTP range requests.
3. yt-dlp retains an interrupted `<track-hash>.<extension>.part` in staging.
4. A completed stage is copied to a destination `.part` file, flushed, and
   atomically renamed within the cache filesystem.
5. PostgreSQL cache rows are reconciled with the entire provider cache directory;
   a row whose completed file vanished is cleared and requeued automatically.
6. Eligible lower-quality files are replaced atomically after a better download
   completes.
7. Later plays return the completed local-file stream.
8. Preloaded requests recheck disk before opening YouTube.

The filename is a SHA-256 hash of the YouTube video ID. The extension reflects
the selected upstream container, commonly `.webm` for Opus.

Completed cache files:

- Survive Music Assistant and container restarts when the directory is
  persistent
- Are never automatically evicted
- Are replaced only by the explicit, configurable quality-upgrade workflow
- Restore seeking on subsequent plays

Music Assistant can continue playing after an active cache file is deleted
because its decoder may already hold an open file descriptor and a decoded audio
buffer. Cached local stream details expire immediately, so once that active
buffer is no longer reusable, Music Assistant asks the provider to check disk
again. At the start of the next background run, PostgreSQL rows marked `cached`
are compared with disk; a missing file becomes a pending priority job and is
downloaded again without resetting the database.

Partial files:

- Are never considered cache hits
- In the staging directory, survive provider and container restarts so yt-dlp
  can resume them
- In the cache directory, are incomplete publication copies and are removed
  after a failed publication attempt

Cache-storage failures are logged but do not block playback.

### Storage example

```yaml
services:
  music-assistant:
    volumes:
      - /mnt/media/music/YouTube Music:/data/ytmusic-cache
      - music-assistant-data:/data/ytmusic-cache-staging
```

For NFS, ensure the Music Assistant container's UID/GID can create, flush,
rename, and read files in the target directory.

## Native background prefetch

Installations can download uncached tracks before they are played. Enable
**Prefetch library to cache** for authenticated YouTube Music library tracks,
or enable **Fetch Last.fm recommendations to cache** with a Last.fm API key and
at least one Last.fm source.

The provider registers a recurring task through Music Assistant's native
Background Tasks controller. It does not require a sidecar, Kubernetes job,
cron process, or separate queue service. The task appears in Music Assistant
with its schedule, progress, retained logs, manual **Run now**, cancellation,
and retry controls.

Each run:

1. Enumerates enabled sources: authenticated library tracks, optional library
   playlists, and/or resolved Last.fm recommendations.
2. Reconciles all PostgreSQL cache hits with completed files on disk and
   requeues stale rows.
3. Schedules cooled-down quality checks for cached files below the configured
   bitrate target.
4. Claims each PostgreSQL job individually, leasing no more than the configured
   parallel-download limit before starting a batch, so newly requested misses
   can still move ahead of untouched bulk-library work.
5. Downloads or upgrades at most the configured number of tracks, using the
   configured parallel-download limit.
6. Staggers starts inside each concurrent batch by the configured request delay
   and uses yt-dlp's resumable, chunked downloader, the provider's audio-quality
   selector, local staging, and atomic cache publication.
7. Stops at the configured cache-size ceiling without deleting existing files.
8. Pauses when foreground playback is active when that protection is enabled.

Parallel downloads default to `1` and are capped at `8`. A value above `1`
creates separate yt-dlp track downloads; it does not split one audio file into
several independent cache entries. Higher values increase bandwidth and
YouTube request pressure. Because already-started jobs finish atomically, a
concurrent batch can exceed the cache-size ceiling by at most the other
in-flight files in that batch.

Without a catalog DSN, completed cache files remain the durable task state and
the next execution rescans the library. With a PostgreSQL DSN, the provider
also persists pending, downloading, retry, failed, and cached states. Workers
claim jobs using `FOR UPDATE SKIP LOCKED` and a 15-minute lease. Failures use
bounded exponential backoff and become terminal after ten attempts. Cache rows
also store bitrate, upgrade intent, and the last quality-check time so an
unavailable higher format is not probed on every run.
An uncached track requested for playback is inserted at priority zero; ordinary
library and Last.fm inventory use priority 100. Playback still uses its normal
remote stream immediately and does not wait for the background download.

The PostgreSQL integration is fail-open: connection or query failures disable
durable coordination for that run but do not prevent normal remote playback or
local-file cache hits. Use a dedicated database and role. The provider creates
and migrates its `ytmusic_cache_*` queue/cache tables and
`ytmusic_account_*` mirror tables.

Prefetch requires a Music Assistant release that exposes the native Background
Tasks controller. Older releases continue to support normal playback and
local playback of already completed cache files, but log a warning and do not
register the scheduled task.

## Supported features

| Feature | Anonymous | Browser cookie |
| --- | :---: | :---: |
| Search and browse | Yes | Yes |
| Track, album, artist, and playlist playback | Yes | Yes |
| Pasted YouTube links | Yes | Yes |
| Video trimming | Yes | Yes |
| Play completed local cache files | Yes | Yes |
| Populate cache with native library prefetch | No | Yes |
| Artist albums and top tracks | Yes | Yes |
| Similar tracks and song radio | Yes | Yes |
| Multiple provider instances | Yes | Yes |
| Saved library synchronization | No | Yes |
| Personalized recommendations | No | Yes |
| Library editing | No | Yes |
| Podcasts | No | No |

## Pasting YouTube links

Paste a supported URL directly into Music Assistant's search field:

- `https://music.youtube.com/watch?v=VIDEO_ID`
- `https://www.youtube.com/watch?v=VIDEO_ID`
- `https://youtu.be/VIDEO_ID`
- `https://music.youtube.com/playlist?list=PLAYLIST_ID`
- `https://www.youtube.com/playlist?list=PLAYLIST_ID`

A watch URL containing `list=` resolves the individual video rather than the
surrounding playlist. Plain YouTube videos can have less complete music metadata
than catalogue tracks.

### Trimming a video

Append an `@start-end` range:

```text
https://youtu.be/VIDEO_ID @0:15-3:42
https://youtu.be/VIDEO_ID @15-222
https://youtu.be/VIDEO_ID @1m30s-
https://youtu.be/VIDEO_ID @-3:42
```

Timestamps accept seconds, `MM:SS`, `HH:MM:SS`, or unit notation such as
`1m30s`. The range remains part of the saved provider item. Trimmed items use a
direct stream and are not cached.

## Troubleshooting

### The provider is missing

- Confirm the directory is named `ytmusic`.
- Confirm it contains `__init__.py` and `manifest.json`.
- Confirm it is inside Music Assistant's active Python `site-packages`, not
  merely staged under `/config`.
- Inspect Music Assistant startup logs for provider import errors.

### A track cannot play

- Check for `UnplayableMediaError` and yt-dlp errors in Music Assistant logs.
- Confirm the track plays in YouTube from the same region and account.
- Update to the latest provider release and yt-dlp dependency.
- Some restricted or removed tracks cannot be made playable by this provider.

### A `.part` appears beside a completed file

- Foreground playback never writes the cache. A destination `.part` is created
  only while the background task copies a completed local stage to the cache
  filesystem, including during a quality upgrade.
- The prior completed file remains playable until the copy is flushed and
  atomically renamed. The `.part` should then disappear.
- If it remains after the background task ends, inspect the prefetch task and
  Music Assistant logs for an interrupted pod, NFS write, flush, or rename
  failure. Confirm only one Music Assistant deployment writes to the directory.

### Confirming that playback uses the persistent cache

- Look for `Playback source: Local cache` in Music Assistant logs.
- Cached stream details carry `playback_source=local_cache`,
  `playback_source_label=Local cache`, and `cache_hit=true`. Remote first-play
  details carry the corresponding `youtube`, `YouTube Music`, and `false`
  values. This stable provenance contract is intended for diagnostics and for
  Music Assistant UI support when it exposes provider-defined stream data.
- A cache hit does not create or grow a `.part` file.
- The Music Assistant signal-path panel does not expose provider-defined
  transport provenance in the current release; use the explicit log until that
  UI supports it.
- `Quality unknown` can appear for a local cache hit because the cached
  `LOCAL_FILE` response does not currently persist the original bitrate as
  metadata; it is not proof of an upstream request.

### A staging `.part` survives a restart

- This is expected for an interrupted yt-dlp transfer under the configured
  staging directory; the next background run resumes it.
- Destination `.part` files in the cache directory are publication copies, are
  never cache hits, and are removed after a handled publication failure.
- Completed cache files never use the `.part` suffix.

### Audio is lower quality than expected

- Keep **Prefer highest audio quality** enabled.
- Inspect the input codec and bitrate in Music Assistant's stream details.
- Remember that “best” is limited to formats YouTube exposes to the extractor.
- With PostgreSQL prefetch enabled, keep **Upgrade lower-quality cached files**
  enabled and set **Cached quality target** to the floor you want.
- An old or unknown-quality file is checked at most once per configured recheck
  interval unless it is deleted and requeued.

### Authentication fails

- Recapture the entire cookie from a fresh private session.
- Confirm the recommended cookie values are present.
- Do not sign out after capture.
- Verify the brand account ID and account index.

### The authenticated library is empty or duplicated

- Confirm the content is explicitly saved, liked, or subscribed on the selected
  account.
- Verify that each provider instance points to the intended account.
- For multiple signed-in Google accounts, use separate private sessions or the
  correct `X-Goog-AuthUser` account index.

## Dependencies

The provider installs these packages through Music Assistant:

- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi)
- [`asyncpg`](https://magicstack.github.io/asyncpg/)

First startup requires outbound package access and a writable Python
environment. Container deployments may prefer the prebuilt image or a
declaratively prepared persistent virtual environment.

## Development and validation

Run the Python suite:

```sh
uv run --with-requirements tests/python/requirements.txt \
  python -m pytest tests/python -q
```

Run the installer suites:

```sh
for test_script in tests/test_*.sh; do
  sh "$test_script"
done
```

Pull requests should preserve anonymous playback, authenticated multi-instance
isolation, incomplete-download safety, and persistent-cache compatibility.

## Security, terms, and user responsibility

- This project does not host or distribute media.
- Cached files remain on storage controlled by the user.
- Users are responsible for complying with applicable laws, licenses, account
  terms, and YouTube's Terms of Service.
- Browser cookies are credentials and must be protected accordingly.
- Last.fm API keys should be treated as application credentials and kept out of
  logs, screenshots, and public configuration.
- The software is provided without warranty under the repository's license.
- Google or YouTube can change or block the unofficial interfaces at any time.

## License

Licensed under the [MIT License](LICENSE).

## Credits and project lineage

This repository is a fork of
[`sproft/music-assistant-ytmusic`](https://github.com/sproft/music-assistant-ytmusic).
The original provider was created and maintained by
[@sproft](https://github.com/sproft). Their work established the provider,
authentication flow, installers, and community around the project.

Notable upstream contributors include:

- [@mawoka-myblock](https://github.com/mawoka-myblock) — bitrate-based format
  selection and modern yt-dlp audio extraction
- [@jojo141185](https://github.com/jojo141185) — automated container image
  builds
- [@bygadd](https://github.com/bygadd) — watcher auto-update support
- [@gusjengis](https://github.com/gusjengis) — pasted-link resolution and video
  trimming
- [@bsny](https://github.com/bsny) — artist parsing and fork-aware installers

This fork is maintained by [@abhi1693](https://github.com/abhi1693), with
persistent background caching, home-lab deployment support, and subsequent
fixes contributed with the help of the wider open-source community.
