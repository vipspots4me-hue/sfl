import os
import re
import io
import sys
import json
import time
import base64
import shutil
import signal
import hashlib
import logging
import tarfile
import tempfile
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

import requests
import streamlit as st

try:
    import fcntl
except ImportError:
    fcntl = None


# ============================================================
# SHL - STREAMLIT PERSISTENT UBUNTU
# ============================================================

APP_NAME = "SHL"
APP_VERSION = "3.0"

BASE_DIR = Path("/tmp/shl-runtime")
ROOTFS_DIR = BASE_DIR / "ubuntu"
PROOT_DIR = BASE_DIR / "proot"
RESTORE_DIR = BASE_DIR / "restore"
STATE_DIR = BASE_DIR / "state"

SSHX_DIR = ROOTFS_DIR / "root/.local/bin"
SSHX_PATH = SSHX_DIR / "sshx"

SSHX_PID_FILE = STATE_DIR / "sshx.pid"
SSHX_LOG_FILE = STATE_DIR / "sshx.log"
SSHX_LOCK_FILE = STATE_DIR / "sshx.lock"

BOOTSTRAP_LOCK_FILE = STATE_DIR / "bootstrap.lock"
BACKUP_LOCK_FILE = STATE_DIR / "backup.lock"

BACKUP_STATE_FILE = STATE_DIR / "backup-state.json"
WATCHER_STATE_FILE = STATE_DIR / "watcher-state.json"

SNAPSHOT_FILE = BASE_DIR / "ubuntu-snapshot.tar.gz"

SERVICE_DIR = ROOTFS_DIR / "etc/shl/services"

UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/download/"
    "proot-amd64"
)

SSHX_BINARY_URL = (
    "https://s3.amazonaws.com/sshx/"
    "sshx-x86_64-unknown-linux-musl.tar.gz"
)

GITHUB_API = "https://api.github.com"

DEFAULT_GITHUB_REPO = "vipspots4me-hue/sfl"
DEFAULT_GITHUB_BRANCH = "main"
DEFAULT_PERSISTENT_BRANCH = "shl-persistent"

PERSISTENT_BRANCH = os.environ.get(
    "GITHUB_STATE_BRANCH",
    DEFAULT_PERSISTENT_BRANCH,
)

CHUNK_SIZE = 45 * 1024 * 1024

AUTO_BACKUP_INTERVAL = 15 * 60

REQUEST_TIMEOUT = 120

VOLATILE_TOP_LEVEL = {
    "proc",
    "sys",
    "dev",
    "run",
    "tmp",
    "mnt",
    "media",
}


# ============================================================
# LOGGING
# ============================================================

LOG_FILE = STATE_DIR / "shl.log"

STATE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[SHL] %(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(APP_NAME)


def log(message):
    logger.info(message)


# ============================================================
# DIRECTORIES
# ============================================================

def ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PROOT_DIR.mkdir(parents=True, exist_ok=True)
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)


ensure_dirs()


# ============================================================
# SECRETS
# ============================================================

def get_secret(name, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass

    return os.environ.get(name, default)


GITHUB_TOKEN = get_secret("GITHUB_TOKEN")
GITHUB_REPO = get_secret(
    "GITHUB_REPO",
    DEFAULT_GITHUB_REPO,
)
GITHUB_BRANCH = get_secret(
    "GITHUB_BRANCH",
    DEFAULT_GITHUB_BRANCH,
)

if get_secret("GITHUB_STATE_BRANCH"):
    PERSISTENT_BRANCH = get_secret("GITHUB_STATE_BRANCH")


# ============================================================
# FILE LOCK
# ============================================================

class FileLock:
    def __init__(self, path, blocking=True):
        self.path = Path(path)
        self.blocking = blocking
        self.fp = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.fp = open(self.path, "a+")

        if fcntl is not None:
            flags = fcntl.LOCK_EX

            if not self.blocking:
                flags |= fcntl.LOCK_NB

            try:
                fcntl.flock(self.fp.fileno(), flags)
            except BlockingIOError:
                self.fp.close()
                self.fp = None
                raise

        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fp:
            try:
                if fcntl is not None:
                    fcntl.flock(
                        self.fp.fileno(),
                        fcntl.LOCK_UN,
                    )
            finally:
                self.fp.close()

        self.fp = None


# ============================================================
# COMMAND HELPERS
# ============================================================

def run_command(
    cmd,
    check=True,
    timeout=120,
    cwd=None,
    env=None,
):
    log("$ " + " ".join(map(str, cmd)))

    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )

    if result.stdout:
        for line in result.stdout.rstrip().splitlines():
            log(line)

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
        )

    return result


def sha256_file(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(url, destination, timeout=300):
    destination = Path(destination)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    log(f"Downloading {url}")

    with requests.get(
        url,
        stream=True,
        timeout=timeout,
        headers={
            "User-Agent": "SHL/3.0",
        },
    ) as response:

        response.raise_for_status()

        total = int(
            response.headers.get(
                "content-length",
                "0",
            )
        )

        downloaded = 0

        with open(destination, "wb") as f:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if not chunk:
                    continue

                f.write(chunk)
                downloaded += len(chunk)

                if total:
                    percent = downloaded * 100 / total

                    if (
                        downloaded == len(chunk)
                        or downloaded % (20 * 1024 * 1024)
                        < len(chunk)
                    ):
                        log(
                            f"Download progress: "
                            f"{percent:.1f}%"
                        )

    return destination


# ============================================================
# GITHUB API
# ============================================================

def github_headers():
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing."
        )

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SHL-Persistent/3.0",
    }


