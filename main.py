import os
import sys
import json
import time
import signal
import shutil
import tarfile
import hashlib
import logging
import subprocess
import threading
import urllib.request
import urllib.error
import base64
from pathlib import Path
from datetime import datetime, timezone

import streamlit as st


# ============================================================
# SHL - GitHub Persistent Ubuntu Server
# ============================================================

BASE_DIR = Path("/tmp/shl-runtime")

ROOTFS_DIR = BASE_DIR / "ubuntu"
PROOT_DIR = BASE_DIR / "proot"
PROOT_PATH = PROOT_DIR / "proot"

SSHX_DIR = Path.home() / ".local" / "bin"
SSHX_PATH = SSHX_DIR / "sshx"

RESTORE_DIR = BASE_DIR / "restore"

STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

LOCK_FILE = STATE_DIR / "backup.lock"
BACKUP_STATUS_FILE = STATE_DIR / "backup-status.json"

SERVICE_DIR = ROOTFS_DIR / "etc" / "shl" / "services"

UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/download/"
    "proot-amd64"
)

SSHX_INSTALL_URL = "https://sshx.io/get"

GITHUB_API = "https://api.github.com"

# Keep chunks safely below GitHub's 100 MB single-file limit.
CHUNK_SIZE = 40 * 1024 * 1024

# Persistent state branch.
STATE_BRANCH_DEFAULT = "shl-persistent"

BACKUP_INTERVAL = 180

