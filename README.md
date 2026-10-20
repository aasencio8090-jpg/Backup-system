# AutoBackup

Universal automatic backup & recovery in a single Python file. Works on Windows, macOS,
Linux, and Android (Termux / Pydroid). The default mode is **pure standard library — no
installs, no accounts.**

## Quick start

```bash
python backup_system.py setup --source "/path/to/my/project"
python backup_system.py backup          # back up now (skips if nothing changed)
python backup_system.py schedule        # turn on automatic backups
python backup_system.py list            # see all saved versions
python backup_system.py restore         # recover latest into a NEW folder (never overwrites)
```

`backup` skips instantly when nothing has changed (size + mtime check). Add `--paranoid`
to detect changes by hashing file contents instead — slower, but catches edits that keep
the exact same size and timestamp.

## Getting backups to the cloud

There are two ways, and they are independent.

### 1. Sync-folder mode (default, no dependencies)

Point the backup destination at a folder that a cloud app already syncs, and let that app
do the uploading:

```bash
python backup_system.py setup --source "/path/to/project" --dest "/path/to/Dropbox/AutoBackups"
```

Auto-detection finds Dropbox, OneDrive, iCloud, and Google Drive in their usual spots,
including:
- Google Drive on macOS: `~/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive`
- Google Drive legacy folder: `~/Google Drive`
- **Google Drive on Windows**, which mounts as a virtual drive letter (usually `G:\My Drive`)

If auto-detection misses it, just name it: `--dest "G:\My Drive\AutoBackups"`.

> Note: a folder existing does **not** prove the cloud app is actually syncing. Make sure
> the app is installed, signed in, and syncing, then confirm your first backup appears on
> the provider's website.

### 2. Google Drive API mode (optional, uploads directly)

This uploads each backup to Drive itself, independent of the desktop app, and confirms the
upload. It needs a one-time Google login and three libraries.

```bash
pip install -r requirements.txt
```

**Create your own OAuth client (one time):**
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. Enable the **Google Drive API** for that project.
3. Configure the OAuth consent screen (External is fine; add yourself as a test user).
4. Create credentials → **OAuth client ID** → application type **Desktop app**.
5. Download the JSON and save it as `~/.autobackup/credentials.json`
   (or point `AUTOBACKUP_GOOGLE_CREDENTIALS` at it).

Then:

```bash
python backup_system.py setup --source "/path/to/project" --drive-api
```

A browser opens once for consent; a token is saved to `~/.autobackup/token.json` and
reused after that. Every `backup` then also uploads to `AutoBackups/<project>/` in your
Drive. Local backups are still kept too, so a failed upload never loses a backup.

**Least privilege:** the app requests only the `drive.file` scope — it can see and manage
*only the files it creates*, never the rest of your Drive.

## Putting this on GitHub securely

The code contains **no secrets** and is safe to publish. The things that must never be
committed are your personal credential and token files — which is why they live in
`~/.autobackup/`, outside the repo, and are also listed in `.gitignore` as a backstop.

Checklist before your first push:

1. **Use the included `.gitignore`.** It excludes `credentials.json`, `token.json`, `.env`,
   `*.zip`/`*.part` (your actual data), and the `.autobackup/` directory.
2. **Never hardcode a client ID, client secret, or token** in the source. This project
   loads them from your home directory or an env var — keep it that way.
3. **Each user brings their own OAuth client.** Don't ship your `credentials.json`; the
   README tells users to create their own. That keeps your Google quota and project yours.
4. **Scan before you push.** Install a scanner such as
   [`gitleaks`](https://github.com/gitleaks/gitleaks) and run `gitleaks detect` on the repo,
   or add a [`pre-commit`](https://pre-commit.com/) hook so it runs automatically.
5. **If in doubt, start the repo private.** Make it public only after a clean scan.

If you ever commit a secret by accident, deleting it in a new commit is **not** enough — it
stays in git history:
- **Rotate it immediately** in the Google Cloud Console (delete/regenerate the OAuth client;
  revoke the token).
- **Scrub history** with [`git filter-repo`](https://github.com/newren/git-filter-repo) or
  the [BFG Repo-Cleaner](https://rtyley.github.io/bfg-repo-cleaner/), then force-push.

## Commands

| Command | What it does |
|---|---|
| `setup` | Choose the folder to protect and where backups go (`--drive-api` for API mode) |
| `backup` | Back up now; skips if unchanged (`--force` to override, `--paranoid` for content hashing) |
| `list` | Show saved versions |
| `restore` | Recover a version (default: latest, into a new folder; `--overwrite`/`--mirror` available) |
| `schedule` / `unschedule` | Turn automatic backups on/off via the OS scheduler |
| `watch` | Keep running and back up on an interval (works anywhere, incl. phones) |