def github_request(
    method,
    path,
    payload=None,
    timeout=120,
):
    url = GITHUB_API + path

    response = requests.request(
        method,
        url,
        headers=github_headers(),
        json=payload,
        timeout=timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"GitHub API {method} {path} failed: "
            f"{response.status_code} "
            f"{response.text[:1000]}"
        )

    if not response.content:
        return None

    return response.json()


def github_repo_exists():
    try:
        github_request(
            "GET",
            f"/repos/{GITHUB_REPO}",
        )
        return True
    except Exception:
        return False


def github_branch_exists(branch):
    try:
        github_request(
            "GET",
            f"/repos/{GITHUB_REPO}/git/ref/heads/{branch}",
        )
        return True
    except Exception:
        return False


def github_default_branch_sha():
    data = github_request(
        "GET",
        f"/repos/{GITHUB_REPO}/git/ref/heads/{GITHUB_BRANCH}",
    )

    return data["object"]["sha"]


def create_persistent_branch():
    if github_branch_exists(PERSISTENT_BRANCH):
        return

    log(
        f"Creating persistent branch: "
        f"{PERSISTENT_BRANCH}"
    )

    sha = github_default_branch_sha()

    github_request(
        "POST",
        f"/repos/{GITHUB_REPO}/git/refs",
        {
            "ref": f"refs/heads/{PERSISTENT_BRANCH}",
            "sha": sha,
        },
    )


def github_get_file(path, branch=PERSISTENT_BRANCH):
    try:
        return github_request(
            "GET",
            f"/repos/{GITHUB_REPO}/contents/{path}"
            f"?ref={branch}",
        )
    except Exception:
        return None


def github_put_file(
    path,
    content,
    message,
    branch=PERSISTENT_BRANCH,
    sha=None,
):
    if isinstance(content, str):
        raw = content.encode()
    else:
        raw = content

    encoded = base64.b64encode(raw).decode()

    payload = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }

    if sha:
        payload["sha"] = sha

    return github_request(
        "PUT",
        f"/repos/{GITHUB_REPO}/contents/{path}",
        payload,
        timeout=300,
    )


def github_delete_file(
    path,
    message,
    branch=PERSISTENT_BRANCH,
):
    current = github_get_file(
        path,
        branch,
    )

    if not current:
        return

    github_request(
        "DELETE",
        f"/repos/{GITHUB_REPO}/contents/{path}",
        {
            "message": message,
            "sha": current["sha"],
            "branch": branch,
        },
    )


# ============================================================
# MANIFEST
# ============================================================

MANIFEST_PATH = ".shl/manifest.json"


def load_manifest():
    if not GITHUB_TOKEN:
        return None

    data = github_get_file(
        MANIFEST_PATH,
        PERSISTENT_BRANCH,
    )

    if not data:
        return None

    try:
        raw = base64.b64decode(
            data["content"]
        )

        return json.loads(
            raw.decode("utf-8")
        )

    except Exception as exc:
        log(
            f"Cannot parse manifest: {exc}"
        )

        return None


# ============================================================
# SNAPSHOT FILTER
# ============================================================

def snapshot_filter(tarinfo):
    """
    IMPORTANT:
    TarInfo has no issock() method.
    """

    name = tarinfo.name.strip("/")

    if not name:
        return tarinfo

    first = name.split("/", 1)[0]

    if first in VOLATILE_TOP_LEVEL:
        return None

    if (
        name == "var/cache/apt"
        or name.startswith("var/cache/apt/")
    ):
        return None

    if (
        name == "var/lib/apt/lists"
        or name.startswith("var/lib/apt/lists/")
    ):
        return None

    if tarinfo.ischr():
        return None

    if tarinfo.isblk():
        return None

    return tarinfo


# ============================================================
# CREATE SNAPSHOT
# ============================================================

def create_snapshot():
    if not ROOTFS_DIR.exists():
        raise RuntimeError(
            "Ubuntu rootfs does not exist."
        )

    log("Creating Ubuntu snapshot...")

    SNAPSHOT_FILE.unlink(
        missing_ok=True
    )

    started = time.time()

    with tarfile.open(
        SNAPSHOT_FILE,
        "w:gz",
        compresslevel=6,
    ) as tar:

        tar.add(
            ROOTFS_DIR,
            arcname=".",
            recursive=True,
            filter=snapshot_filter,
        )

    size = SNAPSHOT_FILE.stat().st_size

    elapsed = time.time() - started

    digest = sha256_file(
        SNAPSHOT_FILE
    )

    log(
        f"Ubuntu snapshot created: "
        f"{size / 1024 / 1024:.2f} MB"
    )

    log(
        f"Snapshot SHA256: {digest}"
    )

    log(
        f"Snapshot time: {elapsed:.1f}s"
    )

    return SNAPSHOT_FILE


# ============================================================
# UPLOAD SNAPSHOT
# ============================================================

