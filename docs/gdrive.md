# gdrive source — rclone setup

The `gdrive` knowledge base is populated from Google Drive shared drives through
[rclone][rclone]. `make gdrive-sync` runs `rclone sync` of the shared drives
into `./gdrive`, then POSTs `/index` to reconcile the tree into the KB. This
doc covers the **one-time rclone setup** (the prerequisite). The sync / index
mechanics, the exclude list, the backup-dir retention, and the make targets are
in [docs/operations.md](operations.md) ("Populating the source") and the
README gdrive section.

## Prerequisite

`make gdrive-sync` needs two host tools and one authenticated rclone remote:

- **rclone** (v1.60+) and **jq** on the host `PATH`. The script fail-fasts with
  a clear message if either is missing (`exit 127`).
- A rclone remote (default name **`gdrive`**) of storage type `drive` (Google
  Drive), authenticated with a Google account that has access to the shared
  drives you index. Override the remote name with `gdrive-sync --remote NAME`
  or `make gdrive-sync REMOTE=NAME` is not wired — use the script flag.

`make gdrive-sync` enumerates the shared drives with
`rclone backend --json drives gdrive:` and syncs each one. It fail-fasts if
the remote is missing, not authenticated, or sees zero shared drives.

## Configure the `gdrive` remote (one-time)

Run `rclone config` and create the remote:

1. `n` — new remote.
2. `name> gdrive` — must match the default the script uses (or pass
   `--remote`).
3. `Storage> drive` — Google Drive (type the listed number or `drive`).
4. `client_id>` / `client_secret>` — leave blank to use rclone's bundled
   credentials, or set your own Google OAuth client (recommended for heavy use;
   rclone's shared client_id is rate-limited). See the rclone [drive
   docs][rclone-drive].
5. `Scope> drive.readonly` — **read-only is sufficient and correct**: the sync
   only reads the remote (downloads); local deletions to match the source are
   local filesystem ops, not remote writes. Least privilege.
6. `root_folder_id>` — leave blank so the remote sees the whole account (the
   script enumerates shared drives itself).
7. `service_account>` — `n` (use OAuth, not a service account).
8. `Edit advanced config?` — `n`.
9. `Use auto config?` — `y` to open a browser and complete the OAuth login on
   this machine. On a headless server, answer `n` and run `rclone authorize
   "drive"` on a machine with a browser, then paste the resulting token back
   (see [Headless auth](#headless-auth-server) below).
10. `Configure as a Shared Drive ("team drive")?` — `n`. The script enumerates
    shared drives itself via `rclone backend drives`; do not pin the remote to a
    single team drive, or the enumeration may not see the others.

The account you log in with must have access to the shared drives you want to
index. Shared ("team") drives are a Google Workspace feature; a personal
`@gmail.com` account without shared-drive access will see zero drives and the
sync will fail-fast.

## Headless auth (server)

On a host with no browser, do the OAuth dance on another machine:

1. On the headless host: `rclone config` → create `gdrive` as above, but at
   `Use auto config?` answer `n`. rclone prints a command to run on a
   browser-equipped machine.
2. On that machine (rclone installed): run the printed
   `rclone authorize "drive" "<client_id>" "<client_secret>"` (omit the id/secret
   if you left them blank). A browser opens; log in + authorize. rclone prints a
   JSON token blob.
3. Paste that blob back into the headless `rclone config` prompt.

The refresh token is stored in your rclone config (host-only, owner-readable).
**Never commit it** — rclone config lives outside this repo.

## Verify

After config, confirm the remote sees the shared drives (the same call the
script makes):

```
rclone backend --json drives gdrive: | jq 'length'   # shared-drive count (>0)
rclone lsd gdrive:                                    # list the drives
```

If the count is 0, the account has no shared-drive access (personal account, or
access not granted) — the sync will fail-fast.

## Re-authenticate

Re-login only when the OAuth token expires or Drive access is revoked:

```
rclone config reconnect gdrive:    # re-run the OAuth flow, replaces the token
```

Symptoms: `rclone backend drives gdrive:` fails with a 401 / `invalid_grant`,
or the sync fail-fasts on `cannot list shared drives`.

## How indexing uses the remote

`make gdrive-sync` (and `make gdrive-index`, the `/index`-only variant):

- enumerates shared drives with `rclone backend --json drives gdrive:`;
- runs `rclone sync --backup-dir --delete-after` of each drive into a per-drive
  subdir of `./gdrive` (delta: files removed from Drive are deleted from
  `./gdrive`; deleted/overwritten files are retained in `./.gdrive-backup/`);
- normalizes the synced tree to owner-only perms (dirs 700, files 600) because
  Drive content is business-sensitive — rclone v1.60 has no `--umask`, so the
  script normalizes after the sync;
- POSTs `/index` to api-gateway to reconcile `./gdrive` into the `gdrive` KB.

The exclude list (`./gdrive-exclude.conf`, gitignored) and the backup-dir
recovery are documented in [docs/operations.md](operations.md). The
`gdrive` KB itself is created + granted by `make gdrive-index-bootstrap`
([docs/operations.md](operations.md) "Provisioning").

[rclone]: https://rclone.org
[rclone-drive]: https://rclone.org/drive/