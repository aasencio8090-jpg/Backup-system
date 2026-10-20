#!/usr/bin/env python3
"""
AutoBackup - universal automatic backup & recovery (Windows / macOS / Linux / Termux / Pydroid).
Pure Python standard library. No installs needed.

QUICK START
  python backup_system.py setup --source "/path/to/my/project"
  python backup_system.py backup          # make a backup now
  python backup_system.py schedule        # turn on automatic backups
  python backup_system.py list            # see all saved versions
  python backup_system.py restore         # recover latest version into a NEW folder (never overwrites)

The destination is auto-detected: Dropbox, OneDrive, Google Drive, iCloud, or ~/Backups.
If that cloud app is signed in and syncing, backups reach the cloud too (check the provider's website once).
"""
import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

CONFIG_DIR = Path.home() / ".autobackup"
CONFIG_FILE = CONFIG_DIR / "config.json"
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".cache", ".Trash"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_EXT = {".pyc", ".tmp"}
CRON_MARK = "# autobackup"
TASK_NAME = "AutoBackup"
CHUNK = 1 << 20
PRERESTORE_KEEP = 5          # keep only the most recent few safety snapshots

# -- Google Drive API (optional; default sync-folder mode needs none of this) --
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]  # least privilege: only files THIS app creates
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"            # you download this from Google Cloud; NEVER commit
TOKEN_FILE = CONFIG_DIR / "token.json"                        # minted on first login; secret; NEVER commit


# -- Small helpers -------------------------------------------------------------
def is_within(child: Path, root: Path) -> bool:
    """True only if child is root or truly inside it (component-wise, not string prefix)."""
    return child == root or root in child.parents


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
        i += 1