def upload_snapshot(snapshot):
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing."
        )

    create_persistent_branch()

    snapshot_id = datetime.now(
        timezone.utc
    ).strftime(
        "%Y%m%d-%H%M%S"
    )

    snapshot_dir = (
        f".shl/snapshots/{snapshot_id}"
    )

    size = snapshot.stat().st_size

    total_parts = (
        size + CHUNK_SIZE - 1
    ) // CHUNK_SIZE

    digest = sha256_file(
        snapshot
    )

    log(
        f"Uploading snapshot "
        f"{snapshot_id}"
    )

    log(
        f"Snapshot size: "
        f"{size / 1024 / 1024:.2f} MB"
    )

    log(
        f"Parts: {total_parts}"
    )

    parts = []

    with open(snapshot, "rb") as f:

        for index in range(total_parts):

            data = f.read(
                CHUNK_SIZE
            )

            if not data:
                break

            filename = (
                f"part-{index:05d}"
            )

            path = (
                f"{snapshot_dir}/{filename}"
            )

            log(
                f"Uploading "
                f"{index + 1}/{total_parts}: "
                f"{filename}"
            )

            github_put_file(
                path,
                data,
                (
                    f"SHL snapshot "
                    f"{snapshot_id} "
                    f"part {index + 1}/{total_parts}"
                ),
                branch=PERSISTENT_BRANCH,
            )

            parts.append(
                {
                    "index": index,
                    "path": path,
                    "size": len(data),
                    "sha256": hashlib.sha256(
                        data
                    ).hexdigest(),
                }
            )

    manifest = {
        "version": 1,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "snapshot_id": snapshot_id,
        "archive": "ubuntu-snapshot.tar.gz",
        "archive_size": size,
        "archive_sha256": digest,
        "chunk_size": CHUNK_SIZE,
        "parts": parts,
        "rootfs": "ubuntu-22.04.5-amd64",
        "app_version": APP_VERSION,
    }

    github_put_file(
        MANIFEST_PATH,
        json.dumps(
            manifest,
            indent=2,
        ),
        (
            f"SHL manifest "
            f"{snapshot_id}"
        ),
        branch=PERSISTENT_BRANCH,
    )

    log(
        "Persistent GitHub snapshot uploaded."
    )

    return manifest


# ============================================================
# BACKUP
# ============================================================

def write_backup_state(data):
    BACKUP_STATE_FILE.write_text(
        json.dumps(
            data,
            indent=2,
        ),
        encoding="utf-8",
    )