logging.basicConfig(
    level=logging.INFO,
    format="[SHL] %(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("SHL")


# ============================================================
# SECRETS
# ============================================================

def secret(name, default=None):
    try:
        value = st.secrets.get(name)
        if value:
            return str(value)
    except Exception:
        pass

    value = os.environ.get(name)
    if value:
        return value

    return default


GITHUB_TOKEN = secret("GITHUB_TOKEN")
GITHUB_REPO = secret("GITHUB_REPO", "vipspots4me-hue/sfl")
GITHUB_BRANCH = secret("GITHUB_BRANCH", "main")
GITHUB_STATE_BRANCH = secret(
    "GITHUB_STATE_BRANCH",
    STATE_BRANCH_DEFAULT,
)


# ============================================================
# PATH HELPERS
# ============================================================

def ensure_dirs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    ROOTFS_DIR.parent.mkdir(parents=True, exist_ok=True)
    PROOT_DIR.mkdir(parents=True, exist_ok=True)
    SSHX_DIR.mkdir(parents=True, exist_ok=True)
    RESTORE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(url, destination):
    destination = Path(destination)

    if destination.exists() and destination.stat().st_size > 1024:
        return destination

    tmp = destination.with_suffix(destination.suffix + ".tmp")

    log.info("Downloading: %s", url)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SHL/1.0",
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        with open(tmp, "wb") as f:
            while True:
                data = response.read(1024 * 1024)
                if not data:
                    break
                f.write(data)

    tmp.replace(destination)

    return destination


# ============================================================
# PROOT
# ============================================================

def install_proot():
    ensure_dirs()

    if PROOT_PATH.exists():
        PROOT_PATH.chmod(0o755)
        return

    log.info("Downloading PRoot...")

    download_file(PROOT_URL, PROOT_PATH)

    PROOT_PATH.chmod(0o755)

    result = subprocess.run(
        [str(PROOT_PATH), "--version"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "PRoot installation failed:\n"
            + result.stderr
        )

    log.info("PRoot installed.")


# ============================================================
# UBUNTU BASE
# ============================================================

def ubuntu_exists():
    return (
        ROOTFS_DIR.exists()
        and (ROOTFS_DIR / "bin").exists()
        and (ROOTFS_DIR / "etc" / "os-release").exists()
    )


def extract_ubuntu_base():
    ensure_dirs()

    if ubuntu_exists():
        return

    archive = BASE_DIR / "ubuntu-base.tar.gz"

    log.info("Downloading Ubuntu 22.04 base...")

    download_file(UBUNTU_URL, archive)

    temp_root = BASE_DIR / "ubuntu-new"

    if temp_root.exists():
        shutil.rmtree(temp_root)

    temp_root.mkdir(parents=True)

    log.info("Extracting Ubuntu...")

    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(temp_root)

    temp_root.replace(ROOTFS_DIR)

    log.info("Ubuntu base installed.")


# ============================================================
# GITHUB API
# ============================================================

class GitHubError(Exception):
    pass


class GitHub:
    def __init__(self):
        if not GITHUB_TOKEN:
            raise GitHubError(
                "GITHUB_TOKEN is missing from Streamlit Secrets."
            )

        self.repo = GITHUB_REPO
        self.token = GITHUB_TOKEN

    def request(self, method, path, data=None):
        url = GITHUB_API + path

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SHL-Persistent-Server",
        }

        body = None

        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                raw = response.read()

                if not raw:
                    return None

                return json.loads(raw.decode("utf-8"))

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")

            raise GitHubError(
                f"GitHub HTTP {e.code}: {error_body}"
            )

    def get_branch(self, branch):
        return self.request(
            "GET",
            f"/repos/{self.repo}/branches/{branch}",
        )

    def branch_exists(self, branch):
        try:
            self.get_branch(branch)
            return True
        except GitHubError:
            return False

    def get_file(self, path, branch):
        encoded = urllib.parse.quote(path, safe="/")

        return self.request(
            "GET",
            f"/repos/{self.repo}/contents/{encoded}?ref={urllib.parse.quote(branch)}",
        )

    def create_or_update_file(
        self,
        path,
        content_bytes,
        branch,
        message,
    ):
        encoded = urllib.parse.quote(path, safe="/")

        sha = None

        try:
            existing = self.get_file(path, branch)

            if isinstance(existing, dict):
                sha = existing.get("sha")

        except Exception:
            pass

        payload = {
            "message": message,
            "content": base64.b64encode(
                content_bytes
            ).decode("ascii"),
            "branch": branch,
        }

        if sha:
            payload["sha"] = sha

        return self.request(
            "PUT",
            f"/repos/{self.repo}/contents/{encoded}",
            payload,
        )


# urllib.parse is imported after class declaration intentionally.
import urllib.parse


# ============================================================
# STATE BRANCH
# ============================================================

def ensure_state_branch():
    gh = GitHub()

    if gh.branch_exists(GITHUB_STATE_BRANCH):
        return

    log.info(
        "Creating persistent branch: %s",
        GITHUB_STATE_BRANCH,
    )

    main = gh.get_branch(GITHUB_BRANCH)

    sha = main["commit"]["sha"]

    gh.request(
        "POST",
        f"/repos/{GITHUB_REPO}/git/refs",
        {
            "ref": f"refs/heads/{GITHUB_STATE_BRANCH}",
            "sha": sha,
        },
    )

    log.info("Persistent branch created.")


# ============================================================
# SNAPSHOT
# ============================================================

def calculate_sha256(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            data = f.read(1024 * 1024)

            if not data:
                break

            h.update(data)

    return h.hexdigest()


def create_snapshot():
    ensure_dirs()

    snapshot = BASE_DIR / "ubuntu-snapshot.tar.gz"

    if snapshot.exists():
        snapshot.unlink()

    log.info("Creating Ubuntu snapshot...")

    # Exclude volatile runtime directories.
    exclude_names = {
        "proc",
        "sys",
        "dev",
        "run",
        "tmp",
        "mnt",
        "media",
    }

    def filter_members(tar):
        for member in tar:
            parts = Path(member.name).parts

            if parts and parts[0] in exclude_names:
                continue

            yield member

    with tarfile.open(
        snapshot,
        "w:gz",
        compresslevel=6,
    ) as tar:
        for member in filter_members(
            tarfile.open(ROOTFS_DIR, "r")
        ):
            tar.add(
                ROOTFS_DIR / member.name,
                arcname=member.name,
                recursive=False,
            )

    size = snapshot.stat().st_size
    sha = calculate_sha256(snapshot)

    log.info(
        "Snapshot created: %.2f MB",
        size / 1024 / 1024,
    )

    return snapshot, sha


# ============================================================
# SNAPSHOT CHUNKS
# ============================================================

def split_snapshot(snapshot):
    snapshot = Path(snapshot)

    chunks = []

    with open(snapshot, "rb") as f:
        index = 0

        while True:
            data = f.read(CHUNK_SIZE)

            if not data:
                break

            chunk = BASE_DIR / (
                f"ubuntu-snapshot.part-{index:04d}"
            )

            with open(chunk, "wb") as out:
                out.write(data)

            chunks.append(chunk)

            index += 1

    return chunks


# ============================================================
# UPLOAD SNAPSHOT
# ============================================================

def upload_snapshot(snapshot, sha):
    gh = GitHub()

    ensure_state_branch()

    chunks = split_snapshot(snapshot)

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = {
        "format": 1,
        "created_at": timestamp,
        "snapshot_sha256": sha,
        "snapshot_size": snapshot.stat().st_size,
        "chunk_size": CHUNK_SIZE,
        "chunks": [
            chunk.name
            for chunk in chunks
        ],
    }

    log.info(
        "Uploading %d snapshot chunks to GitHub...",
        len(chunks),
    )

    # Manifest first.
    gh.create_or_update_file(
        ".shl-state/manifest.json",
        json.dumps(
            manifest,
            indent=2,
        ).encode(),
        GITHUB_STATE_BRANCH,
        f"SHL snapshot {timestamp}",
    )

    for i, chunk in enumerate(chunks):
        log.info(
            "Uploading chunk %d/%d...",
            i + 1,
            len(chunks),
        )

        with open(chunk, "rb") as f:
            data = f.read()

        gh.create_or_update_file(
            f".shl-state/{chunk.name}",
            data,
            GITHUB_STATE_BRANCH,
            f"SHL snapshot chunk {i + 1}/{len(chunks)}",
        )

    return manifest


# ============================================================
# RESTORE
# ============================================================

def download_github_file(gh, path, branch):
    result = gh.get_file(path, branch)

    if not isinstance(result, dict):
        raise GitHubError(
            f"Invalid GitHub file response: {path}"
        )

    if result.get("encoding") != "base64":
        raise GitHubError(
            f"Unexpected GitHub encoding: {path}"
        )

    content = result.get("content", "")

    return base64.b64decode(
        content.replace("\n", "")
    )


def restore_snapshot():
    gh = GitHub()

    if not gh.branch_exists(GITHUB_STATE_BRANCH):
        log.info("No persistent state branch yet.")
        return False

    try:
        manifest_bytes = download_github_file(
            gh,
            ".shl-state/manifest.json",
            GITHUB_STATE_BRANCH,
        )
    except Exception as e:
        log.info(
            "No previous SHL snapshot found: %s",
            e,
        )
        return False

    manifest = json.loads(
        manifest_bytes.decode("utf-8")
    )

    chunks = manifest.get("chunks", [])

    if not chunks:
        return False

    archive = BASE_DIR / "restored-ubuntu.tar.gz"

    if archive.exists():
        archive.unlink()

    log.info(
        "Restoring snapshot from GitHub..."
    )

    with open(archive, "wb") as out:
        for i, filename in enumerate(chunks):
            log.info(
                "Downloading chunk %d/%d...",
                i + 1,
                len(chunks),
            )

            data = download_github_file(
                gh,
                f".shl-state/{filename}",
                GITHUB_STATE_BRANCH,
            )

            out.write(data)

    actual_sha = calculate_sha256(archive)

    expected_sha = manifest.get(
        "snapshot_sha256"
    )

    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(
            "GitHub snapshot SHA256 mismatch."
        )

    temp_root = BASE_DIR / "ubuntu-restored"

    if temp_root.exists():
        shutil.rmtree(temp_root)

    temp_root.mkdir(parents=True)

    log.info("Extracting persistent Ubuntu...")

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:
        tar.extractall(temp_root)

    if not (
        temp_root / "etc" / "os-release"
    ).exists():
        raise RuntimeError(
            "Restored Ubuntu filesystem is invalid."
        )

    if ROOTFS_DIR.exists():
        shutil.rmtree(ROOTFS_DIR)

    temp_root.replace(ROOTFS_DIR)

    log.info(
        "Ubuntu restored successfully."
    )

    return True


# ============================================================
# BACKUP
# ============================================================

backup_lock = threading.Lock()


def write_backup_status(status, **extra):
    data = {
        "status": status,
        "time": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    data.update(extra)

    BACKUP_STATUS_FILE.write_text(
        json.dumps(
            data,
            indent=2,
        )
    )


def backup_now(reason="manual"):
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing."
        )

    if not ubuntu_exists():
        raise RuntimeError(
            "Ubuntu filesystem does not exist."
        )

    if not backup_lock.acquire(
        blocking=False
    ):
        log.info(
            "Backup already running."
        )
        return False

    try:
        write_backup_status(
            "running",
            reason=reason,
        )

        log.info(
            "Starting persistent GitHub backup..."
        )

        snapshot, sha = create_snapshot()

        manifest = upload_snapshot(
            snapshot,
            sha,
        )

        write_backup_status(
            "success",
            reason=reason,
            sha256=sha,
            size=snapshot.stat().st_size,
            chunks=len(
                manifest["chunks"]
            ),
        )

        log.info(
            "Persistent GitHub backup completed."
        )

        return True

    except Exception as e:
        write_backup_status(
            "failed",
            reason=reason,
            error=str(e),
        )

        log.exception(
            "Backup failed."
        )

        return False

    finally:
        backup_lock.release()


# ============================================================
# UBUNTU CONFIG
# ============================================================

def configure_ubuntu():
    etc = ROOTFS_DIR / "etc"

    hostname = etc / "hostname"

    if not hostname.exists():
        hostname.write_text("shl-server\n")

    hosts = etc / "hosts"

    if not hosts.exists():
        hosts.write_text(
            "127.0.0.1 localhost\n"
            "127.0.1.1 shl-server\n"
            "::1 localhost ip6-localhost ip6-loopback\n"
        )

    resolv = etc / "resolv.conf"

    try:
        if resolv.exists() or resolv.is_symlink():
            resolv.unlink()

        resolv.write_text(
            "nameserver 1.1.1.1\n"
            "nameserver 8.8.8.8\n"
        )
    except Exception:
        pass


# ============================================================
# UBUNTU COMMAND
# ============================================================

def ubuntu_command(
    command,
    check=True,
    capture=False,
):
    env = os.environ.copy()

    env.update(
        {
            "HOME": "/root",
            "USER": "root",
            "TERM": "xterm-256color",
        }
    )

    args = [
        str(PROOT_PATH),
        "-0",
        "-r",
        str(ROOTFS_DIR),
        "-w",
        "/root",
        "-b",
        "/dev",
        "-b",
        "/proc",
        "-b",
        "/sys",
        "-b",
        "/tmp",
        "-b",
        "/run",
        "-b",
        "/etc/resolv.conf",
        "/usr/bin/env",
        "-i",
        "HOME=/root",
        "USER=root",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TERM=xterm-256color",
        "/bin/bash",
        "-lc",
        command,
    ]

    log.info(
        "Ubuntu: %s",
        command,
    )

    return subprocess.run(
        args,
        env=env,
        check=check,
        capture_output=capture,
        text=True,
    )


# ============================================================
# BASE PACKAGES
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
]


def install_base_packages():
    marker = (
        ROOTFS_DIR
        / "etc"
        / "shl"
        / ".base-packages-installed"
    )

    marker.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if marker.exists():
        log.info(
            "Base packages already installed."
        )
        return

    packages = " ".join(BASE_PACKAGES)

    result = ubuntu_command(
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update && "
        f"apt-get install -y {packages}",
        check=False,
        capture=True,
    )

    if result.returncode != 0:
        log.error(result.stdout)
        log.error(result.stderr)

        raise RuntimeError(
            "Ubuntu base package installation failed."
        )

    marker.write_text(
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    log.info(
        "Base packages installed."
    )


# ============================================================
# SSHX
# ============================================================

def install_sshx():
    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if SSHX_PATH.exists():
        SSHX_PATH.chmod(0o755)
        return

    log.info("Installing SSHX...")

    request = urllib.request.Request(
        SSHX_INSTALL_URL,
        headers={
            "User-Agent": "SHL/1.0",
        },
    )

    proc = subprocess.run(
        [
            "bash",
            "-c",
            "curl -sSf https://sshx.io/get | sh",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(Path.home()),
        },
    )

    if proc.stdout:
        log.info(proc.stdout)

    if proc.stderr:
        log.info(proc.stderr)

    if proc.returncode != 0:
        raise RuntimeError(
            "SSHX installation failed."
        )

    if not SSHX_PATH.exists():
        found = shutil.which("sshx")

        if found:
            shutil.copy2(
                found,
                SSHX_PATH,
            )

    if not SSHX_PATH.exists():
        raise RuntimeError(
            "SSHX executable was not found."
        )

    SSHX_PATH.chmod(0o755)


def sshx_running():
    pid_file = STATE_DIR / "sshx.pid"

    if not pid_file.exists():
        return False

    try:
        pid = int(
            pid_file.read_text().strip()
        )

        os.kill(pid, 0)

        return True

    except Exception:
        try:
            pid_file.unlink()
        except Exception:
            pass

        return False


def start_sshx():
    install_sshx()

    if sshx_running():
        log.info(
            "SSHX already running."
        )
        return

    pid_file = STATE_DIR / "sshx.pid"
    log_file = STATE_DIR / "sshx.log"
    link_file = STATE_DIR / "sshx.link"

    log_handle = open(
        log_file,
        "a",
        buffering=1,
    )

    proc = subprocess.Popen(
        [
            str(SSHX_PATH),
            "run",
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    pid_file.write_text(
        str(proc.pid)
    )

    log.info(
        "SSHX started. PID=%s",
        proc.pid,
    )

    # Give SSHX a moment to print its URL.
    def find_link():
        for _ in range(30):
            time.sleep(1)

            try:
                text = log_file.read_text(
                    errors="ignore"
                )

                for line in text.splitlines():
                    if "https://" in line:
                        pos = line.find("https://")

                        link = line[pos:].split()[0]

                        link_file.write_text(
                            link
                        )

                        log.info(
                            "SSHX link: %s",
                            link,
                        )

                        return

            except Exception:
                pass

    threading.Thread(
        target=find_link,
        daemon=True,
    ).start()


# ============================================================
# PERSISTENT SERVICES
# ============================================================

def start_persistent_services():
    if not SERVICE_DIR.exists():
        return

    for definition in SERVICE_DIR.glob(
        "*.json"
    ):
        try:
            data = json.loads(
                definition.read_text()
            )

            name = data.get("name")
            command = data.get("command")

            if not name or not command:
                continue

            pid_file = STATE_DIR / (
                f"service-{name}.pid"
            )

            running = False

            if pid_file.exists():
                try:
                    pid = int(
                        pid_file.read_text()
                    )

                    os.kill(pid, 0)

                    running = True

                except Exception:
                    pass

            if running:
                continue

            log_file = STATE_DIR / (
                f"service-{name}.log"
            )

            handle = open(
                log_file,
                "a",
                buffering=1,
            )

            proc = subprocess.Popen(
                [
                    str(PROOT_PATH),
                    "-0",
                    "-r",
                    str(ROOTFS_DIR),
                    "-w",
                    "/root",
                    "-b",
                    "/dev",
                    "-b",
                    "/proc",
                    "-b",
                    "/sys",
                    "-b",
                    "/tmp",
                    "/bin/bash",
                    "-lc",
                    command,
                ],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

            pid_file.write_text(
                str(proc.pid)
            )

            log.info(
                "Persistent service started: %s",
                name,
            )

        except Exception:
            log.exception(
                "Could not start service: %s",
                definition,
            )


# ============================================================
# INITIALIZATION
# ============================================================

@st.cache_resource
def initialize_server():
    ensure_dirs()

    install_proot()

    restored = False

    if GITHUB_TOKEN:
        try:
            restored = restore_snapshot()
        except Exception:
            log.exception(
                "GitHub restore failed."
            )

    if not restored:
        extract_ubuntu_base()
        configure_ubuntu()
        install_base_packages()

    else:
        configure_ubuntu()

    return restored


# ============================================================
# BACKGROUND BACKUP
# ============================================================

@st.cache_resource
def start_backup_worker():
    stop_event = threading.Event()

    def worker():
        last_backup = 0

        while not stop_event.is_set():
            now = time.time()

            if (
                now - last_backup
                >= BACKUP_INTERVAL
            ):
                if GITHUB_TOKEN and ubuntu_exists():
                    success = backup_now(
                        "automatic"
                    )

                    if success:
                        last_backup = time.time()

            stop_event.wait(10)

    thread = threading.Thread(
        target=worker,
        daemon=True,
        name="SHL-GitHub-Backup",
    )

    thread.start()

    return stop_event


# ============================================================
# SYSTEM INFO
# ============================================================

def get_os_info():
    path = ROOTFS_DIR / "etc" / "os-release"

    if not path.exists():
        return "Ubuntu unavailable"

    data = path.read_text(
        errors="ignore"
    )

    for line in data.splitlines():
        if line.startswith(
            "PRETTY_NAME="
        ):
            return line.split(
                "=",
                1,
            )[1].strip('"')

    return "Ubuntu"


def get_backup_status():
    if not BACKUP_STATUS_FILE.exists():
        return {}

    try:
        return json.loads(
            BACKUP_STATUS_FILE.read_text()
        )
    except Exception:
        return {}


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="SHL Persistent Server",
    page_icon="🖥️",
    layout="wide",
)

st.title("🖥️ SHL Persistent Server")

st.caption(
    "Ubuntu 22.04 + PRoot + GitHub Persistent Storage"
)


if not GITHUB_TOKEN:
    st.error(
        "GITHUB_TOKEN در Streamlit Secrets پیدا نشد."
    )

    st.code(
        """GITHUB_TOKEN = "..."
GITHUB_REPO = "vipspots4me-hue/sfl"
GITHUB_BRANCH = "main"
GITHUB_STATE_BRANCH = "shl-persistent"
""",
        language="toml",
    )

    st.stop()


# Initialize.
try:
    restored = initialize_server()
except Exception as e:
    st.exception(e)
    st.stop()


# Start worker.
start_backup_worker()


# Start SSHX.
try:
    start_sshx()
except Exception as e:
    log.error(
        "SSHX startup failed: %s",
        e,
    )


# Start saved services.
try:
    start_persistent_services()
except Exception:
    log.exception(
        "Persistent service startup failed."
    )


# ============================================================
# STATUS
# ============================================================

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric(
        "Ubuntu",
        get_os_info(),
    )

with col2:
    st.metric(
        "Architecture",
        "x86_64",
    )

with col3:
    st.metric(
        "Persistence",
        "GitHub",
    )

with col4:
    st.metric(
        "State Branch",
        GITHUB_STATE_BRANCH,
    )


if restored:
    st.success(
        "آخرین وضعیت Ubuntu از GitHub بازیابی شد."
    )
else:
    st.info(
        "Ubuntu جدید ساخته شد. اولین Backup خودکار انجام خواهد شد."
    )


# ============================================================
# BACKUP CONTROL
# ============================================================

st.subheader("💾 Persistent Backup")

status = get_backup_status()

if status:
    st.json(status)

if st.button(
    "💾 Backup Now",
    use_container_width=True,
):
    with st.spinner(
        "در حال Backup کامل Ubuntu به GitHub..."
    ):
        success = backup_now("manual")

    if success:
        st.success(
            "Backup با موفقیت در GitHub ذخیره شد."
        )
        st.rerun()
    else:
        st.error(
            "Backup انجام نشد. Log را بررسی کن."
        )


# ============================================================
# SSHX
# ============================================================

st.subheader("🔗 SSHX")

link_file = STATE_DIR / "sshx.link"
log_file = STATE_DIR / "sshx.log"

if link_file.exists():
    link = link_file.read_text().strip()

    if link:
        st.success(
            f"SSHX: {link}"
        )

        st.code(link)

else:
    st.info(
        "لینک SSHX هنوز پیدا نشده است."
    )


if log_file.exists():
    with st.expander(
        "SSHX Log"
    ):
        text = log_file.read_text(
            errors="ignore"
        )

        st.code(
            text[-12000:]
        )


# ============================================================
# GITHUB CONFIG
# ============================================================

st.subheader("☁️ GitHub Persistence")

st.write(
    f"Repository: `{GITHUB_REPO}`"
)

st.write(
    f"Code branch: `{GITHUB_BRANCH}`"
)

st.write(
    f"Persistent branch: `{GITHUB_STATE_BRANCH}`"
)

st.success(
    "تمام Snapshotهای دائمی داخل همین GitHub Repository ذخیره می‌شوند."
)


# ============================================================
# UBUNTU TERMINAL
# ============================================================

st.subheader("🐧 Ubuntu")

command = st.text_input(
    "Ubuntu command",
    value="neofetch",
)

if st.button(
    "▶ Run",
    use_container_width=True,
):
    result = ubuntu_command(
        command,
        check=False,
        capture=True,
    )

    output = ""

    if result.stdout:
        output += result.stdout

    if result.stderr:
        output += result.stderr

    st.code(
        output or "(no output)"
    )


# ============================================================
# FINAL INFORMATION
# ============================================================

st.divider()

st.info(
    """
نکته:
تغییرات Ubuntu به صورت دوره‌ای روی Branch جداگانه
به نام `shl-persistent` ذخیره می‌شوند.

بنابراین تغییر Backup باعث Deploy مجدد `main` نمی‌شود.

در Restart/Rebuild:
1. Snapshot از GitHub خوانده می‌شود.
2. Ubuntu قبلی Restore می‌شود.
3. نصب‌ها و فایل‌ها برمی‌گردند.
4. سرویس‌های ذخیره‌شده دوباره اجرا می‌شوند.
5. SSHX دوباره اجرا می‌شود.
"""
)
