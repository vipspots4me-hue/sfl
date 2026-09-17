import os
import sys
import io
import json
import time
import base64
import hashlib
import tarfile
import signal
import shutil
import logging
import threading
import subprocess
import urllib.request
import urllib.error
import re
from pathlib import Path
from datetime import datetime, timezone

import streamlit as st


# ============================================================
# SHL - Persistent Ubuntu Server on Streamlit
# GitHub-backed persistence
# ============================================================

APP_NAME = "SHL"

# ============================================================
# GitHub configuration
# ============================================================

GITHUB_REPO = "vipspots4me-hue/sfl"
GITHUB_BRANCH = "main"

# Persistent branch used only for server state
PERSISTENT_BRANCH = "shl-persistent"

GITHUB_API = "https://api.github.com"

# GitHub individual file limit is 100 MB.
# Keep considerably below that.
CHUNK_SIZE = 45 * 1024 * 1024

# Backup debounce
BACKUP_DEBOUNCE = 15

# Minimum time between automatic GitHub backups
MIN_BACKUP_INTERVAL = 60


# ============================================================
# Runtime directories
# ============================================================

BASE_DIR = Path("/tmp/shl-runtime")

ROOTFS_DIR = BASE_DIR / "ubuntu"
PROOT_DIR = BASE_DIR / "proot"
RESTORE_DIR = BASE_DIR / "restore"

STATE_DIR = BASE_DIR / "state"

SSHX_DIR = Path.home() / ".local" / "bin"
SSHX_PATH = SSHX_DIR / "sshx"

SSHX_PID_FILE = STATE_DIR / "sshx.pid"
SSHX_LINK_FILE = STATE_DIR / "sshx.link"
SSHX_LOG_FILE = STATE_DIR / "sshx.log"

BACKUP_STATE_FILE = STATE_DIR / "backup-state.json"
BACKUP_LOCK_FILE = STATE_DIR / "backup.lock"

SERVICE_DIR = ROOTFS_DIR / "etc" / "shl" / "services"

BASE_PACKAGES_MARKER = ROOTFS_DIR / "etc" / "shl" / ".base-packages-installed"

UBUNTU_VERSION = "22.04.5"

UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/"
    "22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/"
    "download/proot-amd64"
)

SSHX_INSTALLER = "https://sshx.io/get"


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="[SHL] %(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("shl")


# ============================================================
# Required Ubuntu packages
# ============================================================

BASE_PACKAGES = [
    "bash",
    "ca-certificates",
    "curl",
    "wget",
    "unzip",
    "tar",
    "gzip",
    "procps",
    "iproute2",
    "iputils-ping",
    "net-tools",
    "python3",
    "python3-pip",
    "git",
    "openssl",
    "netcat-openbsd",
    "neofetch",
    "sudo",
    "nano",
    "vim",
    "less",
    "psmisc",
    "file",
    "jq",
    "xz-utils",
    "bzip2",
    "zip",
    "unzip",
]


# ============================================================
# Generic helpers
# ============================================================

def ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ROOTFS_DIR.parent.mkdir(parents=True, exist_ok=True)
    PROOT_DIR.mkdir(parents=True, exist_ok=True)
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SSHX_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def calculate_sha256(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            data = f.read(1024 * 1024)

            if not data:
                break

            h.update(data)

    return h.hexdigest()


def atomic_write_text(path, text):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        text,
        encoding="utf-8",
    )

    tmp.replace(path)


def atomic_write_json(path, obj):
    atomic_write_text(
        path,
        json.dumps(
            obj,
            indent=2,
            ensure_ascii=False,
        ),
    )


def load_json(path, default=None):
    try:
        return json.loads(
            Path(path).read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return default


# ============================================================
# Streamlit Secrets
# ============================================================

def get_secret(name, default=""):
    try:
        value = st.secrets.get(name)

        if value is not None:
            return str(value)

    except Exception:
        pass

    return os.environ.get(
        name,
        default,
    )


def github_token():
    return get_secret(
        "GITHUB_TOKEN"
    ).strip()


# ============================================================
# GitHub API
# ============================================================

def github_request(
    method,
    path,
    payload=None,
    timeout=60,
):
    token = github_token()

    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is missing."
        )

    url = GITHUB_API + path

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "SHL-Persistent-Server",
    }

    data = None

    if payload is not None:
        data = json.dumps(
            payload
        ).encode("utf-8")

        headers["Content-Type"] = (
            "application/json"
        )

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout,
        ) as response:

            raw = response.read()

            if not raw:
                return {}

            return json.loads(
                raw.decode("utf-8")
            )

    except urllib.error.HTTPError as e:

        body = ""

        try:
            body = e.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            pass

        raise RuntimeError(
            f"GitHub API {e.code}: {body[:1000]}"
        )


def github_repo_info():
    return github_request(
        "GET",
        f"/repos/{GITHUB_REPO}",
    )


def github_branch_exists(branch):
    try:
        github_request(
            "GET",
            f"/repos/{GITHUB_REPO}/branches/"
            f"{branch}",
        )

        return True

    except Exception:
        return False


def github_default_branch_sha():
    info = github_repo_info()

    branch = info.get(
        "default_branch",
        GITHUB_BRANCH,
    )

    result = github_request(
        "GET",
        f"/repos/{GITHUB_REPO}/branches/{branch}",
    )

    return (
        result["commit"]["sha"],
        branch,
    )