def backup_now(reason="manual"):
    if not GITHUB_TOKEN:
        log(
            "Backup skipped: "
            "GITHUB_TOKEN is missing."
        )
        return False

    try:
        lock = FileLock(
            BACKUP_LOCK_FILE,
            blocking=False,
        )

        lock.__enter__()

    except BlockingIOError:
        log(
            "Backup already running "
            "in another process."
        )
        return False

    try:
        log(
            f"Starting persistent "
            f"GitHub backup: {reason}"
        )

        write_backup_state(
            {
                "status": "running",
                "reason": reason,
                "started_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )

        snapshot = create_snapshot()

        manifest = upload_snapshot(
            snapshot
        )

        write_backup_state(
            {
                "status": "success",
                "reason": reason,
                "finished_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "snapshot_id": manifest[
                    "snapshot_id"
                ],
                "size": manifest[
                    "archive_size"
                ],
                "sha256": manifest[
                    "archive_sha256"
                ],
            }
        )

        return True

    except Exception as exc:

        log(
            f"Backup failed: {exc}"
        )

        write_backup_state(
            {
                "status": "failed",
                "reason": reason,
                "finished_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "error": str(exc),
            }
        )

        return False

    finally:
        try:
            lock.__exit__(
                None,
                None,
                None,
            )
        except Exception:
            pass


# ============================================================
# RESTORE
# ============================================================

def download_github_file(
    path,
    branch=PERSISTENT_BRANCH,
):
    data = github_get_file(
        path,
        branch,
    )

    if not data:
        raise RuntimeError(
            f"GitHub file not found: {path}"
        )

    raw = base64.b64decode(
        data["content"]
    )

    return raw


def restore_snapshot(manifest):
    log(
        "Restoring Ubuntu from "
        "persistent GitHub snapshot..."
    )

    restore_root = (
        RESTORE_DIR / "ubuntu"
    )

    if restore_root.exists():
        shutil.rmtree(
            restore_root,
            ignore_errors=True,
        )

    restore_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    archive = (
        RESTORE_DIR /
        "ubuntu-snapshot.tar.gz"
    )

    with open(archive, "wb") as out:

        for part in sorted(
            manifest["parts"],
            key=lambda x: x["index"],
        ):

            log(
                f"Downloading snapshot part "
                f"{part['index'] + 1}/"
                f"{len(manifest['parts'])}"
            )

            data = download_github_file(
                part["path"]
            )

            actual_sha = hashlib.sha256(
                data
            ).hexdigest()

            if actual_sha != part["sha256"]:
                raise RuntimeError(
                    "Snapshot chunk SHA256 "
                    "verification failed."
                )

            out.write(data)

    actual_archive_sha = sha256_file(
        archive
    )

    if (
        actual_archive_sha
        != manifest["archive_sha256"]
    ):
        raise RuntimeError(
            "Full snapshot SHA256 "
            "verification failed."
        )

    log(
        "Snapshot SHA256 verified."
    )

    log(
        "Extracting Ubuntu snapshot..."
    )

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:

        tar.extractall(
            restore_root
        )

    if ROOTFS_DIR.exists():
        shutil.rmtree(
            ROOTFS_DIR,
            ignore_errors=True,
        )

    ROOTFS_DIR.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.move(
        str(restore_root),
        str(ROOTFS_DIR),
    )

    log(
        "Ubuntu snapshot restored."
    )

    archive.unlink(
        missing_ok=True
    )


# ============================================================
# UBUNTU BASE INSTALL
# ============================================================

def extract_ubuntu_base():
    archive = (
        BASE_DIR /
        "ubuntu-base.tar.gz"
    )

    if ROOTFS_DIR.exists():
        log(
            "Ubuntu rootfs already exists."
        )
        return

    log(
        "Downloading Ubuntu 22.04.5 base..."
    )

    download_file(
        UBUNTU_URL,
        archive,
        timeout=600,
    )

    ROOTFS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log(
        "Extracting Ubuntu base..."
    )

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:

        tar.extractall(
            ROOTFS_DIR
        )

    archive.unlink(
        missing_ok=True
    )

    log(
        "Ubuntu base extracted."
    )


# ============================================================
# PROOT
# ============================================================

def install_proot():
    proot_path = (
        PROOT_DIR / "proot"
    )

    if (
        proot_path.exists()
        and os.access(
            proot_path,
            os.X_OK,
        )
    ):
        return proot_path

    archive = (
        PROOT_DIR /
        "proot-amd64"
    )

    log(
        "Downloading PRoot..."
    )

    download_file(
        PROOT_URL,
        archive,
        timeout=300,
    )

    shutil.copy2(
        archive,
        proot_path,
    )

    proot_path.chmod(0o755)

    archive.unlink(
        missing_ok=True
    )

    return proot_path


# ============================================================
# PRoot COMMAND
# ============================================================

def proot_command(
    command,
    check=True,
    timeout=300,
):
    proot_path = install_proot()

    if isinstance(command, str):
        command = [
            "/bin/bash",
            "-lc",
            command,
        ]

    env = os.environ.copy()

    env["HOME"] = "/root"
    env["USER"] = "root"
    env["LOGNAME"] = "root"
    env["PATH"] = (
        "/root/.local/bin:"
        "/usr/local/sbin:"
        "/usr/local/bin:"
        "/usr/sbin:"
        "/usr/bin:"
        "/sbin:"
        "/bin"
    )

    cmd = [
        str(proot_path),
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
        "/etc/resolv.conf:"
        "/etc/resolv.conf",

        "-w",
        "/root",

        "/usr/bin/env",
        "-i",

        f"HOME={env['HOME']}",
        f"USER={env['USER']}",
        f"LOGNAME={env['LOGNAME']}",
        f"PATH={env['PATH']}",

        "TERM=xterm-256color",

        *command,
    ]

    return run_command(
        cmd,
        check=check,
        timeout=timeout,
        env=os.environ.copy(),
    )


# ============================================================
# UBUNTU CONFIGURATION
# ============================================================

def configure_ubuntu():
    etc = ROOTFS_DIR / "etc"

    etc.mkdir(
        parents=True,
        exist_ok=True,
    )

    hostname = (
        etc / "hostname"
    )

    hostname.write_text(
        "shl\n",
        encoding="utf-8",
    )

    hosts = (
        etc / "hosts"
    )

    hosts.write_text(
        "127.0.0.1 localhost\n"
        "127.0.1.1 shl\n"
        "::1 localhost ip6-localhost "
        "ip6-loopback\n",
        encoding="utf-8",
    )


# ============================================================
# UBUNTU PACKAGES
# ============================================================

UBUNTU_PACKAGES = [
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
    "screen",
    "tmux",
]


def install_base_packages():
    marker = (
        ROOTFS_DIR /
        "var/lib/shl-base-installed"
    )

    if marker.exists():
        log(
            "Base packages already installed."
        )
        return

    log(
        "Installing Ubuntu base packages..."
    )

    command = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update && "
        "apt-get install -y "
        + " ".join(
            UBUNTU_PACKAGES
        )
    )

    proot_command(
        command,
        check=True,
        timeout=900,
    )

    marker.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    marker.write_text(
        datetime.now(
            timezone.utc
        ).isoformat(),
        encoding="utf-8",
    )

    log(
        "Base packages installed."
    )


# ============================================================
# UBUNTU TEST
# ============================================================

def test_ubuntu():
    result = proot_command(
        [
            "/bin/bash",
            "-lc",
            (
                "echo SHL_UBUNTU_OK; "
                "id; "
                "cat /etc/os-release | "
                "grep -E 'PRETTY_NAME|VERSION_ID'; "
                "uname -m; "
                "command -v python3 || true; "
                "command -v git || true; "
                "command -v curl || true"
            ),
        ],
        check=False,
        timeout=60,
    )

    return result.returncode == 0


# ============================================================
# SSHX
# ============================================================

def sshx_running():
    if not SSHX_PID_FILE.exists():
        return False

    try:
        pid = int(
            SSHX_PID_FILE.read_text(
                encoding="utf-8"
            ).strip()
        )

        os.kill(pid, 0)

        return True

    except Exception:
        return False


