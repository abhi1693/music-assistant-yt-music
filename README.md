# YouTube Music for Music Assistant

An experimental Music Assistant music provider for YouTube and YouTube Music.
It supports anonymous catalogue playback, optional account-backed library
features, and a persistent read-through audio cache for bandwidth-constrained
installations.

This is an unofficial community integration. It is not affiliated with Google,
YouTube, YouTube Music, or the Music Assistant project.

## What it provides

- Search and browse for tracks, albums, artists, and playlists
- Direct playback without a YouTube Premium subscription
- Optional saved library, likes, subscriptions, playlists, and recommendations
  through a browser cookie
- Multiple Music Assistant provider instances for separate accounts
- Direct resolution of pasted YouTube and YouTube Music links
- Optional start/end trimming for individual videos
- Persistent, read-through caching of completed streams
- Standalone Docker images for the latest and beta Music Assistant channels

Podcast support is not implemented.

## How it works

| Component | Responsibility |
| --- | --- |
| `ytmusicapi` | Catalogue metadata and authenticated account features |
| `yt-dlp` | Resolving playable audio formats and playlist fallbacks |
| Music Assistant | Library management, queueing, decoding, normalization, and players |
| Read-through cache | Retaining the original completed audio response for later plays |

For an uncached track, the provider opens one upstream response. The same bytes
are sent to Music Assistant and written to a temporary `.part` file. A complete
download is flushed and atomically promoted to its final cache filename.
Interrupted or failed transfers are not accepted as cache hits.

Before any custom stream begins, the provider checks the cache again. This
matters because Music Assistant may preload stream details while an earlier
request is still downloading. If that earlier request has since completed, the
preloaded request reads the new local file instead of downloading the track
again.

## Important limitations

- The provider depends on unofficial YouTube interfaces that can change without
  notice.
- Some tracks can be unavailable because of region, age, account, or rights
  restrictions.
- The highest-quality option means the best format yt-dlp can access for that
  request. Anonymous playback commonly resolves to Opus around 128–160 kbps; it
  does not imply lossless audio.
- The current playback extractor does not use the account cookie supplied for
  library synchronization, so YouTube Music Premium's 256 kbps tier is not
  guaranteed.
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
  https://raw.githubusercontent.com/abhi1693/music-assistant-yt-music/main/scripts/install_provider.sh \
  | sh -s -- --repo-owner abhi1693
```

Upgrade an existing installation with:

```sh
curl -fsSL \
  https://raw.githubusercontent.com/abhi1693/music-assistant-yt-music/main/scripts/install_provider.sh \
  | sh -s -- --repo-owner abhi1693 --force
```

The `sh -s --` separator is required. It passes the remaining arguments to the
downloaded script instead of treating them as shell options.

The installer:

1. Detects the Music Assistant container.
2. Detects its active Python version and configuration path.
3. Stages the provider under the Music Assistant custom-components path.
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
  https://raw.githubusercontent.com/abhi1693/music-assistant-yt-music/main/scripts/install_watcher_addon.sh \
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

docker cp ./ytmusic_free \
  "$MA_CONTAINER:/app/venv/lib/$PYTHON_DIR/site-packages/music_assistant/providers/"
docker restart "$MA_CONTAINER"
```

Replace the container name if your installation uses a different one.

## Adding the provider

After installation:

1. Open Music Assistant.
2. Go to **Settings → Music sources → Add music source**.
3. Select **YouTube Music (Free)**.
4. Configure authentication, quality, and caching.
5. Save the provider.

The provider can be renamed to **YouTube Music** or any other local display
name without changing its internal `ytmusic_free` domain.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| Authentication | None | Enables account-backed features when set to Browser cookie |
| Cookie header | Empty | Browser session used by `ytmusicapi` |
| Brand account ID | Empty | Selects a YouTube brand account |
| Account index | `0` | Selects an account from a multi-account Google session |
| Prefer highest audio quality | Enabled | Selects yt-dlp's highest-ranked accessible audio format |
| Cache streamed tracks | Enabled | Saves complete, untrimmed first plays |
| Cache directory | `/data/ytmusic-cache` | Writable persistent cache location |

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