def create_persistent_branch():
    if github_branch_exists(
        PERSISTENT_BRANCH
    ):
        return

    main_sha, _ = (
        github_default_branch_sha()
    )

    log.info(
        "Creating persistent branch %s",
        PERSISTENT_BRANCH,
    )

    github_request(
        "POST",
        f"/repos/{GITHUB_REPO}/git/refs",
        {
            "ref": f"refs/heads/{PERSISTENT_BRANCH}",
            "sha": main_sha,
        },
    )


def github_get_file(path):
    try:
        return github_request(
            "GET",
            f"/repos/{GITHUB_REPO}/contents/"
            f"{path}?ref={PERSISTENT_BRANCH}",
        )

    except Exception:
        return None


def github_put_file(
    path,
    content_bytes,
    message,
):
    encoded = base64.b64encode(
        content_bytes
    ).decode("ascii")

    existing = github_get_file(
        path
    )

    payload = {
        "message": message,
        "content": encoded,
        "branch": PERSISTENT_BRANCH,
        "committer": {
            "name": "SHL Persistent Server",
            "email": "shl@users.noreply.github.com",
        },
    }

    if existing and existing.get("sha"):
        payload["sha"] = existing["sha"]

    return github_request(
        "PUT",
        f"/repos/{GITHUB_REPO}/contents/"
        f"{path}",
        payload,
        timeout=120,
    )


def github_delete_file(
    path,
    message,
):
    existing = github_get_file(
        path
    )

    if not existing:
        return

    sha = existing.get("sha")

    if not sha:
        return

    github_request(
        "DELETE",
        f"/repos/{GITHUB_REPO}/contents/"
        f"{path}",
        {
            "message": message,
            "sha": sha,
            "branch": PERSISTENT_BRANCH,
            "committer": {
                "name": "SHL Persistent Server",
                "email": "shl@users.noreply.github.com",
            },
        },
    )


# ============================================================
# Ubuntu / PRoot download
# ============================================================

def download_file(
    url,
    destination,
):
    destination = Path(destination)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    log.info(
        "Downloading %s",
        url,
    )

    tmp = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    with urllib.request.urlopen(
        url,
        timeout=300,
    ) as response:

        with open(
            tmp,
            "wb",
        ) as f:

            while True:
                data = response.read(
                    1024 * 1024
                )

                if not data:
                    break

                f.write(data)

    tmp.replace(destination)


def install_proot():
    ensure_dirs()

    proot_path = PROOT_DIR / "proot"

    if proot_path.exists():
        proot_path.chmod(0o755)

        log.info(
            "PRoot already installed."
        )

        return proot_path

    log.info(
        "Downloading PRoot..."
    )

    download_file(
        PROOT_URL,
        proot_path,
    )

    proot_path.chmod(0o755)

    return proot_path


# ============================================================
# Ubuntu rootfs
# ============================================================

def rootfs_exists():
    return (
        ROOTFS_DIR.exists()
        and (ROOTFS_DIR / "bin").exists()
        and (ROOTFS_DIR / "etc").exists()
        and (ROOTFS_DIR / "usr").exists()
    )


def extract_ubuntu():
    ensure_dirs()

    if rootfs_exists():
        return

    archive = BASE_DIR / (
        "ubuntu-base.tar.gz"
    )

    log.info(
        "Ubuntu rootfs not found."
    )

    if not archive.exists():
        download_file(
            UBUNTU_URL,
            archive,
        )

    ROOTFS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log.info(
        "Extracting Ubuntu %s...",
        UBUNTU_VERSION,
    )

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:

        tar.extractall(
            ROOTFS_DIR
        )

    try:
        archive.unlink()
    except Exception:
        pass


# ============================================================
# PRoot execution
# ============================================================

def run_ubuntu(
    command,
    check=True,
    timeout=600,
    capture=True,
):
    install_proot()
    extract_ubuntu()

    env = os.environ.copy()

    env.update(
        {
            "HOME": "/root",
            "USER": "root",
            "LOGNAME": "root",
            "PATH": (
                "/usr/local/sbin:"
                "/usr/local/bin:"
                "/usr/sbin:"
                "/usr/bin:"
                "/sbin:"
                "/bin"
            ),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
        }
    )

    cmd = [
        str(PROOT_DIR / "proot"),
        "-0",
        "-r",
        str(ROOTFS_DIR),
        "-b",
        "/dev",
        "-b",
        "/dev/pts",
        "-b",
        "/proc",
        "-b",
        "/sys",
        "-b",
        "/etc/resolv.conf:/etc/resolv.conf",
        "-w",
        "/root",
        "/usr/bin/env",
        "-i",
        "HOME=/root",
        "USER=root",
        "LOGNAME=root",
        (
            "PATH=/usr/local/sbin:"
            "/usr/local/bin:"
            "/usr/sbin:"
            "/usr/bin:"
            "/sbin:"
            "/bin"
        ),
        "TERM=xterm-256color",
        "LANG=C.UTF-8",
        "/bin/bash",
        "-lc",
        command,
    ]

    log.info(
        "$ %s",
        " ".join(cmd),
    )

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )

    if capture:

        if result.stdout:
            for line in result.stdout.splitlines():
                log.info(
                    "UBUNTU: %s",
                    line,
                )

        if result.stderr:
            for line in result.stderr.splitlines():
                log.info(
                    "UBUNTU: %s",
                    line,
                )

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Ubuntu command failed: "
            f"{result.returncode}"
        )

    return result