@contextmanager
def vault_lock(v: Path):
    """One backup/restore at a time per vault. OS-level lock held in the local config dir, so it
    is released automatically even if a process crashes (no stale lock files)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lp = CONFIG_DIR / (hashlib.sha1(str(v.resolve()).encode()).hexdigest()[:16] + ".lock")
    f = open(lp, "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("Another backup/restore is already running on this folder - try again in a minute.")
        yield
    finally:
        f.close()                                    # closing releases the lock


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


# -- Config --------------------------------------------------------------------
def load_config():
    if not CONFIG_FILE.exists():
        sys.exit("Not set up yet. Run: python backup_system.py setup --source <folder>")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if cfg.get("keep", 0) < 1 or cfg.get("interval_minutes", 0) < 1:
        sys.exit("Invalid config (keep and interval must be >= 1). Re-run setup.")
    return cfg


def _windows_drive_my_drives():
    """Google Drive for Desktop on Windows mounts as a virtual drive letter (G: by default),
    not a folder under home. A top-level 'My Drive' is its signature. Returns any found."""
    found = []
    if os.name != "nt":
        return found
    for letter in "GHIJKLMNOPQRSTUVWXYZDEF":      # G is Drive's default; scan the rest too
        md = Path(f"{letter}:\\") / "My Drive"
        try:
            if md.is_dir():
                found.append(md)
        except OSError:
            pass
    return found


def detect_cloud_folders():
    home = Path.home()
    candidates = [home / n for n in ("Dropbox", "OneDrive", "Google Drive", "GoogleDrive", "My Drive",
                                     "iCloudDrive", "pCloudDrive", "MEGA", "Sync")]
    candidates += [home / "Library/Mobile Documents/com~apple~CloudDocs", home / "Library/CloudStorage"]
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(env):
            candidates.insert(0, Path(os.environ[env]))
    candidates += _windows_drive_my_drives()      # catch Windows Google Drive (G:\My Drive etc.)
    out = []
    for c in candidates:
        if c.exists():
            subs = [p for p in c.iterdir() if p.is_dir()] if c.name == "CloudStorage" else [c]
            for sub in subs:
                out.append(sub / "My Drive" if (sub / "My Drive").is_dir() else sub)
    return out


def _require_google_libs():
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        sys.exit("Google Drive API support needs extra libraries. Install them with:\n"
                 "    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n"
                 "(The default sync-folder mode needs none of this.)")
    return Credentials, InstalledAppFlow, Request, build, MediaFileUpload


def drive_service(interactive=True):
    """Return an authorized Drive API client, logging in the first time if needed.
    The OAuth client file (credentials.json) and the saved token never leave your machine."""
    Credentials, InstalledAppFlow, Request, build, _ = _require_google_libs()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), DRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interactive:
            cred_path = os.environ.get("AUTOBACKUP_GOOGLE_CREDENTIALS", str(CREDENTIALS_FILE))
            if not Path(cred_path).exists():
                sys.exit(f"Missing OAuth client file: {cred_path}\n"
                         "In Google Cloud: create a project, enable the Drive API, make an OAuth client of\n"
                         "type 'Desktop app', download the JSON, and save it to that path. See the README.")
            flow = InstalledAppFlow.from_client_secrets_file(cred_path, DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError("Google Drive not authorized yet - run 'setup --drive-api' interactively first.")
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)                # token holds a refresh token: lock it down
        except OSError:
            pass
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_folder_id(service, name, parent=None):
    """Find (or create) a Drive folder by name under an optional parent. With the drive.file scope
    this sees only folders this app made, which is exactly what we want."""
    q = (f"name = {json.dumps(name)} and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    if parent:
        q += f" and '{parent}' in parents"
    hits = service.files().list(q=q, spaces="drive", fields="files(id,name)", pageSize=10).execute()
    files = hits.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    return service.files().create(body=meta, fields="id").execute()["id"]


def drive_upload(service, local_path: Path, folder_id):
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(local_path), mimetype="application/zip", resumable=True)
    meta = {"name": local_path.name, "parents": [folder_id]}
    req = service.files().create(body=meta, media_body=media, fields="id,name")
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp


def maybe_cloud_upload(cfg, path: Path, quiet=False):
    """Upload a freshly made backup to Drive if API mode is on. A failure here only warns -
    the local backup already succeeded and must never be lost to a network hiccup."""
    if cfg.get("cloud") != "gdrive_api":
        return
    project = Path(cfg["source"]).name
    try:
        service = drive_service(interactive=False)
        parent = drive_folder_id(service, "AutoBackups")
        folder = drive_folder_id(service, project, parent)
        drive_upload(service, path, folder)
        if not quiet:
            print(f"  Uploaded to Google Drive: AutoBackups/{project}/{path.name}")
    except (SystemExit, Exception) as e:          # never crash a successful local backup
        print(f"  WARNING: Google Drive upload failed ({e}). Local backup is safe at {path}.")


def cmd_setup(a):
    if a.keep < 1:
        sys.exit("--keep must be at least 1.")
    if a.interval < 1:
        sys.exit("--interval must be at least 1 minute.")
    source = Path(a.source).expanduser().resolve()
    if not source.is_dir():
        sys.exit(f"Source folder not found: {source}")
    clouds = detect_cloud_folders()
    if a.dest:
        base = Path(a.dest).expanduser().resolve()
    else:
        base = (clouds[0] / "AutoBackups") if clouds else (Path.home() / "Backups")
        print("Possible cloud folders (found on disk, NOT verified as syncing):",
              ", ".join(map(str, clouds)) if clouds else "none (using local ~/Backups)")
    if is_within(base, source) or is_within(source, base):
        sys.exit("Source and backup folders must not contain each other.")
    base.mkdir(parents=True, exist_ok=True)
    cfg = {"source": str(source), "dest": str(base), "keep": a.keep, "interval_minutes": a.interval}
    if a.drive_api:
        cfg["cloud"] = "gdrive_api"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if a.drive_api:
        print("\nAuthorizing Google Drive API (a browser window will open for consent)...")
        svc = drive_service(interactive=True)
        drive_folder_id(svc, source.name, drive_folder_id(svc, "AutoBackups"))
        print(f"Google Drive authorized. Each backup will also upload to: AutoBackups/{source.name}/\n")
    print(f"Saved settings:\n  Source : {source}\n  Backups: {base / source.name}\n  Keep   : {a.keep} versions")
    if not any(is_within(base, c) for c in clouds):
        print("Tip: point --dest at a Dropbox/OneDrive/Drive/iCloud folder so backups can reach the cloud.")
    else:
        print("Note: a folder existing does not prove cloud sync is working. Make sure the cloud app is signed in\n"
              "      and syncing, then confirm the first backup appears on the provider's website.")
    print("Next: run 'backup' once, then 'schedule' for automatic backups.")


def vault(cfg):
    v = Path(cfg["dest"]) / Path(cfg["source"]).name
    v.mkdir(parents=True, exist_ok=True)
    return v


# -- Scanning / fingerprint (content-based) ------------------------------------
def iter_entries(source: Path, dest: Path):
    """Yield (abs_path, rel_path, is_dir) in deterministic order; skips junk, symlinks, and the backup folder."""
    dest = dest.resolve()
    for root, dirs, files in os.walk(source):
        root_p = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS
                         and not (root_p / d).is_symlink() and (root_p / d).resolve() != dest)
        files.sort()
        for d in dirs:
            yield root_p / d, (root_p / d).relative_to(source), True
        for f in files:
            p = root_p / f
            if f in EXCLUDE_FILES or p.suffix in EXCLUDE_EXT or p.is_symlink():
                continue
            yield p, p.relative_to(source), False


def scan(source: Path, v: Path, z: "zipfile.ZipFile | None" = None):
    """
    One pass over the tree. Hashes the CONTENT of every file (not size/mtime), so any real change is
    detected. If z is given, streams each file into the zip while hashing, so the fingerprint describes
    exactly what was stored. Returns (fingerprint, file_count).
    """
    h = hashlib.sha256()
    count = 0
    for p, rel, is_dir in iter_entries(source, v):
        name = rel.as_posix()
        if is_dir:
            h.update(f"D|{name}\n".encode())
            if z is not None:
                zi = zipfile.ZipInfo(name + "/")
                zi.external_attr = (0o40755 << 16) | 0x10
                z.writestr(zi, b"")
            continue
        try:
            src = open(p, "rb")                      # unreadable/locked -> skip with a visible marker
        except OSError as e:
            print(f"  skipped (cannot open): {name} - {e}")
            h.update(f"E|{name}\n".encode())
            continue
        with src:
            fh, size = hashlib.sha256(), 0
            if z is not None:
                zi = zipfile.ZipInfo.from_file(p, name, strict_timestamps=False)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with z.open(zi, "w", force_zip64=True) as dst:
                    for chunk in iter(lambda: src.read(CHUNK), b""):
                        fh.update(chunk); dst.write(chunk); size += len(chunk)
            else:
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    fh.update(chunk); size += len(chunk)
        h.update(f"F|{name}|{size}|{fh.hexdigest()}\n".encode())
        count += 1
    return h.hexdigest(), count


def fast_manifest(source: Path, v: Path):
    """Cheap change signature: {relpath: [size, mtime_ns]} (None for dirs). No content read.
    Used to skip work when nothing changed, without re-reading every byte on disk."""
    m = {}
    for p, rel, is_dir in iter_entries(source, v):
        if is_dir:
            m[rel.as_posix()] = None
        else:
            try:
                st = p.stat()
                m[rel.as_posix()] = [st.st_size, st.st_mtime_ns]
            except OSError:
                m[rel.as_posix()] = "err"
    return m


def read_state(state_file: Path):
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


# -- Backup --------------------------------------------------------------------
def list_versions(v):
    return sorted(v.glob("*.zip"), key=lambda p: p.name, reverse=True)  # newest first


def make_backup(cfg, prefix="backup", force=False, quiet=False, locked=False, paranoid=False):
    if locked:
        return _make_backup(cfg, prefix, force, quiet, paranoid)
    with vault_lock(vault(cfg)):
        return _make_backup(cfg, prefix, force, quiet, paranoid)


def _make_backup(cfg, prefix, force, quiet, paranoid=False):
    source = Path(cfg["source"])
    if not source.is_dir():
        sys.exit(f"Source folder missing: {source}")
    v = vault(cfg)
    state_file = v / ".state.json"

    prior = read_state(state_file)
    have_versions = bool(list_versions(v))
    fm = fast_manifest(source, v)                    # cheap: path + size + mtime, no content read

    # Fast skip (default): nothing to do if every file's size and mtime is unchanged.
    # Metadata only -- a content edit that preserves the exact size AND timestamp is not caught
    # here; use paranoid=True / `backup --paranoid` to decide by hashing contents instead.
    if not force and not paranoid and have_versions and prior and prior.get("manifest") == fm:
        if not quiet:
            print("No changes since last backup - skipped.")
        return None

    final = unique_path(v / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.zip")
    tmp = final.with_name(final.name + ".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            fp, count = scan(source, v, z)           # single content pass: hashes while zipping
        with zipfile.ZipFile(tmp) as z:              # verify before trusting it
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"Corrupt entry: {bad}")
        # Paranoid skip: contents identical to the last kept backup -> throw this one away.
        if paranoid and not force and have_versions and prior and prior.get("fingerprint") == fp:
            tmp.unlink()
            if not quiet:
                print("No changes since last backup - skipped.")
            return None
        os.replace(tmp, final)                       # atomic: never a half-written backup
    except BaseException as e:
        if tmp.exists():
            tmp.unlink()
        if isinstance(e, KeyboardInterrupt):
            raise
        sys.exit(f"Backup FAILED: {e}")

    if prefix == "backup":                           # snapshots must not reset change detection
        state_file.write_text(json.dumps({"fingerprint": fp, "manifest": fm, "last": final.name}),
                              encoding="utf-8")
        prune(v, cfg["keep"], "backup")
    elif prefix == "prerestore":                     # bound safety snapshots so they can't pile up
        prune(v, PRERESTORE_KEEP, "prerestore")
    if not quiet:
        print(f"Backed up {count} files -> {final} ({human(final.stat().st_size)}, verified)")
    if prefix == "backup":
        maybe_cloud_upload(cfg, final, quiet)
    return final


def prune(v, keep, prefix="backup"):
    for p in [p for p in list_versions(v) if p.name.startswith(prefix + "_")][keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def cmd_backup(a):
    make_backup(load_config(), force=a.force, paranoid=a.paranoid)


# -- List / Restore ------------------------------------------------------------
def cmd_list(a):
    v = vault(load_config())
    vers = list_versions(v)
    if not vers:
        return print("No backups yet. Run: python backup_system.py backup")
    print(f"{'#':<4}{'Version':<34}{'Size':<12}Date")
    for i, p in enumerate(vers, 1):
        st = p.stat()
        print(f"{i:<4}{p.name:<34}{human(st.st_size):<12}{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}")


def pick_version(v, which):
    vers = list_versions(v)
    if not vers:
        sys.exit("No backups to restore from.")
    if which in (None, "latest"):
        regular = [p for p in vers if p.name.startswith("backup_")]
        return (regular or vers)[0]
    if which.isdigit() and 1 <= int(which) <= len(vers):
        return vers[int(which) - 1]
    for p in vers:
        if which in (p.name, p.stem):
            return p
    sys.exit(f"Version not found: {which} (use 'list' to see options)")


def plan_extract(z: zipfile.ZipFile, target: Path):
    """Validate EVERY entry before writing anything. Returns [(member, out_path)]."""
    root = target.resolve()
    plan = []
    for m in z.infolist():
        n = m.filename
        if (not n or "\x00" in n or "\\" in n or n.startswith("/") or n.startswith("~")
                or (len(n) > 1 and n[1] == ":")):
            sys.exit(f"Unsafe name in archive: {n!r}")
        if ".." in PurePosixPath(n).parts:
            sys.exit(f"Unsafe path in archive: {n!r}")
        if (m.external_attr >> 16) & 0o170000 == 0o120000:
            sys.exit(f"Archive contains a symlink, refusing: {n!r}")
        out = Path(os.path.normpath(root / n))
        # resolve() follows any symlinks already on disk, so a link inside target can't redirect writes
        if not is_within(out.parent.resolve(), root) or out.is_symlink():
            sys.exit(f"Entry would escape the restore folder: {n!r}")
        plan.append((m, out))
    return plan


def extract_plan(z, plan):
    for m, out in plan:
        if m.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".restoring")
        with z.open(m) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        os.replace(tmp, out)                         # per-file atomic


def cmd_restore(a):
    if a.mirror and not a.overwrite:
        sys.exit("--mirror only works together with --overwrite.")
    cfg = load_config()
    v = vault(cfg)
    src = Path(cfg["source"])
    with vault_lock(v):
        chosen = pick_version(v, a.version)
        if a.overwrite:
            target = src
        elif a.to:
            target = Path(a.to).expanduser()
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                sys.exit(f"Refusing: {target} already has files. Choose an empty/new folder, or use --overwrite.")
        else:
            target = unique_path(src.parent / f"{src.name}_restored_{chosen.stem.split('_', 1)[-1]}")
        target.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(chosen) as z:
            if z.testzip():
                sys.exit("That backup file is corrupt - try an older version.")
            plan = plan_extract(z, target)
            clobber = sum(1 for m, out in plan if not m.is_dir() and out.exists())
            stale, entries = [], []
            if a.mirror:
                keep = {out for _, out in plan}
                entries = list(iter_entries(src, v))
                stale = [p for p, rel, d in entries if not d and Path(os.path.normpath(p)) not in keep]
            if a.overwrite:
                print(f"This will REPLACE {clobber} existing file(s) in {src} with versions from {chosen.name}.")
                if a.mirror:
                    print(f"--mirror: {len(stale)} file(s) that are NOT in that backup will be DELETED "
                          f"(recoverable from the safety snapshot).")
                if not a.yes:
                    if not sys.stdin.isatty():
                        sys.exit("Add --yes to confirm overwrite (non-interactive).")
                    if input("Type YES to continue: ").strip() != "YES":
                        sys.exit("Cancelled. Nothing changed.")
                print("Saving a safety snapshot of your current files first...")
                if make_backup(cfg, prefix="prerestore", force=True, quiet=True, locked=True) is None:
                    sys.exit("Could not create safety snapshot - aborting.")
            elif clobber:
                sys.exit("Refusing: restore would overwrite existing files.")
            extract_plan(z, plan)
            if stale:
                for p in stale:
                    try:
                        p.unlink()
                    except OSError as e:
                        print(f"  could not delete {p}: {e}")
                plan_dirs = {out for m, out in plan if m.is_dir()}
                for p, rel, d in sorted((e for e in entries if e[2]), key=lambda e: len(str(e[0])), reverse=True):
                    if Path(os.path.normpath(p)) not in plan_dirs:
                        try:
                            p.rmdir()                # only succeeds if empty
                        except OSError:
                            pass

    if a.overwrite:
        extra = f" and removed {len(stale)} extra file(s)" if a.mirror else " (files added after that backup were left alone)"
        print(f"Restored {chosen.name} INTO {src}{extra}.")
    else:
        print(f"Restored {chosen.name} to a NEW folder (nothing overwritten):\n  {target}")


# -- Automation ----------------------------------------------------------------
def cron_expr(mins: int) -> str:
    """Return an EXACT cron schedule for this interval, or raise. cron can't express arbitrary intervals."""
    ok = ("Valid on Mac/Linux: 1,2,3,4,5,6,10,12,15,20,30 min; 60,120,180,240,360,480,720 min "
          "(1,2,3,4,6,8,12 h); or 1440 (daily). Use 'watch' for any other interval.")
    if mins < 1:
        raise ValueError("Interval must be at least 1 minute.")
    if mins < 60:
        if 60 % mins:
            raise ValueError(f"{mins} min doesn't divide an hour evenly (cron would fire unevenly). {ok}")
        return f"*/{mins} * * * *"
    if mins == 1440:
        return "0 0 * * *"
    if mins % 60 or mins > 1440 or 24 % (mins // 60):
        raise ValueError(f"{mins} min can't be scheduled exactly with cron. {ok}")
    return f"0 */{mins // 60} * * *"


def cmd_schedule(a):
    cfg = load_config()
    mins = a.every if a.every is not None else cfg["interval_minutes"]
    if mins < 1:
        sys.exit("Interval must be at least 1 minute.")
    py, script = sys.executable, str(Path(__file__).resolve())
    if platform.system() == "Windows":
        if not 1 <= mins <= 1440:
            sys.exit("Windows supports 1-1440 minutes.")
        mode = ["/SC", "DAILY", "/MO", "1"] if mins == 1440 else ["/SC", "MINUTE", "/MO", str(mins)]
        r = subprocess.run(["schtasks", "/Create", *mode, "/TN", TASK_NAME,
                            "/TR", f'"{py}" "{script}" backup', "/F"], capture_output=True, text=True)
        ok, msg = r.returncode == 0, (r.stderr or r.stdout)
    elif shutil.which("crontab"):
        try:
            expr = cron_expr(mins)
        except ValueError as e:
            sys.exit(str(e))
        cmd = " ".join(shlex.quote(x) for x in (py, script, "backup")).replace("%", r"\%")
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if cur.returncode != 0 and "no crontab" not in (cur.stderr or "").lower():
            sys.exit(f"Could not read your crontab, not touching it: {cur.stderr.strip()}")
        keep = [l for l in cur.stdout.splitlines() if CRON_MARK not in l]
        r = subprocess.run(["crontab", "-"], input="\n".join(keep + [f"{expr} {cmd} >/dev/null 2>&1 {CRON_MARK}"]) + "\n",
                           text=True, capture_output=True)
        ok, msg = r.returncode == 0, r.stderr
    else:
        ok, msg = False, "No scheduler found."
    if ok:
        print(f"Automatic backups ON - every {mins} min. (Turn off: python backup_system.py unschedule)")
    else:
        print(f"Couldn't install a system scheduler ({msg.strip()}).\n"
              f"Use this instead (works everywhere, incl. phones): python backup_system.py watch")


def cmd_unschedule(a):
    if platform.system() == "Windows":
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)
    elif shutil.which("crontab"):
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if cur.returncode == 0:
            keep = [l for l in cur.stdout.splitlines() if CRON_MARK not in l]
            subprocess.run(["crontab", "-"], input="\n".join(keep) + ("\n" if keep else ""),
                           text=True, capture_output=True)
    print("Automatic backups OFF.")