def install_sshx():
    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        SSHX_PATH.exists()
        and os.access(
            SSHX_PATH,
            os.X_OK,
        )
    ):
        log(
            f"SSHX already installed: "
            f"{SSHX_PATH}"
        )
        return SSHX_PATH

    archive = (
        BASE_DIR /
        "sshx.tar.gz"
    )

    extract_dir = (
        BASE_DIR /
        "sshx-extract"
    )

    archive.unlink(
        missing_ok=True
    )

    if extract_dir.exists():
        shutil.rmtree(
            extract_dir,
            ignore_errors=True,
        )

    extract_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    log(
        "Installing SSHX directly "
        "without sudo..."
    )

    download_file(
        SSHX_BINARY_URL,
        archive,
        timeout=300,
    )

    log(
        "Extracting SSHX..."
    )

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:

        tar.extractall(
            extract_dir
        )

    candidates = []

    for path in extract_dir.rglob("*"):

        if (
            path.is_file()
            and path.name == "sshx"
        ):
            candidates.append(path)

    if not candidates:
        raise RuntimeError(
            "SSHX binary was not found "
            "after extraction."
        )

    source = candidates[0]

    log(
        f"SSHX binary found: {source}"
    )

    shutil.copy2(
        source,
        SSHX_PATH,
    )

    SSHX_PATH.chmod(
        0o755
    )

    archive.unlink(
        missing_ok=True
    )

    shutil.rmtree(
        extract_dir,
        ignore_errors=True,
    )

    result = proot_command(
        [
            "/bin/bash",
            "-lc",
            (
                "if [ -x "
                "/root/.local/bin/sshx ]; then "
                "/root/.local/bin/sshx --version "
                "2>&1 || true; "
                "fi"
            ),
        ],
        check=False,
        timeout=30,
    )

    if result.stdout:
        log(
            "SSHX version: "
            + result.stdout.strip()
        )

    return SSHX_PATH


def extract_sshx_link():
    if not SSHX_LOG_FILE.exists():
        return None

    try:
        text = SSHX_LOG_FILE.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    except Exception:
        return None

    patterns = [
        r"https://sshx\.io/[A-Za-z0-9_\-/?=&.]+",
        r"https://[A-Za-z0-9._-]+\.sshx\.io/[A-Za-z0-9_\-/?=&.]+",
    ]

    for pattern in patterns:
        matches = re.findall(
            pattern,
            text,
        )

        if matches:
            return matches[-1]

    return None


def start_sshx():
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        lock = FileLock(
            SSHX_LOCK_FILE,
            blocking=False,
        )

        lock.__enter__()

    except BlockingIOError:
        log(
            "Another process is handling SSHX."
        )
        return

    try:
        if sshx_running():
            log(
                "SSHX is already running."
            )
            return

        install_sshx()

        if not SSHX_PATH.exists():
            raise RuntimeError(
                "SSHX binary does not exist."
            )

        SSHX_LOG_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        log_file = open(
            SSHX_LOG_FILE,
            "a",
            buffering=1,
            encoding="utf-8",
        )

        log_file.write(
            "\n\n===== SSHX START =====\n"
        )

        process = subprocess.Popen(
            [
                str(SSHX_PATH),
                "run",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(
                ROOTFS_DIR / "root"
            ),
            start_new_session=True,
        )

        SSHX_PID_FILE.write_text(
            str(process.pid),
            encoding="utf-8",
        )

        log(
            f"SSHX started. PID={process.pid}"
        )

        time.sleep(3)

        if process.poll() is not None:
            log(
                "SSHX exited immediately. "
                f"returncode={process.returncode}"
            )

        link = extract_sshx_link()

        if link:
            log(
                f"SSHX link: {link}"
            )

    finally:
        try:
            lock.__exit__(
                None,
                None,
                None,
            )
        except Exception:
            pass


# ============================================================
# PERSISTENT SERVICE DEFINITIONS
# ============================================================

def ensure_service_dir():
    SERVICE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def service_pid_file(name):
    return (
        STATE_DIR /
        f"service-{name}.pid"
    )


def service_is_running(name):
    path = service_pid_file(name)

    if not path.exists():
        return False

    try:
        pid = int(
            path.read_text(
                encoding="utf-8"
            ).strip()
        )

        os.kill(pid, 0)

        return True

    except Exception:
        return False


def load_services():
    ensure_service_dir()

    services = []

    for path in sorted(
        SERVICE_DIR.glob("*.json")
    ):

        try:
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )

            if data.get(
                "enabled",
                True,
            ):
                services.append(data)

        except Exception as exc:
            log(
                f"Invalid service file "
                f"{path}: {exc}"
            )

    return services