# ============================================================
# Ubuntu configuration
# ============================================================

def configure_ubuntu():
    extract_ubuntu()

    etc = ROOTFS_DIR / "etc"

    hostname = etc / "hostname"
    hosts = etc / "hosts"
    resolv = etc / "resolv.conf"

    if not hostname.exists():
        hostname.write_text(
            "localhost\n",
            encoding="utf-8",
        )

    if not hosts.exists():
        hosts.write_text(
            "127.0.0.1 localhost\n"
            "127.0.1.1 localhost\n"
            "::1 localhost ip6-localhost ip6-loopback\n",
            encoding="utf-8",
        )

    # Do not replace existing resolv.conf.
    if not resolv.exists():
        try:
            resolv.write_text(
                "nameserver 1.1.1.1\n"
                "nameserver 8.8.8.8\n",
                encoding="utf-8",
            )
        except Exception:
            pass


# ============================================================
# Base packages
# ============================================================

def install_base_packages():
    extract_ubuntu()
    configure_ubuntu()

    if BASE_PACKAGES_MARKER.exists():
        log.info(
            "Base packages already installed."
        )
        return

    package_string = " ".join(
        BASE_PACKAGES
    )

    log.info(
        "Installing Ubuntu base packages..."
    )

    run_ubuntu(
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update && "
        f"apt-get install -y {package_string}",
        timeout=1800,
    )

    BASE_PACKAGES_MARKER.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    BASE_PACKAGES_MARKER.write_text(
        now_iso(),
        encoding="utf-8",
    )

    log.info(
        "Base packages installed."
    )


# ============================================================
# Ubuntu diagnostic
# ============================================================

def ubuntu_test():
    result = run_ubuntu(
        """
echo SHL_UBUNTU_OK
echo USER=$(id -un)
echo UID=$(id -u)
echo OS=$(grep PRETTY_NAME /etc/os-release)
echo ARCH=$(uname -m)
""".strip(),
        check=True,
        timeout=120,
    )

    return result.stdout


# ============================================================
# Snapshot exclusion rules
# ============================================================

EXCLUDED_TOP_LEVEL = {
    "proc",
    "sys",
    "dev",
    "run",
    "tmp",
    "mnt",
    "media",
}


EXCLUDED_RELATIVE = {
    "var/cache/apt",
    "var/lib/apt/lists",
    "var/cache/debconf",
    "var/log/journal",
}


def snapshot_filter(tarinfo):
    name = tarinfo.name.strip("/")

    if not name:
        return tarinfo

    parts = name.split("/")

    if parts[0] in EXCLUDED_TOP_LEVEL:
        return None

    for excluded in EXCLUDED_RELATIVE:

        if (
            name == excluded
            or name.startswith(
                excluded + "/"
            )
        ):
            return None

    # Skip socket files.
    if tarinfo.ischr() or tarinfo.isblk():
        return None

    if tarinfo.issock():
        return None

    return tarinfo


# ============================================================
# Snapshot creation
# ============================================================

def create_snapshot():
    ensure_dirs()

    snapshot = (
        BASE_DIR /
        "ubuntu-snapshot.tar.gz"
    )

    if snapshot.exists():
        snapshot.unlink()

    log.info(
        "Creating Ubuntu snapshot..."
    )

    with tarfile.open(
        snapshot,
        mode="w:gz",
        compresslevel=6,
    ) as tar:

        tar.add(
            ROOTFS_DIR,
            arcname=".",
            recursive=True,
            filter=snapshot_filter,
        )

    size = snapshot.stat().st_size

    sha = calculate_sha256(
        snapshot
    )

    log.info(
        "Snapshot created: %.2f MB",
        size / 1024 / 1024,
    )

    log.info(
        "Snapshot SHA256: %s",
        sha,
    )

    return snapshot, sha


# ============================================================
# GitHub persistent snapshot
# ============================================================

def chunk_file(path):
    chunks = []

    with open(
        path,
        "rb",
    ) as f:

        index = 0

        while True:

            data = f.read(
                CHUNK_SIZE
            )

            if not data:
                break

            chunks.append(
                (
                    index,
                    data,
                )
            )

            index += 1

    return chunks


def cleanup_old_snapshot_files():
    """
    Delete old snapshot chunk files from the
    persistent branch.

    Git history remains in GitHub, but the active
    branch only exposes the latest snapshot.
    """

    try:
        contents = github_request(
            "GET",
            f"/repos/{GITHUB_REPO}/contents/"
            f".shl/snapshots"
            f"?ref={PERSISTENT_BRANCH}",
        )

    except Exception:
        return

    if not isinstance(
        contents,
        list,
    ):
        return

    for item in contents:

        name = item.get(
            "name",
            "",
        )

        path = item.get(
            "path",
            "",
        )

        if not name.startswith(
            "ubuntu-"
        ):
            continue

        try:
            github_delete_file(
                path,
                "Remove old SHL snapshot",
            )

        except Exception as e:
            log.warning(
                "Could not delete %s: %s",
                path,
                e,
            )


