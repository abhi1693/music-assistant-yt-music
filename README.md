# YouTube Music (Free): Music Assistant Provider

A custom Music Assistant provider that streams YouTube Music **without a premium
subscription** and keeps completed first plays on persistent local storage.

## How it works

| Component | Role |
|-----------|------|
| `ytmusicapi` | Search, metadata, and library sync (optional auth) |
| `yt-dlp` | Extract direct audio stream URLs and playlist tracks |
| Read-through cache | Tee the first complete play to disk and serve later plays locally |

Audio stream URLs are resolved without a login. Which of YouTube's internal clients does that resolving is left to yt-dlp's own defaults rather than pinned here, because the client that works anonymously keeps moving: the Android Music client this provider originally named has since been removed from yt-dlp entirely, and the Android and iOS clients now require a PO token, which an anonymous session cannot supply. yt-dlp tracks that target for us. This is the same general approach used by NewPipe and SimpMusic on Android.

For playlists, `yt-dlp` is used as a fallback when `ytmusicapi` cannot parse the unauthenticated playlist response from YouTube, ensuring playlists work without a login.

For an untrimmed track that is not cached, the provider uses Music Assistant's
custom stream interface to make one upstream request. Each byte is sent to the
player and written to a temporary file. The file becomes visible to subsequent
plays only after the upstream stream reaches EOF and is flushed to disk. If
playback stops or the request fails, the partial file is removed. This avoids
doubling bandwidth on the first play and avoids serving corrupt partial media.

> **Note:** This uses YouTube's internal (unofficial) API. It may break if Google changes their API. Premium-exclusive content (offline, high-res audio) is not accessible.

---

## Support and bug reports

This is an unofficial, independent provider. It is not affiliated with or supported by the Music Assistant project.

The Music Assistant maintainers have stated more than once that they do not want this provider and will not support it. Please respect that:

- Do not open issues, discussions, or support requests about this provider on the Music Assistant repositories, Discord, or forum.
- Do not mention this provider when reporting an unrelated Music Assistant bug. If you hit a problem in Music Assistant itself, reproduce it with this provider removed before reporting it upstream.
- Report anything about this provider here, on this repository's [issue tracker](https://github.com/abhi1693/music-assistant-yt-music/issues).

Keeping these reports here respects the Music Assistant team's wishes and keeps them out of a project they have asked not to be involved with.

---

## Installation: Standalone Docker Compose

If you are running Music Assistant via standalone Docker Compose instead of the Home Assistant OS Add-on, you can use our pre-built custom image which comes with the `ytmusic_free` provider pre-installed.

Replace the default upstream image (`ghcr.io/music-assistant/server:latest`) with this image in your `docker-compose.yml`:

```yaml
services:
  music-assistant:
    image: ghcr.io/sproft/music-assistant-ytmusic:latest
    container_name: music-assistant
    restart: unless-stopped
    # ... keep your existing volumes, network, devices, and ports settings here
```

> [!NOTE]
> The `:latest` and `:beta` tags are rolling builds synced with the upstream Music Assistant releases.
>
> For reproducible deployments, pin to a specific build. The `:latest-<run_id>` / `:beta-<run_id>` tags identify a single build and are stable in normal use, but a manual workflow re-run reuses the run id and can republish different bits under the same tag. For a guaranteed-immutable pin, use the `@sha256:` digest.

## Installation: Home Assistant OS

Music Assistant runs as a Docker container (HA add-on). The provider files must be copied **inside the container**. Placing them in `/config/` is not sufficient.

### Quick install (recommended)

One-line install from a shell with **host Docker access**. On Home Assistant OS that means the **Advanced SSH & Web Terminal** community add-on with **Protection mode off** (the official Terminal & SSH add-on is sandboxed and cannot reach Docker, so the script aborts with a `the 'docker' command was not found` error explaining how to proceed). On a Supervised install, a normal root SSH session works:

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/music-assistant-ytmusic/main/scripts/install_provider.sh | sh
```

The script auto-detects your MA container ID, Python version, and `/config` path, downloads the latest provider, stages it under `/config/custom_components/mass/providers/`, copies it into the MA container, and restarts MA.

To upgrade later, re-run with `--force` so the already-staged provider is overwritten without stalling on the interactive prompt (a `curl | sh` pipe has no terminal to answer it):

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/music-assistant-ytmusic/main/scripts/install_provider.sh | sh -s -- --force
```