The cookie should include `__Secure-3PAPISID`, `SID`, `HSID`, and `SSID`.
Treat it like a password. Never put it in an issue, log excerpt, image, or
unencrypted configuration file.

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

Existing cached tracks keep the quality they had when first downloaded. Changing
the quality option does not silently delete or replace them.

## Persistent stream cache

Caching is enabled by default. The cache directory must be writable and backed
by persistent storage if it should survive container replacement.

### Lifecycle

1. An uncached stream writes to `<track-hash>.<extension>.part`.
2. The same bytes are forwarded to Music Assistant during playback.
3. At completion, the provider flushes and atomically renames the file.
4. Later plays return a local-file stream.
5. Preloaded requests recheck disk before opening YouTube.

The filename is a SHA-256 hash of the YouTube video ID. The extension reflects
the selected upstream container, commonly `.webm` for Opus.

Completed cache files:

- Survive Music Assistant and container restarts when the directory is
  persistent
- Are never automatically evicted
- Are never silently replaced because the quality preference changed
- Restore seeking on subsequent plays

Partial files:

- Are never considered cache hits
- Are removed after an interrupted or failed managed stream
- Can remain after an ungraceful process or host failure and may be safely
  removed when no stream is active

Cache-storage failures are logged but do not block playback.

### Storage example

```yaml
services:
  music-assistant:
    volumes:
      - /mnt/media/music/YouTube Music:/data/ytmusic-cache
```

For NFS, ensure the Music Assistant container's UID/GID can create, flush,
rename, and read files in the target directory.

## Supported features

| Feature | Anonymous | Browser cookie |
| --- | :---: | :---: |
| Search and browse | Yes | Yes |
| Track, album, artist, and playlist playback | Yes | Yes |
| Pasted YouTube links | Yes | Yes |
| Video trimming | Yes | Yes |
| Persistent stream cache | Yes | Yes |
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

- Confirm the directory is named `ytmusic_free`.
- Confirm it contains `__init__.py` and `manifest.json`.
- Confirm it is inside Music Assistant's active Python `site-packages`, not
  merely staged under `/config`.
- Inspect Music Assistant startup logs for provider import errors.

### A track cannot play

- Check for `UnplayableMediaError` and yt-dlp errors in Music Assistant logs.
- Confirm the track plays in YouTube from the same region and account.
- Update to the latest provider release and yt-dlp dependency.
- Some restricted or removed tracks cannot be made playable by this provider.

### Playback creates another `.part` beside a completed file

- Upgrade to `v0.2.2` or later. Earlier releases could trust preloaded stream
  details and begin another download after the first request had already
  published the cache file.
- Confirm the completed and partial filenames have the same hash and extension.
- Confirm only one Music Assistant deployment writes to that cache directory.

### A `.part` remains after playback

- A growing file indicates an active writer.
- A zero-byte or stale file can be left by a hard process or host failure.
- Do not remove a growing `.part` while playback is active.
- Completed files never use the `.part` suffix.

### Audio is lower quality than expected

- Keep **Prefer highest audio quality** enabled.
- Inspect the input codec and bitrate in Music Assistant's stream details.
- Remember that “best” is limited to formats YouTube exposes to the extractor.
- Existing cached files are reused; changing the preference does not redownload
  them.

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

### `ytmusic://` items are skipped

The `ytmusic://` scheme belongs to Music Assistant's official premium provider.
This provider uses `ytmusic_free://`. Third-party applications must emit the
correct provider scheme. Rewriting is safe only for raw `track/<video-id>`
links; other media types use different identifier namespaces.

## Dependencies

The provider installs these packages through Music Assistant:

- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi)

First startup requires outbound package access and a writable Python
environment. Container deployments may prefer the prebuilt image or a
declaratively prepared persistent virtual environment.

## Development and validation

Run the Python suite:

```sh
uv run --with pytest --with 'yt-dlp>=2024.1.0' \
  python -m pytest -q
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
persistent read-through caching, home-lab deployment support, and subsequent
fixes contributed with the help of the wider open-source community.