def cmd_watch(a):
    cfg = load_config()
    mins = a.every if a.every is not None else cfg["interval_minutes"]
    if mins < 1:
        sys.exit("Interval must be at least 1 minute.")
    print(f"Watching {cfg['source']} - backing up every {mins} min. Ctrl+C to stop.")
    try:
        while True:
            try:
                make_backup(cfg, quiet=True)
            except SystemExit as e:                  # one failed run must not kill the loop
                print(f"[{datetime.now():%H:%M:%S}] {e}")
            time.sleep(mins * 60)
    except KeyboardInterrupt:
        print("Stopped.")


# -- CLI -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Universal automatic backup & recovery")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="choose folder to protect + where backups go")
    s.add_argument("--source", required=True)
    s.add_argument("--dest", help="default: auto-detected cloud folder, else ~/Backups")
    s.add_argument("--keep", type=int, default=30, help="versions to keep (default 30)")
    s.add_argument("--interval", type=int, default=30, help="minutes between auto-backups (default 30)")
    s.add_argument("--drive-api", action="store_true",
                   help="also upload each backup via the Google Drive API (one-time login; see README)")
    s.set_defaults(fn=cmd_setup)

    b = sub.add_parser("backup", help="back up now (skips if nothing changed)")
    b.add_argument("--force", action="store_true")
    b.add_argument("--paranoid", action="store_true",
                   help="detect changes by hashing file contents, not just size+mtime "
                        "(slower; catches edits that keep the same size and timestamp)")
    b.set_defaults(fn=cmd_backup)

    sub.add_parser("list", help="show saved versions").set_defaults(fn=cmd_list)

    r = sub.add_parser("restore", help="recover a version (default: latest, into a new folder)")
    r.add_argument("version", nargs="?", help="'latest', a number from 'list', or a file name")
    r.add_argument("--to", help="restore into this EMPTY folder")
    r.add_argument("--overwrite", action="store_true", help="restore over the original (snapshots current first)")
    r.add_argument("--yes", action="store_true", help="skip the overwrite confirmation")
    r.add_argument("--mirror", action="store_true",
                   help="with --overwrite: also delete files that are not in the backup (snapshot taken first)")
    r.set_defaults(fn=cmd_restore)

    sc = sub.add_parser("schedule", help="turn on automatic backups (OS scheduler)")
    sc.add_argument("--every", type=int, help="minutes")
    sc.set_defaults(fn=cmd_schedule)
    sub.add_parser("unschedule", help="turn off automatic backups").set_defaults(fn=cmd_unschedule)

    w = sub.add_parser("watch", help="keep running and back up on an interval (any device)")
    w.add_argument("--every", type=int, help="minutes")
    w.set_defaults(fn=cmd_watch)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