Note the `sh -s --` separator, as with the watcher installer below; `curl ... | sh --force` makes the shell parse `--force` as its own option and fail.

> **Installing from a fork?** Both install scripts accept `--repo-owner OWNER` to download from your own fork instead of the default `sproft`. For example: `curl -fsSL .../install_provider.sh | sh -s -- --repo-owner youruser`.

> **No Docker in your shell?** On Home Assistant OS the watcher add-on route does not need Docker in your terminal: run `install_watcher_addon.sh` (see below), then install and start the **MA Provider Watcher** local add-on with Protection mode off. It injects the provider for you and keeps it installed across restarts.

Then jump to step 4 below to add the provider in the MA UI, and see [WATCHER_ADDON.md](WATCHER_ADDON.md) (or the quick installer further down) to make the install survive HA restarts.

### Manual install

### 1. Find your MA container name

In an HAOS / Supervised setup the container is typically named:
```
addon_d5369777_music_assistant
```
Confirm it with:
```bash
docker ps | grep music
```

### 2. Copy the provider into the container

The provider lives in MA's Python `site-packages`, and the Python version moves over time (recent Music Assistant builds use `python3.14`, older ones `python3.13`). Detect it instead of hard-coding the path:

```bash
PYVER=$(docker exec addon_d5369777_music_assistant sh -c 'ls /app/venv/lib' | grep -m1 '^python3')
docker cp /path/to/ytmusic_free \
  "addon_d5369777_music_assistant:/app/venv/lib/$PYVER/site-packages/music_assistant/providers/"
```

Replace `/path/to/ytmusic_free` with wherever you placed the folder (e.g. `/config/custom_components/mass/providers/ytmusic_free`). The one-line `install_provider.sh` runs this detection for you, so prefer it unless you are debugging.

### 3. Restart Music Assistant

```bash
docker restart addon_d5369777_music_assistant
```

> **Important:** Restarting MA from the Home Assistant UI recreates the container from its image, wiping any files you copied in. Always use `docker restart` to preserve the provider files.

### 4. Add the provider in MA

Go to **Settings → Apps → Add** in the MA UI. You should see **"YouTube Music (Free)"** listed. No credentials are required for basic playback.

### Adding more than one account

The provider is multi-instance, so you can add it several times and give each
entry its own cookie, brand account ID, audio quality setting, and cache path.
Repeat the step above once per account. Music Assistant labels the entries
automatically once there is more than one, using the brand account ID where you
set one, and you can rename any of them in its settings.

Typical reasons to run more than one:

| Setup | What each instance holds |
|-------|--------------------------|
| Personal plus brand account | The same cookie in both, with the **Brand account ID** field set on one of them |
| Two people in one household | A separate cookie per Google account, captured in separate incognito windows |
| Authenticated plus anonymous | One instance with your library, one without auth |

Each instance authenticates on its own and syncs only the account its own cookie resolves to. Music Assistant then merges what they sync into its single library, tagging every item with the instance it came from.

> **Capturing two cookies?** Use a separate incognito window per account. A browser signed in to several Google accounts at once sends one identical cookie for all of them, and the **Account index** field (the `X-Goog-AuthUser` header value) is then the only thing that tells them apart. Two separate incognito sessions avoid the problem entirely, since each has a single account at index 0.

> **Upgrading from an earlier version?** Your existing entry keeps working and needs no changes. On first start after the upgrade, the provider deletes the old `/data/ytmusic_browser_auth.json` file that previous releases used to store your cookie in plaintext.

### Keeping the provider across HA restarts