def upload_snapshot(
    snapshot,
    sha,
):
    create_persistent_branch()

    log.info(
        "Preparing GitHub snapshot..."
    )

    chunks = chunk_file(
        snapshot
    )

    log.info(
        "Snapshot contains %d chunks.",
        len(chunks),
    )

    # Remove active old chunks first.
    cleanup_old_snapshot_files()

    uploaded = []

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y%m%d-%H%M%S"
    )

    for index, data in chunks:

        filename = (
            f"ubuntu-{timestamp}-"
            f"{index:05d}.part"
        )

        path = (
            ".shl/snapshots/"
            + filename
        )

        log.info(
            "Uploading chunk %d/%d: %s",
            index + 1,
            len(chunks),
            filename,
        )

        github_put_file(
            path,
            data,
            (
                f"SHL snapshot "
                f"{timestamp} "
                f"part {index + 1}/"
                f"{len(chunks)}"
            ),
        )

        uploaded.append(
            {
                "path": path,
                "index": index,
                "size": len(data),
            }
        )

    manifest = {
        "format": 1,
        "created_at": now_iso(),
        "repo": GITHUB_REPO,
        "branch": PERSISTENT_BRANCH,
        "source_branch": GITHUB_BRANCH,
        "snapshot_sha256": sha,
        "snapshot_size": snapshot.stat().st_size,
        "chunk_size": CHUNK_SIZE,
        "chunks": uploaded,
        "ubuntu": UBUNTU_VERSION,
    }

    github_put_file(
        ".shl/manifest.json",
        json.dumps(
            manifest,
            indent=2,
        ).encode("utf-8"),
        (
            "SHL persistent snapshot "
            "manifest"
        ),
    )

    state = {
        "last_backup": now_iso(),
        "snapshot_sha256": sha,
        "snapshot_size": snapshot.stat().st_size,
        "chunks": len(chunks),
        "branch": PERSISTENT_BRANCH,
    }

    atomic_write_json(
        BACKUP_STATE_FILE,
        state,
    )

    log.info(
        "Persistent GitHub backup completed."
    )

    return manifest


# ============================================================
# Restore from GitHub
# ============================================================

def download_github_file(
    path,
):
    data = github_request(
        "GET",
        f"/repos/{GITHUB_REPO}/contents/"
        f"{path}?ref={PERSISTENT_BRANCH}",
    )

    if data.get("encoding") != "base64":
        raise RuntimeError(
            "Unexpected GitHub file encoding."
        )

    content = data.get(
        "content",
        "",
    )

    content = (
        content
        .replace("\n", "")
        .replace("\r", "")
    )

    return base64.b64decode(
        content
    )


def restore_snapshot():
    if not github_token():
        log.warning(
            "GITHUB_TOKEN missing; "
            "cannot restore persistent state."
        )

        return False

    if not github_branch_exists(
        PERSISTENT_BRANCH
    ):
        log.info(
            "No persistent state branch yet."
        )

        return False

    manifest_data = github_get_file(
        ".shl/manifest.json"
    )

    if not manifest_data:
        log.info(
            "No persistent manifest yet."
        )

        return False

    try:
        manifest_raw = base64.b64decode(
            manifest_data["content"]
            .replace("\n", "")
        )

        manifest = json.loads(
            manifest_raw.decode(
                "utf-8"
            )
        )

    except Exception as e:
        log.error(
            "Invalid persistent manifest: %s",
            e,
        )

        return False

    chunks = manifest.get(
        "chunks",
        [],
    )

    if not chunks:
        log.warning(
            "Persistent manifest has no chunks."
        )

        return False

    ensure_dirs()

    archive = (
        BASE_DIR /
        "restored-ubuntu.tar.gz"
    )

    if archive.exists():
        archive.unlink()

    log.info(
        "Restoring persistent Ubuntu from GitHub..."
    )

    chunks = sorted(
        chunks,
        key=lambda x: x["index"],
    )

    with open(
        archive,
        "wb",
    ) as out:

        for i, chunk in enumerate(
            chunks
        ):

            path = chunk["path"]

            log.info(
                "Downloading chunk %d/%d",
                i + 1,
                len(chunks),
            )

            data = download_github_file(
                path
            )

            out.write(data)

    actual_sha = calculate_sha256(
        archive
    )

    expected_sha = manifest.get(
        "snapshot_sha256"
    )

    if (
        expected_sha
        and actual_sha != expected_sha
    ):
        archive.unlink(
            missing_ok=True
        )

        raise RuntimeError(
            "Persistent snapshot SHA256 "
            "does not match."
        )

    # Never restore directly over the current
    # rootfs. Build a new one first.
    restored = (
        RESTORE_DIR /
        "ubuntu"
    )

    if restored.exists():
        shutil.rmtree(
            restored,
            ignore_errors=True,
        )

    restored.mkdir(
        parents=True,
        exist_ok=True,
    )

    log.info(
        "Extracting persistent Ubuntu..."
    )

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:

        tar.extractall(
            restored
        )

    # Basic integrity check.
    if not (
        (restored / "etc").exists()
        and (restored / "usr").exists()
        and (restored / "bin").exists()
    ):
        raise RuntimeError(
            "Restored Ubuntu rootfs is invalid."
        )

    if ROOTFS_DIR.exists():
        shutil.rmtree(
            ROOTFS_DIR,
            ignore_errors=True,
        )

    restored.rename(
        ROOTFS_DIR
    )

    archive.unlink(
        missing_ok=True
    )

    log.info(
        "Persistent Ubuntu restored successfully."
    )

    return True


