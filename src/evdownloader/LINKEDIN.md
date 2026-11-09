# LinkedIn Learning provider

The `linkedin` provider uses session-authenticated course metadata and progressive
MP4 sources through the existing download engines. Offline contracts are tested;
real compatibility is **not yet verified** because the authorized Firefox
Developer Edition profile could not be read during the single bounded attempt.
No lesson was downloaded and no further live requests were attempted.

## Quick path

First grant the application running evd permission to read Firefox's profile
directory through macOS privacy settings. Local metadata-only checks found
`profiles.ini`, `installs.ini`, and `Profiles` unreadable; directory enumeration
raised `PermissionError` despite ordinary file modes. The specific macOS privacy
control responsible has not been established. Closing Firefox or selecting a
profile does not resolve this demonstrated access restriction.

Then open the authorized course in Firefox Developer Edition and complete any
login or challenge. Do not export cookies into this repo or close the browser
solely for this check: the installed loader copies the database before reading it.
The installed yt-dlp 2026.03.17 macOS loader searches
`~/Library/Application Support/Firefox/Profiles`; the existing application loader
does not expose an explicit Developer Edition profile selector. Closing the
browser alone does not resolve a filesystem-permission issue. The supported
yt-dlp API accepts an explicit profile, but no profile-selection change was made:
the inaccessible metadata prevents safe discovery and a profile option cannot
remove the demonstrated permissions blocker. No second live attempt was made.
After resolving the profile issue, a bounded verification command is:

```sh
.venv/bin/evd download 'https://www.linkedin.com/learning/COURSE/LESSON' \
  --cookies-from-browser firefox --no-resources --limit 1
```

This enumerates the course and downloads its **first** lesson, not necessarily the
lesson named in the URL. Do not remove the limit for a smoke test.

## Supported boundaries

| Area | Behavior |
| --- | --- |
| URLs | HTTPS `linkedin.com` / `www.linkedin.com`, `/learning/COURSE[/LESSON]`; ambiguous authorities rejected before cookies |
| Session | Existing browser/session loader; requires live URL-scoped `li_at` and `JSESSIONID`; no password login |
| Enumeration | Course, chapters and ordered video lessons; malformed/duplicate entries fail rather than silently disappear |
| Media | Authorized 720p progressive URL on LinkedIn/`licdn.com`; existing engine handles publication and transfer policy |
| Captions | API transcript lines converted to inline WEBVTT and validated/published by shared subtitle handling; unknown language is `und` |
| Missing captions | Explicit null/empty transcript confirms none; omitted inventory remains incomplete, never false success |
| Resources | Exercise files/page snapshots have no verified API contract. Resolution fails explicitly even when fields are absent; `--no-resources` opts out, never an empty-success inventory |
| Redirects/errors | Metadata redirects are rejected, not followed with credentials; auth, challenge, HTTP, schema and transport failures are sanitized. Metadata has no retries; transfer engines retain their existing bounded retry behavior |
| DRM | Rejected when advertised. No license requests, decryption or access-control bypass; HLS-only media is unsupported |

## Evidence and limits

The installed yt-dlp LinkedIn Learning extractor uses
`https://www.linkedin.com/learning-api/detailedCourses` with `courseSlug`,
`fields`, `q=slugs`, and optional `videoSlug` / `resolution=_720`.
It supplies the JSESSIONID CSRF header and parses `chapters[].videos[]` and
`selectedVideo.url.progressiveUrl` / `selectedVideo.transcript.lines`.
Context7's current yt-dlp documentation confirms browser-cookie and embedding
APIs, but does not establish a public stable LinkedIn Learning API contract.
Provider schema fixtures follow that installed implementation, not captured
private account responses. Account entitlement, actual media playback, resource
inventory and current server compatibility remain unverified.

The provider does not persist media URLs or browser cookies. Course structure
uses the existing cache, without resolved media or transcripts. Cookie handling
reuses shared domain/path/HTTPS/expiry policy; LinkedIn credentials cannot follow
the media URL onto the separate CDN domain. Shared transfer checks apply to
downloads and redirects. Use `--no-resources` until resource support is verified.
The shared yt-dlp logger suppresses extractor/HTTP diagnostics that could expose
signed media URLs; exceptions still propagate to the sanitized service reports.