If you restart HA (not just MA), the container is recreated and the provider files are lost. The recommended fix is the **MA Provider Watcher** local add-on, which re-copies the provider whenever the MA container is recreated. One-line install from a host shell:

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/music-assistant-ytmusic/main/scripts/install_watcher_addon.sh | sh
```

To re-install or upgrade an existing watcher add-on without the overwrite prompt, pass `--force` through to the script with `sh -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/music-assistant-ytmusic/main/scripts/install_watcher_addon.sh | sh -s -- --force
```

> Note the `sh -s --` separator. Writing `... | sh --force` makes the shell parse `--force` as one of its own options and fail with `sh: bad option '--force'`.

The installer auto-detects the local add-ons folder across the common layouts: the SSH/Samba add-on mapping (`/addons`), Home Assistant OS (`/mnt/data/supervisor/apps/local`, or legacy `.../addons/local`), and Supervised hosts. On **HAOS 18+** the Supervisor renamed the `addons` tree to `apps`, so the folder is `apps/local`. Older guides pointing at `addons/local` are out of date.

> **`could not find local add-ons directory`?** Pass the folder explicitly. From the HAOS host console:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/sproft/music-assistant-ytmusic/main/scripts/install_watcher_addon.sh | sh -s -- --force --addons-dir /mnt/data/supervisor/apps/local
> ```
> Inside the SSH/Samba add-on use `--addons-dir /addons`. After re-running, **Rebuild** the add-on (three-dot menu) so the new files are baked into its image, then **Start** it.