# ============================================================
# Backup controller
# ============================================================

class BackupController:

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.pending = False
        self.last_backup_time = 0
        self.thread = None

    def request(self, reason="change"):
        with self.lock:
            self.pending = True

        if self.thread is None or not self.thread.is_alive():

            self.thread = threading.Thread(
                target=self.worker,
                args=(reason,),
                daemon=True,
            )

            self.thread.start()

    def worker(self, reason):
        time.sleep(
            BACKUP_DEBOUNCE
        )

        while True:

            with self.lock:

                if self.running:
                    return

                self.running = True
                self.pending = False

            try:

                now = time.time()

                wait = (
                    MIN_BACKUP_INTERVAL
                    - (
                        now
                        - self.last_backup_time
                    )
                )

                if wait > 0:
                    time.sleep(
                        wait
                    )

                backup_now(
                    reason=reason
                )

                self.last_backup_time = (
                    time.time()
                )

            except Exception:
                log.exception(
                    "Automatic backup failed."
                )

            finally:

                with self.lock:
                    self.running = False

            with self.lock:

                if not self.pending:
                    break

                self.pending = False

                reason = "queued-change"

    def stop(self):
        with self.lock:
            self.pending = False


BACKUP_CONTROLLER = BackupController()


# ============================================================
# Backup
# ============================================================

BACKUP_MUTEX = threading.Lock()


def backup_now(reason="manual"):
    if not github_token():
        raise RuntimeError(
            "GITHUB_TOKEN is missing."
        )

    with BACKUP_MUTEX:

        log.info(
            "Starting persistent GitHub backup: %s",
            reason,
        )

        snapshot, sha = (
            create_snapshot()
        )

        try:
            manifest = upload_snapshot(
                snapshot,
                sha,
            )

            return manifest

        finally:

            try:
                snapshot.unlink()
            except Exception:
                pass


# ============================================================
# Filesystem watcher
# ============================================================

def start_filesystem_watcher():
    """
    Uses watchdog when available.

    The watcher watches the Ubuntu rootfs and
    schedules a GitHub snapshot after changes.
    """

    try:
        from watchdog.observers import Observer
        from watchdog.events import (
            FileSystemEventHandler,
        )

    except Exception as e:
        log.warning(
            "watchdog unavailable: %s",
            e,
        )

        return None

    class Handler(
        FileSystemEventHandler
    ):

        def _changed(self, event):

            if event.is_directory:
                return

            path = str(
                getattr(
                    event,
                    "src_path",
                    "",
                )
            )

            if not path:
                return

            # Ignore temporary/cache areas.
            ignored = (
                "/proc/",
                "/sys/",
                "/dev/",
                "/run/",
                "/tmp/",
                "/var/cache/apt/",
                "/var/lib/apt/lists/",
            )

            if any(
                item in path
                for item in ignored
            ):
                return

            BACKUP_CONTROLLER.request(
                reason="filesystem-change"
            )

        on_created = _changed
        on_modified = _changed
        on_moved = _changed
        on_deleted = _changed

    observer = Observer()

    observer.schedule(
        Handler(),
        str(ROOTFS_DIR),
        recursive=True,
    )

    observer.daemon = True
    observer.start()

    log.info(
        "Filesystem watcher started."
    )

    return observer


# ============================================================
# SSHX
# ============================================================

def sshx_running():
    if not SSHX_PID_FILE.exists():
        return False

    try:
        pid = int(
            SSHX_PID_FILE.read_text().strip()
        )

        os.kill(
            pid,
            0,
        )

        return True

    except ProcessLookupError:

        try:
            SSHX_PID_FILE.unlink()
        except Exception:
            pass

        return False

    except Exception:
        return False


def install_sshx():
    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if SSHX_PATH.exists():
        SSHX_PATH.chmod(
            0o755
        )

        return

    log.info(
        "Installing SSHX..."
    )

    installer = (
        BASE_DIR /
        "sshx-install.sh"
    )

    download_file(
        SSHX_INSTALLER,
        installer,
    )

    installer.chmod(
        0o755
    )

    subprocess.run(
        [
            "bash",
            str(installer),
        ],
        check=True,
        timeout=300,
    )

    # sshx installer normally places the binary
    # into ~/.local/bin.
    if not SSHX_PATH.exists():

        candidates = [
            Path.home()
            / ".local"
            / "bin"
            / "sshx",

            Path("/usr/local/bin/sshx"),

            Path("/usr/bin/sshx"),
        ]

        for candidate in candidates:

            if candidate.exists():

                if candidate != SSHX_PATH:

                    shutil.copy2(
                        candidate,
                        SSHX_PATH,
                    )

                break

    if not SSHX_PATH.exists():
        raise RuntimeError(
            "SSHX installation failed."
        )

    SSHX_PATH.chmod(
        0o755
    )


