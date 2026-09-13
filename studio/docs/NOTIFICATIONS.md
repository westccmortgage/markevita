# Production notifications

Open **Notifications / Уведомления**, click **Enable notifications**, allow the
browser prompt, then use **Send test notification**. Enable separately on each
device. Notifications concern only jobs started by the signed-in administrator:
delivered episodes, completed selected stages, failures/interruption, and pauses
requiring reference review. Existing terminal jobs are baselined on first opt-in.

The server checks persisted jobs every 30 seconds independently of the production
worker. A service worker receives encrypted Web Push messages with the Studio tab
closed. Browser/OS permission, background processing, connectivity and sleep can
delay delivery. This is not email, SMS or a guaranteed real-time alert. Disable on
a shared browser before signing out. No notification is requested on page load.

Production uses the existing Supabase store and private R2 bucket; no SQL migration
or additional paid messaging account is needed. Keep `STUDIO_PUBLIC_URL` set to the
canonical HTTPS Studio origin. The VAPID key and subscriptions live under private
`_studio_private/notifications/` in R2; do not expose this prefix publicly or log
its contents. The public-key endpoint returns only the public VAPID key. Preserve
the private key across deployments or browsers will need to resubscribe.

Push targets are restricted to the Chrome, Firefox and Safari push-service hosts;
outbound redirects are disabled. Setup uses authenticated, same-origin,
session-bound CSRF forms. Expired subscriptions are removed on 404/410. Removing
an administrator from `studio_admins` removes their subscriptions on the next
dispatch. Other delivery failures are retried on a later check and never change
job state, budgets or provider requests. Per-browser event records and stable
notification tags limit duplicate alerts; a crash between delivery and recording
can still cause redelivery.

Delivered episodes now have a player and a prominent watch/download button at the
top of the episode page. Completed job pages link there. Checkpoint restoration
fetches up to four immutable objects concurrently, validates their original
checksums, and updates local JSON paths as before. Paid calls, checkpoint commits,
generation quality settings and QC thresholds remain unchanged.

Offline tests cover encryption/VAPID, destination restrictions, auth/CSRF,
recipient isolation, restart deduplication, transient failures, revocation,
playback placement, and checksum/path-preserving concurrent restoration. A real
device must use the test notification to verify browser/OS delivery permissions.