def start_service(service):
    name = service["name"]
    command = service["command"]

    if service_is_running(name):
        log(
            f"Service already running: "
            f"{name}"
        )
        return

    log(
        f"Starting persistent service: "
        f"{name}"
    )

    service_log = (
        STATE_DIR /
        f"service-{name}.log"
    )

    service_log.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    lf = open(
        service_log,
        "a",
        buffering=1,
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [
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
            "/etc/resolv.conf:"
            "/etc/resolv.conf",

            "-w",
            "/root",

            "/bin/bash",
            "-lc",
            command,
        ],
        stdin=subprocess.DEVNULL,
        stdout=lf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    service_pid_file(
        name
    ).write_text(
        str(process.pid),
        encoding="utf-8",
    )

    log(
        f"Service {name} started "
        f"PID={process.pid}"
    )


def start_persistent_services():
    for service in load_services():
        try:
            start_service(
                service
            )
        except Exception as exc:
            log(
                f"Service {service.get('name')} "
                f"failed: {exc}"
            )


# ============================================================
# WATCHER
# ============================================================

WATCHER_STARTED_FILE = (
    STATE_DIR /
    "watcher.started"
)

watcher_thread = None
watcher_stop = threading.Event()


def should_ignore_watch_path(path):
    try:
        relative = Path(path).relative_to(
            ROOTFS_DIR
        )
    except Exception:
        return True

    parts = relative.parts

    if not parts:
        return True

    if parts[0] in VOLATILE_TOP_LEVEL:
        return True

    if (
        parts[0] == "var"
        and len(parts) >= 2
        and parts[1] == "cache"
    ):
        return True

    if (
        parts[0] == "var"
        and len(parts) >= 3
        and parts[1] == "lib"
        and parts[2] == "apt"
    ):
        return True

    return False


def watcher_loop():
    """
    Lightweight periodic dirty-state watcher.

    It intentionally DOES NOT create a snapshot on every
    filesystem event.

    A full Ubuntu snapshot can be hundreds of MB.
    """

    last_mtime = 0

    while not watcher_stop.is_set():

        try:

            newest = 0

            for root, dirs, files in os.walk(
                ROOTFS_DIR
            ):

                dirs[:] = [
                    d for d in dirs
                    if d not in VOLATILE_TOP_LEVEL
                ]

                for name in files[:]:
                    try:
                        path = (
                            Path(root) /
                            name
                        )

                        if should_ignore_watch_path(
                            path
                        ):
                            continue

                        mtime = path.stat().st_mtime_ns

                        if mtime > newest:
                            newest = mtime

                    except Exception:
                        pass

            if newest > last_mtime:
                last_mtime = newest

                state = {
                    "dirty": True,
                    "detected_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }

                WATCHER_STATE_FILE.write_text(
                    json.dumps(
                        state,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

        except Exception as exc:
            log(
                f"Watcher error: {exc}"
            )

        watcher_stop.wait(
            60
        )


def start_watcher():
    global watcher_thread

    if watcher_thread is not None:
        return

    watcher_thread = threading.Thread(
        target=watcher_loop,
        daemon=True,
        name="shl-watcher",
    )

    watcher_thread.start()

    log(
        "Filesystem watcher started."
    )


# ============================================================
# AUTO BACKUP CHECK
# ============================================================

def maybe_auto_backup():
    if not GITHUB_TOKEN:
        return

    if not WATCHER_STATE_FILE.exists():
        return

    try:
        watcher_state = json.loads(
            WATCHER_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return

    if not watcher_state.get(
        "dirty",
        False,
    ):
        return

    last_backup = 0

    if BACKUP_STATE_FILE.exists():

        try:
            state = json.loads(
                BACKUP_STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )

            finished = state.get(
                "finished_at"
            )

            if finished:
                dt = datetime.fromisoformat(
                    finished
                )

                last_backup = dt.timestamp()

        except Exception:
            pass

    now = time.time()

    if (
        now - last_backup
        < AUTO_BACKUP_INTERVAL
    ):
        return

    backup_now(
        "automatic"
    )

    try:
        watcher_state["dirty"] = False

        WATCHER_STATE_FILE.write_text(
            json.dumps(
                watcher_state,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


# ============================================================
# BOOTSTRAP
# ============================================================

BOOTSTRAP_DONE_FILE = (
    STATE_DIR /
    "bootstrap.done"
)


def bootstrap():
    ensure_dirs()

    try:
        lock = FileLock(
            BOOTSTRAP_LOCK_FILE,
            blocking=True,
        )

        lock.__enter__()

    except Exception as exc:
        log(
            f"Bootstrap lock failed: {exc}"
        )
        raise

    try:

        log(
            f"{APP_NAME} {APP_VERSION} bootstrap..."
        )

        # ----------------------------------------------------
        # PERSISTENT RESTORE
        # ----------------------------------------------------

        restored = False

        if GITHUB_TOKEN:

            try:

                if github_branch_exists(
                    PERSISTENT_BRANCH
                ):

                    manifest = load_manifest()

                    if manifest:
                        log(
                            "Persistent manifest found."
                        )

                        # Restore only if local Ubuntu
                        # is missing.
                        if not ROOTFS_DIR.exists():
                            restore_snapshot(
                                manifest
                            )
                            restored = True

                    else:
                        log(
                            "No persistent manifest yet."
                        )

                else:
                    log(
                        "No persistent state branch yet."
                    )

            except Exception as exc:
                log(
                    f"Persistent restore check "
                    f"failed: {exc}"
                )

        # ----------------------------------------------------
        # BASE UBUNTU
        # ----------------------------------------------------

        if not ROOTFS_DIR.exists():
            extract_ubuntu_base()

        configure_ubuntu()

        # ----------------------------------------------------
        # PROOT
        # ----------------------------------------------------

        install_proot()

        # ----------------------------------------------------
        # PACKAGE INSTALL
        # ----------------------------------------------------

        install_base_packages()

        # ----------------------------------------------------
        # TEST UBUNTU
        # ----------------------------------------------------

        if not test_ubuntu():
            raise RuntimeError(
                "Ubuntu environment test failed."
            )

        # ----------------------------------------------------
        # SSHX
        # ----------------------------------------------------

        try:
            start_sshx()
        except Exception:
            logger.exception(
                "SSHX startup failed."
            )

        # ----------------------------------------------------
        # SERVICES
        # ----------------------------------------------------

        start_persistent_services()

        # ----------------------------------------------------
        # WATCHER
        # ----------------------------------------------------

        start_watcher()

        # ----------------------------------------------------
        # INITIAL BACKUP
        # ----------------------------------------------------

        if (
            GITHUB_TOKEN
            and not manifest_exists()
        ):
            try:
                backup_now(
                    "initial"
                )
            except Exception:
                logger.exception(
                    "Initial persistent "
                    "backup failed."
                )

        BOOTSTRAP_DONE_FILE.write_text(
            datetime.now(
                timezone.utc
            ).isoformat(),
            encoding="utf-8",
        )

        log(
            "SHL Ubuntu environment is ready."
        )

    finally:

        try:
            lock.__exit__(
                None,
                None,
                None,
            )
        except Exception:
            pass


def manifest_exists():
    if not GITHUB_TOKEN:
        return False

    try:
        return load_manifest() is not None
    except Exception:
        return False


# ============================================================
# STREAMLIT SESSION
# ============================================================

@st.cache_resource(
    show_spinner=False
)
def initialize_runtime():
    bootstrap()

    return {
        "ready": True,
        "time": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# UI HELPERS
# ============================================================

def get_backup_state():
    if not BACKUP_STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            BACKUP_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {}


def get_sshx_link():
    return extract_sshx_link()


def get_sshx_pid():
    if not SSHX_PID_FILE.exists():
        return None

    try:
        return int(
            SSHX_PID_FILE.read_text(
                encoding="utf-8"
            ).strip()
        )
    except Exception:
        return None


def ubuntu_command_ui(command):
    try:
        result = proot_command(
            [
                "/bin/bash",
                "-lc",
                command,
            ],
            check=False,
            timeout=300,
        )

        return result.stdout or ""

    except Exception as exc:
        return (
            f"ERROR: {exc}"
        )


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="SHL Persistent Ubuntu",
    page_icon="🖥️",
    layout="wide",
)


st.title(
    "🖥️ SHL Persistent Ubuntu"
)

st.caption(
    f"Version {APP_VERSION} • "
    f"Ubuntu 22.04.5 • "
    f"PRoot • GitHub persistence"
)


# ============================================================
# INITIALIZATION
# ============================================================

try:
    initialize_runtime()

except Exception as exc:

    st.error(
        "SHL bootstrap failed."
    )

    st.exception(exc)

    st.stop()


# Automatic backup check.
try:
    maybe_auto_backup()
except Exception as exc:
    log(
        f"Automatic backup check failed: {exc}"
    )


# ============================================================
# STATUS
# ============================================================

col1, col2, col3, col4 = st.columns(4)


with col1:

    if ROOTFS_DIR.exists():
        st.success(
            "Ubuntu: READY"
        )
    else:
        st.error(
            "Ubuntu: MISSING"
        )


with col2:

    if sshx_running():
        st.success(
            "SSHX: RUNNING"
        )
    else:
        st.warning(
            "SSHX: STOPPED"
        )


with col3:

    if GITHUB_TOKEN:
        if github_branch_exists(
            PERSISTENT_BRANCH
        ):
            st.success(
                "GitHub: CONNECTED"
            )
        else:
            st.info(
                "GitHub: READY"
            )
    else:
        st.error(
            "GitHub: TOKEN MISSING"
        )


with col4:

    backup_state = get_backup_state()

    status = backup_state.get(
        "status"
    )

    if status == "success":
        st.success(
            "Backup: OK"
        )
    elif status == "running":
        st.info(
            "Backup: RUNNING"
        )
    elif status == "failed":
        st.error(
            "Backup: FAILED"
        )
    else:
        st.info(
            "Backup: NOT YET"
        )


st.divider()


# ============================================================
# SSHX
# ============================================================

st.subheader(
    "🔗 SSHX"
)

sshx_col1, sshx_col2 = st.columns(
    [3, 1]
)

with sshx_col1:

    link = get_sshx_link()

    if link:
        st.success(
            "SSHX link detected:"
        )

        st.code(
            link,
            language="text",
        )

        st.markdown(
            f"[Open SSHX]({link})"
        )

    else:
        st.info(
            "SSHX link has not appeared yet."
        )

with sshx_col2:

    st.write(
        f"PID: {get_sshx_pid()}"
    )

    if st.button(
        "Restart SSHX",
        use_container_width=True,
    ):

        try:

            if SSHX_PID_FILE.exists():

                try:
                    pid = int(
                        SSHX_PID_FILE.read_text(
                            encoding="utf-8"
                        ).strip()
                    )

                    os.kill(
                        pid,
                        signal.SIGTERM,
                    )

                except Exception:
                    pass

                SSHX_PID_FILE.unlink(
                    missing_ok=True
                )

            start_sshx()

            st.success(
                "SSHX restart requested."
            )

            st.rerun()

        except Exception as exc:

            st.error(
                f"SSHX restart failed: {exc}"
            )


with st.expander(
    "SSHX log"
):

    if SSHX_LOG_FILE.exists():

        text = SSHX_LOG_FILE.read_text(
            encoding="utf-8",
            errors="ignore",
        )

        st.code(
            text[-15000:],
            language="text",
        )

    else:

        st.info(
            "No SSHX log yet."
        )


# ============================================================
# BACKUP
# ============================================================

st.divider()

st.subheader(
    "💾 Persistent GitHub Backup"
)

backup_col1, backup_col2 = st.columns(
    [2, 1]
)

with backup_col1:

    st.write(
        f"Repository: `{GITHUB_REPO}`"
    )

    st.write(
        f"State branch: `{PERSISTENT_BRANCH}`"
    )

    state = get_backup_state()

    if state:
        st.json(state)

with backup_col2:

    if st.button(
        "💾 Backup Now",
        type="primary",
        use_container_width=True,
    ):

        with st.spinner(
            "Creating and uploading Ubuntu snapshot..."
        ):

            ok = backup_now(
                "manual"
            )

        if ok:
            st.success(
                "Backup completed."
            )
        else:
            st.error(
                "Backup failed. Check logs."
            )

        st.rerun()


# ============================================================
# PERSISTENT MANIFEST
# ============================================================

st.subheader(
    "📦 Persistent Snapshot"
)

try:

    manifest = load_manifest()

    if manifest:

        m1, m2, m3 = st.columns(3)

        with m1:
            st.metric(
                "Snapshot",
                manifest.get(
                    "snapshot_id",
                    "-",
                ),
            )

        with m2:
            size = manifest.get(
                "archive_size",
                0,
            )

            st.metric(
                "Size",
                f"{size / 1024 / 1024:.1f} MB",
            )

        with m3:
            st.metric(
                "Parts",
                len(
                    manifest.get(
                        "parts",
                        [],
                    )
                ),
            )

        st.write(
            "SHA256:"
        )

        st.code(
            manifest.get(
                "archive_sha256",
                "-",
            ),
            language="text",
        )

    else:

        st.info(
            "No persistent snapshot found."
        )

except Exception as exc:

    st.warning(
        f"Manifest unavailable: {exc}"
    )


# ============================================================
# UBUNTU TERMINAL
# ============================================================

st.divider()

st.subheader(
    "⌨️ Ubuntu Terminal"
)

if "terminal_output" not in st.session_state:
    st.session_state.terminal_output = ""

command = st.text_input(
    "Command",
    value="neofetch",
    key="ubuntu_command",
)

run_col1, run_col2 = st.columns(
    [1, 5]
)

with run_col1:

    run_clicked = st.button(
        "Run",
        type="primary",
        use_container_width=True,
    )

if run_clicked:

    with st.spinner(
        "Running..."
    ):

        output = ubuntu_command_ui(
            command
        )

    st.session_state.terminal_output = output


if st.session_state.terminal_output:

    st.code(
        st.session_state.terminal_output,
        language="text",
    )


# ============================================================
# COMMON COMMANDS
# ============================================================

st.subheader(
    "⚡ Quick Commands"
)

quick_commands = [
    "neofetch",
    "uname -a",
    "df -h",
    "free -h",
    "ip addr",
    "ps aux",
    "python3 --version",
    "git --version",
    "curl --version",
]

quick_cols = st.columns(3)

for index, command in enumerate(
    quick_commands
):

    with quick_cols[
        index % 3
    ]:

        if st.button(
            command,
            key=f"quick_{index}",
            use_container_width=True,
        ):

            output = ubuntu_command_ui(
                command
            )

            st.code(
                output,
                language="text",
            )


# ============================================================
# SERVICES
# ============================================================

st.divider()

st.subheader(
    "⚙️ Persistent Services"
)

services = load_services()

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

        running = service_is_running(
            name
        )

        if running:
            st.success(
                f"{name}: RUNNING"
            )
        else:
            st.warning(
                f"{name}: STOPPED"
            )

        st.code(
            service.get(
                "command",
                "",
            ),
            language="bash",
        )


# ============================================================
# FILESYSTEM STATUS
# ============================================================

st.divider()

st.subheader(
    "📁 Runtime Status"
)

runtime_col1, runtime_col2 = st.columns(
    2
)

with runtime_col1:

    st.write(
        f"Runtime: `{BASE_DIR}`"
    )

    st.write(
        f"Ubuntu: `{ROOTFS_DIR}`"
    )

    st.write(
        f"PRoot: `{PROOT_DIR}`"
    )

with runtime_col2:

    if BOOTSTRAP_DONE_FILE.exists():

        st.write(
            "Bootstrap:"
        )

        st.code(
            BOOTSTRAP_DONE_FILE.read_text(
                encoding="utf-8"
            ).strip(),
            language="text",
        )

    else:

        st.write(
            "Bootstrap: unknown"
        )


# ============================================================
# IMPORTANT PERSISTENCE NOTE
# ============================================================

st.divider()

st.info(
    """
**Persistence model**

Ubuntu files are stored temporarily inside the Streamlit
runtime and periodically snapshotted to the GitHub
`shl-persistent` branch.

After a new runtime starts, SHL restores the latest snapshot,
then starts PRoot and SSHX again.

The SSHX process itself cannot survive destruction of the
Streamlit runtime. The filesystem/configuration can be
restored, but the old SSHX process/session cannot remain alive
after the underlying runtime is terminated.
"""
)


# ============================================================
# FOOTER
# ============================================================

st.caption(
    "SHL • Persistent Ubuntu • "
    "GitHub-backed state • "
    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
)
