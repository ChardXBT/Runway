# YouTube — facts for connection, publishing and sync

All pages read 2026-11-14 unless noted. Labels are explained in [`../README.md`](../README.md).

Sources:

- Quota calculator: <https://developers.google.com/youtube/v3/determine_quota_cost>
- Revision history: <https://developers.google.com/youtube/v3/revision_history>
- `videos.insert`: <https://developers.google.com/youtube/v3/docs/videos/insert>
- Quota and compliance audits: <https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits>
- Resumable uploads: <https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol>
- Push notifications: <https://developers.google.com/youtube/v3/guides/push_notifications>
- Developer Policies: <https://developers.google.com/youtube/terms/developer-policies>
- OAuth 2.0 overview: <https://developers.google.com/identity/protocols/oauth2>
- OAuth web server flow: <https://developers.google.com/identity/protocols/oauth2/web-server>
- Sensitive scope verification: <https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification>
- Verification requirements: <https://support.google.com/cloud/answer/13464321>
- Issue tracker 572902667: <https://issuetracker.google.com/issues/572902667>

## 1. Quota

- [Official] "Projects that enable the YouTube Data API have a default quota allocation of 100
  `search.list` calls, 100 `videos.insert` calls, and 10,000 units per day combined for all other
  endpoints." The two methods "have their own quota buckets … The quota cost is 1 per call."
- [Official] History: upload cost dropped from about 1,600 to about 100 units on 2025-12-04; the
  move to separate buckets began 2026-06-01.
- [Official] Costs in the shared 10,000 bucket: `videos.list` 1, `channels.list` 1,
  `playlistItems.list` 1, `commentThreads.list` 1, `comments.list` 1, `videos.update` 50,
  `videos.delete` 50, `thumbnails.set` 50, `comments.insert` 50, `playlistItems.insert` 50,
  `captions.insert` 400. `videos.batchGetStats` (added 2026-06-03) costs 1 in its own bucket of
  10,000 per day.
- [Inference] Quota is per Google Cloud project, so in hosted mode all workspaces share it.
  100 uploads per day is the first ceiling a hosted product hits. One upload with a custom
  thumbnail costs 1 upload call plus 50 shared units.
- [Official] More quota requires a compliance audit through the "YouTube API Services - Audit
  and Quota Extension Form" (<https://support.google.com/youtube/contact/yt_api_form>). The page
  does not state how long it takes.

## 2. The private lock on unaudited projects — lifted