The watcher can also **keep the provider up to date automatically**. Enable the `auto_update` option in the add-on's Configuration tab (opt-in, off by default) and it periodically checks GitHub and reinstalls the provider only when the code actually changed, so you don't have to re-run the installer on every upstream change. Turning it back off pins to the version baked into the add-on image. See [Auto-update](WATCHER_ADDON.md#auto-update) for the options and details.

See **[WATCHER_ADDON.md](WATCHER_ADDON.md)** for the manual procedure, troubleshooting, and the available installer flags.

> **If the automatic installer doesn't work on your system,** the [`v0.1.0-beta.1` pre-release](https://github.com/sproft/music-assistant-ytmusic/releases/tag/v0.1.0-beta.1) is a known-good checkpoint of the manual install path. Pin to it (the manual procedure in `WATCHER_ADDON.md` from that tag was the only documented option at the time and works on HAOS and Supervised installs) and please [open an issue](https://github.com/sproft/music-assistant-ytmusic/issues/new) so the installer can be fixed.

---

## Authentication (optional)

Authentication is **not required** for search, browse, and playback. However, adding a browser cookie unlocks:

- Library sync (liked songs, saved albums, playlists, subscribed artists)
- Personalized recommendations (home feed)
- Library editing (add/remove items)

### Setup

1. In the MA UI, go to **Settings → Music sources** and open the **YouTube Music (Free)** entry you want to authenticate (if you added several, each one holds its own cookie)
2. Set **Authentication** to **Browser cookie**
3. Get your cookie (do this in a fresh **incognito / private window**, see the tip below):
   - Open a new incognito/private window and log in to `music.youtube.com`
   - Open DevTools (F12) → **Network** tab → reload the page
   - Click the first document request → under **Request Headers** find the `Cookie:` header → copy the full value
   - **Do not log out.** Just close the incognito window when you are done. Logging out invalidates the cookie.

> **Tip: use a dedicated incognito session.** Logging in through a new incognito/private window is the easiest way to grab a clean cookie. The session is isolated, so the `Cookie:` header carries only what YouTube Music needs and is shorter and easier to copy. More importantly, that session stays valid for as long as you never click **log out**: closing the window keeps the cookie alive (good for ~2 years). In your everyday browser, an accidental sign-out or Google rotating the session can invalidate the cookie later and silently break library sync.

4. Paste the cookie into the **Cookie header** field
5. **Brand accounts:** If your YouTube Music library is on a brand account, enter your brand account ID in the **Brand account ID** field. Find it at [myaccount.google.com/brandaccounts](https://myaccount.google.com/brandaccounts) or check the `X-Goog-PageId` header in DevTools. After logging into your Google account and selecting the correct Brand account you will find it here: ```https://myaccount.google.com/brandaccounts/THISISYOURIDRIGHTHERE/view```.
6. Click **Save**

The cookie must contain `__Secure-3PAPISID`, `SID`, `HSID`, and `SSID`. Cookies are valid for approximately 2 years unless you log out.

Your cookie is stored by Music Assistant itself, in the encrypted provider config it keeps for every provider, so it survives restarts and provider updates. You never need to re-enter it after an upgrade. The provider keeps the auth headers in memory for as long as the instance runs and writes no copy of its own; earlier releases also dropped a plaintext copy at `/data/ytmusic_browser_auth.json`, which is now removed automatically.

---

## Supported features

| Feature | Without auth | With auth |
|---------|:---:|:---:|
| Search (tracks, albums, artists, playlists) | ✅ | ✅ |
| Add by pasting a YouTube / YTM link | ✅ | ✅ |
| Trim a video with `@start-end` timestamps | ✅ | ✅ |
| Stream audio | ✅ | ✅ |
| Cache completed first plays locally | ✅ | ✅ |
| Artist top tracks / albums | ✅ | ✅ |
| Similar tracks (song radio) | ✅ | ✅ |
| Album / playlist tracks | ✅ | ✅ |
| Library sync (songs, albums, playlists) | ❌ | ✅ |
| Library artists (subscriptions + liked) | ❌ | ✅ |
| Personalized recommendations | ❌ | ✅ |
| Library editing (add/remove) | ❌ | ✅ |
| Multiple accounts side by side | ✅ | ✅ |
| Podcast support | ❌ | ❌ |

## Persistent stream cache

Caching is enabled by default. Configure these fields on the provider:

- **Cache streamed tracks** controls read-through caching.
- **Cache directory** defaults to `/data/ytmusic-cache`. Mount this path on
  persistent storage if `/data` is not already persistent in your deployment.

The first play is forward-only while one upstream response is being copied.
After it completes, Music Assistant receives a `LOCAL_FILE` stream and normal
seeking is restored. Trimmed `@start-end` items keep using the direct HTTP path
and are not cached because the player intentionally stops before the upstream
file is complete.

Cache names are SHA-256 hashes of YouTube video IDs. Temporary files end in
`.part`; they are never treated as cache hits. If Music Assistant closes the
stream immediately after its final chunk, the provider verifies the exact
downloaded byte count before atomically promoting that file into the cache.
Completed files are not cleared when Music Assistant or its container restarts.
Cache eviction is intentionally operator-managed so the provider never silently
deletes media from limited-bandwidth installations.

### Adding an arbitrary YouTube link

Music Assistant's global search normally only surfaces YouTube **Music** catalog
content. To add any specific YouTube or YouTube Music item, including plain
`youtube.com` videos that aren't in the Music catalog, **paste its URL directly
into the search box**. The provider detects the link and resolves it to the exact
item, placed first in the results, that you can then play or add to your library.

Recognized link formats:

- Songs / videos: `https://music.youtube.com/watch?v=…`, `https://www.youtube.com/watch?v=…`, `https://youtu.be/…`
- Playlists: `https://music.youtube.com/playlist?list=…`, `https://www.youtube.com/playlist?list=…`

Notes:

- A watch link that also carries a `list=` parameter resolves to the **song**, not the surrounding playlist.
- A pasted link bypasses any media-type filter, so a deliberate paste always resolves.
- Plain (non-Music) videos still play; their metadata (title, uploader) may be sparse.
- Albums are intentionally left to normal search, since YouTube albums already carry the metadata needed to surface there.

#### Related results use the video's name

When you paste a **track** link, the remaining results aren't matches on the raw
URL string. The provider looks up the video's title and runs a normal search on
that name, so the other results are related songs, albums and artists, while the
pasted video itself stays at the top.

#### Trimming a video (start / end timestamps)

Some great finds have an unrelated intro or an end-card with extra audio. Append a
`@start-end` trim spec to the link to play only part of the video:

```
https://youtu.be/VIDEOID @0:15-3:42      # play from 0:15 to 3:42
https://youtu.be/VIDEOID @15-222          # same, in plain seconds
https://youtu.be/VIDEOID @1m30s-          # from 1:30 to the natural end
https://youtu.be/VIDEOID @-3:42           # from the start to 3:42
```

Timestamps accept plain seconds (`15`), clock form (`3:42`, `1:02:03`) or unit
form (`1m30s`, `2h`, `90s`). The trim is encoded into the track, so it **persists**
when you save the song to your library or a playlist and replays trimmed every
time. The trimmed length is reflected in the duration/progress bar, and a `[start–end]`
label is shown so trimmed entries are easy to spot. (The spec is ignored for
playlist links.)

---

## Troubleshooting

**Provider doesn't appear in MA**
- Confirm the folder is named exactly `ytmusic_free` and contains both `__init__.py` and `manifest.json`.
- Verify the files are inside the container, not just in `/config/`.
- Check MA logs for import errors during startup.

**Track fails to play / `UnplayableMediaError`**
- yt-dlp may need updating: run `pip install -U yt-dlp` inside the MA container.
- Some tracks are region-locked or removed and cannot be streamed.

**Tracks skipped with `ytmusic is not available`**
- Some third-party tools (for example [Beatify](https://github.com/mholzi/beatify)) hand Music Assistant links in the form `ytmusic://track/<id>`. Music Assistant routes a media link by its scheme prefix, and `ytmusic://` belongs to the official premium YouTube Music provider. When that provider is not installed, the queue drops the item and logs `Skipping ytmusic://track/<id>: ytmusic is not available`.
- Rewrite the prefix to this provider's scheme, `ytmusic_free://track/<id>`, and the link resolves here. A track id is the raw YouTube video id and is identical on both providers, so the swap points at the same song. This is safe for `track/` links only. Album, artist, and playlist ids live in separate namespaces and will not map across.
- A provider cannot claim another provider's scheme, since Music Assistant core owns that routing, so the lasting fix belongs in the tool that emits the link. Background and discussion: [#31](https://github.com/sproft/music-assistant-ytmusic/issues/31).

**Playlist shows "No playable items found"**
- Ensure you are on the latest version of this provider (playlist support uses a yt-dlp fallback added after the initial release).
- Very large playlists may take a few seconds to load as yt-dlp fetches the track list.

**Audio quality is low**
- Enable "Prefer highest audio quality" in the provider settings (on by default).
- The android_music client typically provides 128-256 kbps AAC or Opus in an M4A/WebM container.

**Cookie authentication failed**
- Make sure you copied the **entire** cookie string from the Network tab (2000+ characters).
- The cookie must contain `__Secure-3PAPISID`, `SID`, `HSID`, and `SSID`.
- If you use a brand account, enter the brand account ID (21-digit number from [myaccount.google.com/brandaccounts](https://myaccount.google.com/brandaccounts)).

**Library is empty after auth**
- Your YouTube Music library only shows content you've explicitly liked, saved, or subscribed to.
- If your library is on a brand account, make sure the brand account ID is set.
- Running several instances? Check that you authenticated the one you are looking at. Each entry holds its own cookie, and an entry left on **Authentication: None** syncs nothing.

**Two instances show the same library**
- The usual cause is capturing both cookies from one browser that has several Google accounts signed in. Google sends the **same** cookie for every account in that session, and only the `X-Goog-AuthUser` index says which account a request means, so recapturing the cookie "for the other account" changes nothing. Either capture each cookie in a separate incognito window signed in to one account only, or set the **Account index** field on the second instance to that account's `X-Goog-AuthUser` value (visible on any `youtubei/v1/...` request in DevTools).
- If both entries point at the same personal account on purpose, set the **Brand account ID** on one of them to split them apart.

**Files disappear after restarting Home Assistant**
- Only use `docker restart addon_d5369777_music_assistant` to restart MA.
- Restarting HA from the UI recreates the container from scratch. See [WATCHER_ADDON.md](WATCHER_ADDON.md) to set up automatic re-copying.

---

## Dependencies

These are installed automatically by the provider on first run via MA's `install_package` utility:

- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi)

> [!NOTE]
> The first run requires outbound network access and a writable virtual environment (`/app/venv`) because the dependencies above are `pip`-installed at setup time. Subsequent starts use the cached packages.

---

## Credits

Built and maintained by [@sproft](https://github.com/sproft), with features and fixes contributed by the community:

- **[@mawoka-myblock](https://github.com/mawoka-myblock):** noticing that every track was streaming at 48 kbps and fixing the format selector to rank audio by bitrate rather than by container, which also restored audio-only extraction on current yt-dlp ([#44](https://github.com/sproft/music-assistant-ytmusic/pull/44)).
- **[@jojo141185](https://github.com/jojo141185):** automated Docker image builds published to GHCR, so standalone Docker and Compose users can run Music Assistant with the provider baked in ([#33](https://github.com/sproft/music-assistant-ytmusic/pull/33)).
- **[@bygadd](https://github.com/bygadd):** opt-in auto-update for the MA Provider Watcher add-on, keeping the provider current from GitHub without a manual reinstall ([#32](https://github.com/sproft/music-assistant-ytmusic/pull/32)).
- **[@gusjengis](https://github.com/gusjengis):** resolving a pasted YouTube or YTM link directly from the search box, plus the `@start-end` video trimming feature ([#29](https://github.com/sproft/music-assistant-ytmusic/pull/29)).
- **[@bsny](https://github.com/bsny):** correct parsing of artists from search results, plus the `--repo-owner` option for the install scripts so forks can install from their own copy ([#26](https://github.com/sproft/music-assistant-ytmusic/pull/26)).

Contributions are welcome. Please [open an issue](https://github.com/sproft/music-assistant-ytmusic/issues) or a pull request.

---

## Legal Disclaimer & Terms of Use

### 1. 100% Free, Open-Source & Strictly Non-Commercial

This project is fully open-source (FOSS), created purely for educational purposes and personal use. **It is not sold, monetized, or distributed commercially in any way.** There are no advertisements, no premium tiers, no subscriptions, and no financial intent behind it whatsoever. Any form of commercial use is explicitly prohibited.

### 2. Personal Local Cache

This provider queries YouTube and YouTube Music APIs for personal playback. When
caching is enabled, it stores completed streams on storage controlled by the
user. It does not circumvent DRM or provide any mechanism for redistributing
cached media.

### 3. No Hosting of Copyrighted Material

This project does not host or redistribute audio or video. A user who enables
caching is responsible for the media retained on their own system and for
ensuring that retention is lawful in their jurisdiction.

### 4. Support the Artists You Listen To

We strongly encourage all users to subscribe to [YouTube Premium](https://www.youtube.com/premium). A Premium subscription is the most direct way to financially support the musicians and creators whose work you enjoy, and to support the platform that hosts it. This project exists as a technical proof-of-concept for developers and home automation enthusiasts. It is not intended to deprive creators of revenue.

### 5. YouTube Terms of Service

This provider interacts with YouTube's internal (unofficial) APIs without a premium account. **This is against YouTube's Terms of Service.** By using this software you acknowledge that:

- You use it entirely at your own risk.
- The developers accept no liability for account suspensions, legal action, or any other consequences arising from its use.
- This project is not affiliated with, endorsed by, or connected to Google LLC or YouTube in any way.
- Google may change their APIs at any time, which may break functionality.

### 6. User Responsibility

The software is provided **"AS IS"**, without warranty of any kind. Users are
solely responsible for ensuring their use of this project, including local
caching, complies with their local laws and the Terms of Service of any
platforms they access.

---

## License

[MIT](LICENSE)