def start_sshx():
    install_sshx()

    if sshx_running():

        log.info(
            "SSHX already running: PID=%s",
            SSHX_PID_FILE.read_text().strip(),
        )

        return

    lock = (
        STATE_DIR /
        "sshx-start.lock"
    )

    try:
        fd = os.open(
            lock,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY,
        )

        os.close(fd)

    except FileExistsError:

        log.info(
            "Another SSHX startup is already running."
        )

        return

    try:

        if sshx_running():
            return

        log_file = open(
            SSHX_LOG_FILE,
            "a",
            buffering=1,
        )

        proc = subprocess.Popen(
            [
                str(SSHX_PATH),
                "run",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        SSHX_PID_FILE.write_text(
            str(proc.pid),
            encoding="utf-8",
        )

        log.info(
            "SSHX started. PID=%s",
            proc.pid,
        )

        def detect_link():

            for _ in range(60):

                time.sleep(1)

                try:

                    text = (
                        SSHX_LOG_FILE.read_text(
                            errors="ignore"
                        )
                    )

                    for line in text.splitlines():

                        match = re.search(
                            r"https://[^\s]+",
                            line,
                        )

                        if not match:
                            continue

                        link = match.group(
                            0
                        ).rstrip(
                            ".,)"
                        )

                        SSHX_LINK_FILE.write_text(
                            link,
                            encoding="utf-8",
                        )

                        log.info(
                            "SSHX link detected: %s",
                            link,
                        )

                        return

                except Exception:
                    pass

        threading.Thread(
            target=detect_link,
            daemon=True,
        ).start()

    finally:

        try:
            lock.unlink()
        except Exception:
            pass


# ============================================================
# Persistent service definitions
# ============================================================

def service_definitions():
    SERVICE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    result = []

    for file in SERVICE_DIR.glob(
        "*.json"
    ):

        data = load_json(
            file
        )

        if isinstance(
            data,
            dict,
        ):
            result.append(
                data
            )

    return result


def create_shlctl():
    path = (
        ROOTFS_DIR /
        "usr/local/bin/shlctl"
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    script = r'''#!/bin/bash

set -e

SERVICE_DIR="/etc/shl/services"
RUN_DIR="/run/shl"

mkdir -p "$SERVICE_DIR"
mkdir -p "$RUN_DIR"

usage() {
    echo "shlctl list"
    echo "shlctl start NAME"
    echo "shlctl stop NAME"
    echo "shlctl restart NAME"
    echo "shlctl status NAME"
}

get_cmd() {
    local name="$1"
    local file="$SERVICE_DIR/$name.json"

    if [ ! -f "$file" ]; then
        echo "Service not found: $name"
        exit 1
    fi

    python3 - "$file" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    data=json.load(f)

print(data.get("command",""))
PY
}

case "${1:-}" in

    list)
        find "$SERVICE_DIR" -maxdepth 1 \
            -type f -name '*.json' \
            -printf '%f\n' |
            sed 's/\.json$//'
        ;;

    start)
        NAME="${2:-}"

        if [ -z "$NAME" ]; then
            usage
            exit 1
        fi

        CMD="$(get_cmd "$NAME")"

        if [ -z "$CMD" ]; then
            echo "No command"
            exit 1
        fi

        mkdir -p "$RUN_DIR"

        nohup bash -lc "$CMD" \
            >"$RUN_DIR/$NAME.log" 2>&1 &

        echo $! >"$RUN_DIR/$NAME.pid"

        echo "Started $NAME"
        ;;

    stop)
        NAME="${2:-}"
        PID="$RUN_DIR/$NAME.pid"

        if [ -f "$PID" ]; then
            kill "$(cat "$PID")" 2>/dev/null || true
            rm -f "$PID"
        fi

        echo "Stopped $NAME"
        ;;

    restart)
        "$0" stop "$2" || true
        sleep 1
        "$0" start "$2"
        ;;

    status)
        NAME="${2:-}"
        PID="$RUN_DIR/$NAME.pid"

        if [ -f "$PID" ] &&
           kill -0 "$(cat "$PID")" 2>/dev/null
        then
            echo "$NAME: running PID=$(cat "$PID")"
        else
            echo "$NAME: stopped"
        fi
        ;;

    *)
        usage
        exit 1
        ;;
esac
'''

    path.write_text(
        script,
        encoding="utf-8",
    )

    path.chmod(
        0o755
    )


def start_persistent_services():
    create_shlctl()

    services = service_definitions()

    for service in services:

        if not service.get(
            "enabled",
            True,
        ):
            continue

        name = service.get(
            "name"
        )

        command = service.get(
            "command"
        )

        if not name or not command:
            continue

        log.info(
            "Starting persistent service: %s",
            name,
        )

        try:

            run_dir = (
                ROOTFS_DIR /
                "run/shl"
            )

            run_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            pid_file = (
                run_dir /
                f"{name}.pid"
            )

            if pid_file.exists():

                try:

                    pid = int(
                        pid_file.read_text()
                    )

                    os.kill(
                        pid,
                        0,
                    )

                    continue

                except Exception:
                    pass

            log_file = (
                run_dir /
                f"{name}.log"
            )

            command_line = (
                "nohup bash -lc "
                + repr(command)
                + " >"
                + repr(str(log_file))
                + " 2>&1 & echo $! >"
                + repr(str(pid_file))
            )

            run_ubuntu(
                command_line,
                check=False,
                timeout=60,
            )

        except Exception:
            log.exception(
                "Could not start service %s",
                name,
            )


# ============================================================
# Persistent bootstrap
# ============================================================

_BOOTSTRAPPED = False
_WATCHER = None


def bootstrap():
    global _BOOTSTRAPPED
    global _WATCHER

    if _BOOTSTRAPPED:
        return

    ensure_dirs()

    log.info(
        "========================================"
    )

    log.info(
        "SHL persistent Ubuntu bootstrap"
    )

    log.info(
        "========================================"
    )

    token = github_token()

    restored = False

    if token:

        try:
            restored = restore_snapshot()

        except Exception:
            log.exception(
                "Persistent restore failed."
            )

    else:

        log.warning(
            "GITHUB_TOKEN is missing."
        )

    if not restored:

        extract_ubuntu()
        configure_ubuntu()
        install_base_packages()

    else:

        log.info(
            "Persistent Ubuntu restored; "
            "skipping base package installation."
        )

    create_shlctl()

    try:
        ubuntu_test()
    except Exception:
        log.exception(
            "Ubuntu test failed."
        )

    try:
        start_sshx()
    except Exception:
        log.exception(
            "SSHX startup failed."
        )

    try:
        start_persistent_services()
    except Exception:
        log.exception(
            "Persistent service startup failed."
        )

    if token:

        try:
            # If there is no persistent state yet,
            # create the first snapshot.
            if not github_branch_exists(
                PERSISTENT_BRANCH
            ):

                log.info(
                    "No persistent state branch yet."
                )

                backup_now(
                    reason="initial"
                )

        except Exception:
            log.exception(
                "Initial persistent backup failed."
            )

        try:

            _WATCHER = (
                start_filesystem_watcher()
            )

        except Exception:
            log.exception(
                "Filesystem watcher failed."
            )

    else:

        log.warning(
            "GitHub persistence is disabled "
            "because GITHUB_TOKEN is missing."
        )

    _BOOTSTRAPPED = True

    log.info(
        "SHL Ubuntu environment is ready."
    )


# ============================================================
# Shutdown
# ============================================================

_SHUTDOWN_DONE = False


def shutdown_handler(
    signum=None,
    frame=None,
):
    global _SHUTDOWN_DONE

    if _SHUTDOWN_DONE:
        return

    _SHUTDOWN_DONE = True

    log.info(
        "Shutdown detected. "
        "Creating final persistent backup..."
    )

    try:

        if github_token():

            backup_now(
                reason="shutdown"
            )

        else:

            log.warning(
                "Final backup skipped: "
                "GITHUB_TOKEN missing."
            )

    except Exception:
        log.exception(
            "Final persistent backup failed."
        )

    if signum is not None:
        raise SystemExit(0)


try:
    signal.signal(
        signal.SIGTERM,
        shutdown_handler,
    )

    signal.signal(
        signal.SIGINT,
        shutdown_handler,
    )

except Exception:
    pass


# ============================================================
# Streamlit UI
# ============================================================

st.set_page_config(
    page_title="SHL Persistent Ubuntu",
    page_icon="🖥️",
    layout="wide",
)


# Bootstrap before UI.
try:
    bootstrap()

except Exception as e:

    log.exception(
        "Bootstrap failed."
    )

    st.error(
        f"SHL bootstrap failed: {e}"
    )


st.title(
    "🖥️ SHL Persistent Ubuntu Server"
)

st.caption(
    f"GitHub persistence: "
    f"{GITHUB_REPO} → {PERSISTENT_BRANCH}"
)


# ============================================================
# Status
# ============================================================

col1, col2, col3, col4 = st.columns(4)


with col1:

    if rootfs_exists():
        st.success(
            "Ubuntu: READY"
        )
    else:
        st.error(
            "Ubuntu: MISSING"
        )


with col2:

    if github_token():
        st.success(
            "GitHub: CONNECTED"
        )
    else:
        st.error(
            "GitHub: TOKEN MISSING"
        )


with col3:

    if sshx_running():
        st.success(
            "SSHX: RUNNING"
        )
    else:
        st.warning(
            "SSHX: STOPPED"
        )


with col4:

    if github_branch_exists(
        PERSISTENT_BRANCH
    ):
        st.success(
            "Backup: ENABLED"
        )
    else:
        st.warning(
            "Backup: NOT CREATED"
        )


# ============================================================
# Manual backup
# ============================================================

st.divider()

st.subheader(
    "💾 Persistent Backup"
)

c1, c2 = st.columns(2)


with c1:

    if st.button(
        "💾 Backup Now",
        use_container_width=True,
    ):

        with st.spinner(
            "Creating GitHub snapshot..."
        ):

            try:

                manifest = backup_now(
                    reason="manual"
                )

                st.success(
                    "Backup completed successfully."
                )

                st.json(
                    {
                        "branch": PERSISTENT_BRANCH,
                        "sha256": manifest.get(
                            "snapshot_sha256"
                        ),
                        "size_mb": round(
                            manifest.get(
                                "snapshot_size",
                                0,
                            )
                            / 1024
                            / 1024,
                            2,
                        ),
                        "chunks": len(
                            manifest.get(
                                "chunks",
                                [],
                            )
                        ),
                    }
                )

            except Exception as e:

                st.error(
                    f"Backup failed: {e}"
                )


with c2:

    state = load_json(
        BACKUP_STATE_FILE,
        {},
    )

    if state:

        st.write(
            "Last backup:"
        )

        st.code(
            state.get(
                "last_backup",
                "unknown",
            )
        )

        st.write(
            f"Chunks: "
            f"{state.get('chunks', '?')}"
        )

    else:

        st.info(
            "No local backup state yet."
        )


# ============================================================
# SSHX
# ============================================================

st.divider()

st.subheader(
    "🔐 SSHX"
)

if sshx_running():

    pid = SSHX_PID_FILE.read_text(
        encoding="utf-8"
    ).strip()

    st.success(
        f"SSHX running — PID {pid}"
    )

else:

    st.warning(
        "SSHX is not running."
    )

if SSHX_LINK_FILE.exists():

    link = SSHX_LINK_FILE.read_text(
        encoding="utf-8"
    ).strip()

    if link:

        st.markdown(
            f"### SSHX Link\n"
            f"`{link}`"
        )


with st.expander(
    "SSHX log"
):

    if SSHX_LOG_FILE.exists():

        text = SSHX_LOG_FILE.read_text(
            errors="replace"
        )

        st.code(
            text[-12000:]
        )

    else:

        st.info(
            "No SSHX log."
        )


# ============================================================
# Ubuntu terminal
# ============================================================

st.divider()

st.subheader(
    "🐧 Ubuntu"
)

command = st.text_input(
    "Ubuntu command",
    value="neofetch",
)


if st.button(
    "▶ Run command",
    use_container_width=True,
):

    try:

        result = run_ubuntu(
            command,
            check=False,
            timeout=300,
        )

        output = ""

        if result.stdout:
            output += result.stdout

        if result.stderr:
            output += "\n" + result.stderr

        st.code(
            output
        )

        # Command may have changed the filesystem.
        BACKUP_CONTROLLER.request(
            reason="terminal-command"
        )

    except Exception as e:

        st.error(
            f"Command failed: {e}"
        )


# ============================================================
# Package status
# ============================================================

st.divider()

st.subheader(
    "📦 Package Test"
)

if st.button(
    "Check installed packages",
):

    try:

        result = run_ubuntu(
            "command -v neofetch; "
            "command -v python3; "
            "command -v git; "
            "command -v curl; "
            "python3 --version",
            check=False,
            timeout=60,
        )

        st.code(
            result.stdout
            + "\n"
            + result.stderr
        )

    except Exception as e:

        st.error(
            str(e)
        )


# ============================================================
# Persistent services
# ============================================================

st.divider()

st.subheader(
    "⚙️ Persistent Services"
)

services = service_definitions()

if not services:

    st.info(
        "No persistent services configured."
    )

else:

    for service in services:

        name = service.get(
            "name",
            "unknown",
        )

        command = service.get(
            "command",
            "",
        )

        enabled = service.get(
            "enabled",
            True,
        )

        with st.expander(
            name
        ):

            st.write(
                f"Enabled: {enabled}"
            )

            st.code(
                command
            )


# ============================================================
# GitHub persistence information
# ============================================================

st.divider()

st.subheader(
    "🐙 GitHub Persistence"
)

st.write(
    f"Repository: `{GITHUB_REPO}`"
)

st.write(
    f"Application branch: `{GITHUB_BRANCH}`"
)

st.write(
    f"Persistent branch: `{PERSISTENT_BRANCH}`"
)

manifest = github_get_file(
    ".shl/manifest.json"
)

if manifest:

    try:

        data = base64.b64decode(
            manifest["content"]
            .replace("\n", "")
        )

        parsed = json.loads(
            data.decode("utf-8")
        )

        st.json(
            {
                "created_at": parsed.get(
                    "created_at"
                ),
                "snapshot_size_mb": round(
                    parsed.get(
                        "snapshot_size",
                        0,
                    )
                    / 1024
                    / 1024,
                    2,
                ),
                "chunks": len(
                    parsed.get(
                        "chunks",
                        [],
                    )
                ),
                "sha256": parsed.get(
                    "snapshot_sha256"
                ),
            }
        )

    except Exception as e:

        st.warning(
            f"Could not read manifest: {e}"
        )

else:

    st.warning(
        "No persistent GitHub snapshot exists yet."
    )


# ============================================================
# Architecture / environment
# ============================================================

st.divider()

st.subheader(
    "🖥️ Environment"
)

try:

    architecture = (
        os.uname().machine
    )

except Exception:

    architecture = "unknown"


st.code(
    "\n".join(
        [
            f"Architecture: {architecture}",
            f"Ubuntu rootfs: {ROOTFS_DIR}",
            f"PRoot: {PROOT_DIR / 'proot'}",
            f"GitHub repo: {GITHUB_REPO}",
            f"Persistent branch: {PERSISTENT_BRANCH}",
        ]
    )
)


# ============================================================
# Important information
# ============================================================

st.divider()

st.info(
    """
Persistence model:

1. Ubuntu is stored under /tmp/shl-runtime/ubuntu.
2. Changes are detected by the filesystem watcher.
3. After changes, a compressed Ubuntu snapshot is created.
4. The snapshot is split into GitHub-safe chunks.
5. Chunks are uploaded to the shl-persistent branch.
6. manifest.json records the snapshot.
7. After a Streamlit runtime rebuild, the snapshot is downloaded.
8. Ubuntu is reconstructed before PRoot starts.
9. Installed packages, /root files, /etc configuration,
   scripts and other persistent Ubuntu files are restored.

The operating-system process itself cannot survive a
Streamlit runtime destruction. The filesystem/state can
be reconstructed automatically from GitHub.
"""
)