- [Official] Revision history, 2026-11-02: videos uploaded through `videos.insert` "from
  unverified API projects are no longer restricted to private viewing mode". The same entry says
  that when `status.privacyStatus` is omitted it now defaults to `public` (or `private`,
  following the creator's account settings).
- [Official] The `videos.insert` reference now reads that such videos "are not restricted to
  private viewing mode".
- [Official] Issue tracker 572902667 ("Conflicting documentation for unverified YouTube API
  project …") has a Google reply dated 2026-10-13 saying the same.
- [Community] Several vendor guides still quote the old rule. They are stale.
- **Consequence:** always send `status.privacyStatus` explicitly. Never rely on the default; an
  omitted value can now publish publicly.
- **Risk:** the change is days old. Prove it with one live upload from a new project before
  relying on it (acceptance test Y-3 in the plan).

## 3. Uploading a video

- [Official] Endpoint `POST https://www.googleapis.com/upload/youtube/v3/videos`. Maximum file
  size 256 GB. MIME `video/*` or `application/octet-stream`.
- [Official] Scopes that allow it: `youtube.upload`, `youtube`, `youtubepartner`,
  `youtube.force-ssl`. Use `youtube.upload` (narrowest).
- [Official] Settable fields: `snippet.title`, `snippet.description`, `snippet.tags[]`,
  `snippet.categoryId`, `snippet.defaultLanguage`, `localizations`, `status.embeddable`,
  `status.license`, `status.privacyStatus`, `status.publicStatsViewable`, `status.publishAt`,
  `status.selfDeclaredMadeForKids`, `status.containsSyntheticMedia`,
  `recordingDetails.recordingDate`. Query parameter `notifySubscribers`.
- [Official] Errors worth handling by name: `uploadLimitExceeded`, `invalidPublishAt`,
  `invalidTitle`, `invalidDescription`, `invalidTags`, `invalidCategoryId`,
  `forbiddenPrivacySetting`, `mediaBodyRequired`.
- [Official] Resumable protocol:
  1. `POST …/videos?uploadType=resumable&part=snippet,status` with the JSON body and headers
     `X-Upload-Content-Length` and `X-Upload-Content-Type`. Response `200` with a `Location`
     header: the session URI.
  2. `PUT` the bytes to the session URI. `201` returns the video resource.
  3. To resume: `PUT` with `Content-Length: 0` and `Content-Range: bytes */TOTAL`. A `308`
     with `Range: bytes=0-N` says how much arrived. Continue from `N+1`.
  4. Chunks must be multiples of 256 KB except the last. Retry `500`, `502`, `503`, `504` with
     exponential backoff. An expired session returns `404`; start over.
- [Official] Scheduling: set `status.privacyStatus=private` and `status.publishAt`. YouTube
  publishes at that time itself. This is the one platform of the three with native scheduling.
- [Official] Thumbnails: `thumbnails.set` (50 units). Maximum size 50 MB since 2026-09-14.
  Since 2026-11-03 it also works for Shorts after processing finishes (`shortsAspectRatio`).
- [Unverified] Custom thumbnails have historically required a phone-verified channel. Not
  re-checked. Treat a `403` from `thumbnails.set` as "not available for this channel", not as a
  publish failure.
- [Unverified] Shorts have no API flag. A vertical or square video within YouTube's Shorts
  length limit is classified as a Short by YouTube. Runway's existing "≤ 180 s" label is a
  heuristic and should stay labelled as one.
- [Official] After upload, poll `videos.list?part=status,processingDetails` for
  `status.uploadStatus` (`uploaded`, `processed`, `failed`, `rejected`) to verify.

## 4. OAuth and verification

- [Official] Use `access_type=offline` to get a refresh token and `prompt=consent` to force one
  on reconnect. `include_granted_scopes=true` enables incremental authorization.
- [Official] Users may grant only some requested scopes; the app "must verify which scopes were
  actually granted". Runway already stores granted scopes; publishing must check for
  `youtube.upload` at request time.
- [Official] Redirect URIs must be HTTPS, with localhost exempt. Local mode keeps working.
- [Official] Revocation endpoint: `https://oauth2.googleapis.com/revoke`.
- [Official] A refresh token stops working when: the user revokes access; it is unused for six
  months; the account exceeds its limit of live refresh tokens; time-based access expires; or an
  admin restricts the service. "There is currently a limit of 100 refresh tokens per Google
  Account per OAuth 2.0 client ID."
- [Official] **Testing status trap:** a project with an external user type and publishing status
  "Testing" "is issued a refresh token expiring in 7 days". An owner-only install in Testing
  status would need reconnecting every week.
- [Community] The usual fix for personal use is to set publishing status to "In production"
  without submitting for verification: tokens then last, the owner clicks through an
  "unverified app" warning once, and the app is capped at 100 users. I did not find a single
  official page stating all three together on 2026-11-14; test it (acceptance test Y-1).
- [Official] Sensitive-scope verification needs: a public homepage that describes the app; a
  privacy policy on the same domain, linked from the consent screen; domain ownership verified in
  Search Console; a demo video (unlisted on YouTube) showing the consent flow with the client ID
  visible in the address bar and each scope in use; a written justification per scope. Stated
  duration: "can take up to 10 days".
- [Official] The security assessment applies "for restricted scopes only". [Unverified] YouTube
  scopes are classed as sensitive rather than restricted; confirm in the Cloud Console scope
  picker, which labels each scope.

## 5. Policy duties (hosted mode)

Developer Policies, section numbers as on the page:

- III.E.4.c: "After 30 calendar days, the API Client must either delete or refresh the stored
  data."
- III.D.2.c.1: when a user revokes consent in the app, revoke the token "right away" and delete
  Authorized Data "within 7 calendar days".
- III.D.2.c.2: when a user revokes through Google's page, delete related API Data "within 30
  calendar days".
- III.E.4.g: on a user's deletion request, delete "within 7 calendar days".
- III.D.1.c.3: "you must create exactly one (1) API Project for that API Client". No extra
  projects to multiply quota.
- III.J.2: an exception lets a developer create a separate "Upload Project" to "upload your own
  videos to your own official YouTube channel(s)".
- III.E.2.b: aggregate per content owner only; "must not combine API Data from the different
  content owners".

Runway already avoids blended metrics. What is new: a 30-day refresh-or-delete job, deletion on
disconnect, and detection of revocations made on Google's side.

## 6. Real-time

- [Official] WebSub hub `https://pubsubhubbub.appspot.com/subscribe`, topic
  `https://www.youtube.com/xml/feeds/videos.xml?channel_id=CHANNEL_ID`. Notifications fire when a
  channel uploads a video or updates a title or description. Payload is Atom with
  `yt:videoId`, `yt:channelId`, `published`, `updated`.
- [Official] Nothing on that page covers comments, analytics or processing status.
- [Community] Subscriptions carry a lease and must be renewed; delivery is best-effort. Treat a
  notification as a hint to sync now, never as the record.
- Comments and analytics: polling only. Analytics data lags by about two days.

## 7. Could not determine

- Audit duration and rejection reasons for the quota extension form.
- Whether "In production, unverified" behaves as described in §4 in November 2026.
- Whether custom thumbnails still require channel phone verification.
- The exact Shorts length limit the API path honours today.
- WebSub lease length and whether `hub.secret` signatures are sent.
