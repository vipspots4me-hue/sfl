import atexit
import bz2
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import streamlit as st


# ============================================================
# SHL - Persistent Ubuntu 22.04 + PRoot + SSHX + Restic + R2
# ============================================================
#
# این برنامه یک محیط Ubuntu 22.04.5 را داخل Streamlit ایجاد می‌کند.
#
# Persistence:
#   Cloudflare R2
#       ↓
#   Restic encrypted snapshots
#       ↓
#   Ubuntu rootfs
#
# نکته مهم:
# - فایل‌ها، تنظیمات، نصب پکیج‌ها و داده‌های داخل Ubuntu ذخیره می‌شوند.
# - پردازش‌های در حال اجرا بعد از نابودی Runtime قابل ذخیره نیستند.
# - سرویس‌های ثبت‌شده در /etc/shl/services بعد از راه‌اندازی مجدد
#   دوباره اجرا می‌شوند.
# - SIGKILL ناگهانی ممکن است آخرین چند ثانیه تغییرات را قبل از Snapshot
#   از بین ببرد؛ برای همین Backup خودکار با تأخیر کوتاه انجام می‌شود.
#
# سیستم قدیمی persistent-state/*.tar.gz عمداً در این نسخه وجود ندارد.
# ============================================================


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path("/tmp/shl-runtime")

ROOTFS_DIR = BASE_DIR / "ubuntu"

PRoot_DIR = BASE_DIR / "proot"
PROOT_PATH = PRoot_DIR / "proot"

RESTIC_DIR = BASE_DIR / "restic"
RESTIC_PATH = RESTIC_DIR / "restic"
RESTIC_CACHE_DIR = BASE_DIR / "restic-cache"

RESTORE_DIR = BASE_DIR / "restore"

SSHX_DIR = Path.home() / ".local" / "bin"
SSHX_PATH = SSHX_DIR / "sshx"

SSHX_PID_FILE = BASE_DIR / "sshx.pid"
SSHX_LINK_FILE = BASE_DIR / "sshx.link"
SSHX_LOG_FILE = BASE_DIR / "sshx.log"

SUPERVISOR_LOCK = BASE_DIR / "supervisor.lock"
BACKUP_LOCK = BASE_DIR / "backup.lock"

BACKUP_STATE = BASE_DIR / "backup-state.json"

SERVICE_DIR = ROOTFS_DIR / "etc" / "shl" / "services"

BOOTSTRAP_MARKER = BASE_DIR / "bootstrap-complete"

BASE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# DOWNLOAD URLS
# ============================================================

UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/download/"
    "proot-amd64"
)

SSHX_INSTALL_URL = "https://sshx.io/get"

RESTIC_API_URL = (
    "https://api.github.com/repos/restic/restic/releases/latest"
)


# ============================================================
# SETTINGS
# ============================================================

BACKUP_DEBOUNCE_SECONDS = 5
BACKUP_MAX_INTERVAL_SECONDS = 120

# پکیج‌های پایه‌ای که فقط هنگام ساخت Ubuntu جدید نصب می‌شوند.
# بعد از Restore روی محیط موجود دوباره apt اجرا نمی‌کنیم تا
# Restore واقعاً همان Snapshot را حفظ کند.
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

shutdown_started = False


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(f"[SHL] {message}", flush=True)


# ============================================================
# STREAMLIT SECRETS
# ============================================================

def get_secret(name):
    """
    ابتدا Streamlit Secrets و سپس environment variable را بررسی می‌کند.
    """

    try:
        value = st.secrets.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    except Exception:
        pass

    value = os.environ.get(name)

    if value and value.strip():
        return value.strip()

    return None


def get_r2_settings():
    """
    تنظیمات Cloudflare R2 را از Streamlit Secrets می‌خواند.
    """

    account_id = get_secret("R2_ACCOUNT_ID")
    access_key = get_secret("R2_ACCESS_KEY_ID")
    secret_key = get_secret("R2_SECRET_ACCESS_KEY")
    bucket = get_secret("R2_BUCKET")
    restic_password = get_secret("RESTIC_PASSWORD")

    missing = []

    if not account_id:
        missing.append("R2_ACCOUNT_ID")

    if not access_key:
        missing.append("R2_ACCESS_KEY_ID")

    if not secret_key:
        missing.append("R2_SECRET_ACCESS_KEY")

    if not bucket:
        missing.append("R2_BUCKET")

    if not restic_password:
        missing.append("RESTIC_PASSWORD")

    if missing:
        return None, missing

    repository = (
        f"s3:https://{account_id}.r2.cloudflarestorage.com/{bucket}"
    )

    return {
        "account_id": account_id,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "password": restic_password,
        "repository": repository,
    }, []


# ============================================================
# ARCHITECTURE
# ============================================================

def detect_arch():
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        return "x86_64"

    if machine in ("aarch64", "arm64"):
        return "aarch64"

    return machine


# ============================================================
# FILE DOWNLOAD
# ============================================================

def download_file(url, destination, executable=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_suffix(
        destination.suffix + ".download"
    )

    if temporary.exists():
        temporary.unlink()

    log(f"Downloading: {url}")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SHL-Streamlit/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        total = response.headers.get("Content-Length")

        if total:
            total = int(total)

        downloaded = 0

        with open(temporary, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)

                if not chunk:
                    break

                f.write(chunk)
                downloaded += len(chunk)

                if total:
                    percent = downloaded * 100 / total

                    print(
                        f"\r[SHL] Download: {percent:5.1f}%",
                        end="",
                        flush=True,
                    )

    if total:
        print("", flush=True)

    temporary.replace(destination)

    if executable:
        mode = destination.stat().st_mode
        destination.chmod(
            mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )


# ============================================================
# LOCK
# ============================================================

class FileLock:
    """
    قفل ساده برای جلوگیری از اجرای همزمان چند عملیات سنگین.
    """

    def __init__(self, path, timeout=300):
        self.path = Path(path)
        self.timeout = timeout
        self.fd = None

    def acquire(self):
        start = time.time()

        while True:
            try:
                self.fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )

                os.write(
                    self.fd,
                    str(os.getpid()).encode(),
                )

                return True

            except FileExistsError:

                if time.time() - start > self.timeout:
                    try:
                        self.path.unlink()
                    except Exception:
                        pass

                    continue

                time.sleep(0.5)

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass

            self.fd = None

        try:
            self.path.unlink()
        except Exception:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


# ============================================================
# COMMAND RUNNER
# ============================================================

def run_command(
    command,
    timeout=300,
    cwd=None,
    env=None,
    check=False,
):
    log("$ " + " ".join(map(str, command)))

    process = subprocess.run(
        [str(x) for x in command],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )

    output = process.stdout or ""

    if output:
        print(output, end="", flush=True)

    if check and process.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code "
            f"{process.returncode}"
        )

    return process.returncode, output


# ============================================================
# UBUNTU INSTALL
# ============================================================

def ubuntu_exists():
    return (
        ROOTFS_DIR.is_dir()
        and (ROOTFS_DIR / "bin" / "bash").exists()
        and (ROOTFS_DIR / "etc" / "os-release").exists()
    )


def install_ubuntu():
    """
    Ubuntu Base را فقط زمانی نصب می‌کند که Restore موفقی وجود نداشته باشد.
    """

    with FileLock(BASE_DIR / "ubuntu-install.lock", timeout=600):

        if ubuntu_exists():
            return

        log("No Ubuntu rootfs found.")

        archive = BASE_DIR / "ubuntu-base.tar.gz"

        if not archive.exists():
            log("Downloading Ubuntu 22.04.5 Base...")
            download_file(
                UBUNTU_URL,
                archive,
                executable=False,
            )

        temporary_rootfs = BASE_DIR / "ubuntu-new"

        if temporary_rootfs.exists():
            shutil.rmtree(temporary_rootfs)

        temporary_rootfs.mkdir(parents=True)

        log("Extracting Ubuntu 22.04.5...")

        with tarfile.open(
            archive,
            "r:gz",
        ) as tar:
            tar.extractall(temporary_rootfs)

        if not (
            temporary_rootfs / "bin" / "bash"
        ).exists():

            nested = temporary_rootfs / "ubuntu-base"

            if nested.exists():
                extracted = nested
            else:
                extracted = temporary_rootfs

        else:
            extracted = temporary_rootfs

        if ROOTFS_DIR.exists():
            shutil.rmtree(ROOTFS_DIR)

        extracted.rename(ROOTFS_DIR)

        if temporary_rootfs.exists():
            shutil.rmtree(
                temporary_rootfs,
                ignore_errors=True,
            )

        configure_ubuntu(initial=True)

        log("Ubuntu base installation completed.")


# ============================================================
# UBUNTU CONFIGURATION
# ============================================================

def configure_ubuntu(initial=False):
    """
    تنظیمات ضروری Ubuntu.

    در Restore مجدد فایل‌های موجود را بی‌دلیل overwrite نمی‌کنیم.
    این موضوع برای حفظ تغییرات کاربر مهم است.
    """

    etc = ROOTFS_DIR / "etc"
    etc.mkdir(parents=True, exist_ok=True)

    hostname = etc / "hostname"
    hosts = etc / "hosts"
    resolv = etc / "resolv.conf"

    if initial:

        hostname.write_text(
            "localhost\n",
            encoding="utf-8",
        )

        hosts.write_text(
            "127.0.0.1 localhost\n"
            "127.0.1.1 localhost\n"
            "::1 localhost ip6-localhost ip6-loopback\n",
            encoding="utf-8",
        )

        # فقط اگر فایل وجود ندارد ایجاد می‌کنیم.
        # بعد از Restore تنظیمات کاربر overwrite نمی‌شود.
        if not resolv.exists():
            resolv.write_text(
                "nameserver 1.1.1.1\n"
                "nameserver 8.8.8.8\n",
                encoding="utf-8",
            )

    SERVICE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


# ============================================================
# PROOT
# ============================================================

def install_proot():
    if PROOT_PATH.exists():
        log("PRoot already installed.")
        return

    PRoot_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    arch = detect_arch()

    if arch != "x86_64":
        raise RuntimeError(
            f"Unsupported architecture for current PRoot binary: {arch}"
        )

    log("Downloading PRoot...")

    download_file(
        PROOT_URL,
        PROOT_PATH,
        executable=True,
    )

    log("PRoot installed.")


def proot_command(command):
    """
    command را داخل Ubuntu اجرا می‌کند.
    """

    return [
        str(PROOT_PATH),
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
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "TERM=xterm-256color",
        "LANG=C.UTF-8",
        *command,
    ]


def run_ubuntu(
    command,
    timeout=300,
    check=False,
):
    return run_command(
        proot_command(command),
        timeout=timeout,
        check=check,
    )


def test_ubuntu():
    code, output = run_ubuntu(
        [
            "/bin/bash",
            "-lc",
            (
                "echo SHL_UBUNTU_OK; "
                "echo USER=$(id -un); "
                "echo UID=$(id -u); "
                "echo OS=$(grep PRETTY_NAME /etc/os-release); "
                "echo ARCH=$(uname -m)"
            ),
        ],
        timeout=60,
    )

    return code == 0, output


# ============================================================
# FIRST INSTALL PACKAGES
# ============================================================

def package_installed(package):
    code, _ = run_ubuntu(
        [
            "/bin/bash",
            "-lc",
            f"dpkg-query -W -f='${{Status}}' {package} "
            "| grep -q 'install ok installed'",
        ],
        timeout=30,
    )

    return code == 0


def ensure_base_packages():
    """
    این تابع فقط برای Ubuntu جدید استفاده می‌شود.
    بعد از Restore دیگر apt را بدون دلیل اجرا نمی‌کنیم.
    """

    marker = ROOTFS_DIR / "etc" / "shl" / ".base-packages-installed"

    if marker.exists():
        log("All required packages already installed.")
        return

    log("Installing Ubuntu base packages...")

    run_ubuntu(
        [
            "/bin/bash",
            "-lc",
            (
                "export DEBIAN_FRONTEND=noninteractive && "
                "apt-get update"
            ),
        ],
        timeout=600,
        check=True,
    )

    package_string = " ".join(
        BASE_PACKAGES
    )

    run_ubuntu(
        [
            "/bin/bash",
            "-lc",
            (
                "export DEBIAN_FRONTEND=noninteractive && "
                f"apt-get install -y {package_string}"
            ),
        ],
        timeout=1200,
        check=True,
    )

    marker.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    marker.write_text(
        f"installed_at={time.time()}\n",
        encoding="utf-8",
    )

    log("Base packages installed.")


# ============================================================
# RESTIC INSTALL
# ============================================================

def get_latest_restic_asset():

    request = urllib.request.Request(
        RESTIC_API_URL,
        headers={
            "User-Agent": "SHL-Streamlit/1.0"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:

        data = json.loads(
            response.read().decode("utf-8")
        )

    arch = detect_arch()

    if arch == "x86_64":
        wanted = "linux_amd64"

    elif arch == "aarch64":
        wanted = "linux_arm64"

    else:
        raise RuntimeError(
            f"Unsupported architecture: {arch}"
        )

    for asset in data.get("assets", []):
        name = asset.get("name", "")

        if (
            wanted in name
            and name.endswith(".bz2")
            and "restic_" in name
        ):
            return (
                data.get("tag_name", ""),
                asset.get("browser_download_url"),
            )

    raise RuntimeError(
        "Could not find Restic Linux asset."
    )


def install_restic():

    if RESTIC_PATH.exists():
        code, output = run_command(
            [
                str(RESTIC_PATH),
                "version",
            ],
            timeout=30,
        )

        if code == 0:
            log(
                "Restic already installed: "
                + output.strip().splitlines()[0]
            )
            return

    RESTIC_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log("Architecture: " + detect_arch())
    log("Downloading Restic...")

    version, url = get_latest_restic_asset()

    compressed = RESTIC_DIR / "restic.bz2"

    download_file(
        url,
        compressed,
    )

    log("Extracting Restic...")

    with bz2.open(
        compressed,
        "rb",
    ) as src, open(
        RESTIC_PATH,
        "wb",
    ) as dst:

        shutil.copyfileobj(
            src,
            dst,
        )

    RESTIC_PATH.chmod(0o755)

    try:
        compressed.unlink()
    except Exception:
        pass

    code, output = run_command(
        [
            str(RESTIC_PATH),
            "version",
        ],
        timeout=30,
        check=True,
    )

    log(
        "Restic installed: "
        + output.strip().splitlines()[0]
    )


# ============================================================
# RESTIC ENVIRONMENT
# ============================================================

def restic_env():

    settings, missing = get_r2_settings()

    if settings is None:
        raise RuntimeError(
            "R2 secrets missing: "
            + ", ".join(missing)
        )

    env = os.environ.copy()

    env["RESTIC_REPOSITORY"] = settings["repository"]
    env["RESTIC_PASSWORD"] = settings["password"]

    env["AWS_ACCESS_KEY_ID"] = settings["access_key"]
    env["AWS_SECRET_ACCESS_KEY"] = settings["secret_key"]

    # Cloudflare R2
    env["AWS_DEFAULT_REGION"] = "auto"

    env["RESTIC_CACHE_DIR"] = str(
        RESTIC_CACHE_DIR
    )

    RESTIC_CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    return env


def restic_command(args):

    return [
        str(RESTIC_PATH),
        "-o",
        "s3.region=auto",
        *args,
    ]


# ============================================================
# RESTIC REPOSITORY
# ============================================================

def restic_repository_exists():

    try:
        env = restic_env()
    except Exception:
        return False

    code, _ = run_command(
        restic_command(
            [
                "snapshots",
                "--last",
            ]
        ),
        timeout=180,
        env=env,
    )

    return code == 0


def ensure_restic_repository():

    settings, missing = get_r2_settings()

    if settings is None:
        log(
            "R2 secrets are missing: "
            + ", ".join(missing)
        )

        return False

    log("R2 configuration detected.")

    env = restic_env()

    code, output = run_command(
        restic_command(
            [
                "snapshots",
                "--last",
            ]
        ),
        timeout=180,
        env=env,
    )

    if code == 0:
        log("Restic repository is available.")
        return True

    log("Restic repository does not exist.")

    log("Initializing Restic repository...")

    code, output = run_command(
        restic_command(
            [
                "init",
            ]
        ),
        timeout=300,
        env=env,
    )

    if code != 0:
        raise RuntimeError(
            "Restic repository initialization failed."
        )

    log("Restic repository initialized.")

    return True


# ============================================================
# RESTORE
# ============================================================

def find_restored_rootfs(base):
    """
    Restic معمولاً مسیر absolute را زیر target بازسازی می‌کند:
        target/tmp/shl-runtime/ubuntu

    این تابع آن مسیر را به شکل مطمئن پیدا می‌کند.
    """

    expected = (
        base
        / "tmp"
        / "shl-runtime"
        / "ubuntu"
    )

    if (
        expected.is_dir()
        and (expected / "bin" / "bash").exists()
    ):
        return expected

    # fallback برای snapshotهایی که مسیر متفاوتی داشته‌اند.
    for bash in base.rglob("bash"):

        if bash.name != "bash":
            continue

        if bash.parent.name != "bin":
            continue

        candidate = bash.parent.parent

        if (
            (candidate / "etc" / "os-release").exists()
            and (candidate / "bin" / "bash").exists()
        ):
            return candidate

    return None


def restore_latest():

    if not ensure_restic_repository():
        return False

    env = restic_env()

    if RESTORE_DIR.exists():
        shutil.rmtree(
            RESTORE_DIR,
            ignore_errors=True,
        )

    RESTORE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log("Restoring latest Ubuntu snapshot...")

    code, output = run_command(
        restic_command(
            [
                "restore",
                "latest",
                "--target",
                str(RESTORE_DIR),
                "--tag",
                "shl-ubuntu",
            ]
        ),
        timeout=1800,
        env=env,
    )

    if code != 0:
        log("Restic restore failed.")
        return False

    restored = find_restored_rootfs(
        RESTORE_DIR
    )

    if restored is None:
        log(
            "Restore completed but Ubuntu rootfs "
            "could not be located."
        )

        return False

    log(
        "Restored Ubuntu rootfs from: "
        + str(restored)
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
        str(restored),
        str(ROOTFS_DIR),
    )

    configure_ubuntu(
        initial=False
    )

    shutil.rmtree(
        RESTORE_DIR,
        ignore_errors=True,
    )

    log("Ubuntu rootfs restored successfully.")

    return True


# ============================================================
# BACKUP
# ============================================================

def backup_ubuntu(reason="manual"):

    if not ubuntu_exists():
        log("Backup skipped: Ubuntu rootfs does not exist.")
        return False

    settings, missing = get_r2_settings()

    if settings is None:
        log(
            "Backup skipped: R2 secrets missing: "
            + ", ".join(missing)
        )

        return False

    with FileLock(
        BACKUP_LOCK,
        timeout=1800,
    ):

        env = restic_env()

        log(
            f"Creating Ubuntu persistent backup "
            f"(reason={reason})..."
        )

        command = restic_command(
            [
                "backup",
                str(ROOTFS_DIR),
                "--tag",
                "shl-ubuntu",
                "--tag",
                f"reason-{reason}",
                "--compression",
                "auto",
            ]
        )

        code, output = run_command(
            command,
            timeout=1800,
            env=env,
        )

        if code != 0:
            log("Ubuntu backup FAILED.")
            return False

        # آخرین زمان Backup موفق
        state = {
            "timestamp": time.time(),
            "reason": reason,
            "pid": os.getpid(),
        }

        BACKUP_STATE.write_text(
            json.dumps(
                state,
                indent=2,
            ),
            encoding="utf-8",
        )

        log("Ubuntu backup created.")

        return True


# ============================================================
# BACKUP WORKER
# ============================================================

class BackupController:

    def __init__(self):

        self.stop_event = threading.Event()
        self.change_event = threading.Event()

        self.lock = threading.Lock()

        self.last_request = 0.0
        self.last_backup = 0.0

        self.thread = None

    def request(self, reason="filesystem-change"):

        with self.lock:
            self.last_request = time.time()

        self.change_event.set()

    def start(self):

        if (
            self.thread is not None
            and self.thread.is_alive()
        ):
            return

        self.thread = threading.Thread(
            target=self.worker,
            name="shl-backup-worker",
            daemon=True,
        )

        self.thread.start()

        log("Backup worker started.")

    def stop(self):

        self.stop_event.set()
        self.change_event.set()

    def worker(self):

        while not self.stop_event.is_set():

            self.change_event.wait(
                timeout=10
            )

            if self.stop_event.is_set():
                break

            with self.lock:
                request_time = self.last_request

            if request_time <= 0:
                continue

            # صبر می‌کنیم تا تغییرات پشت سر هم تمام شوند.
            while not self.stop_event.is_set():

                time.sleep(
                    BACKUP_DEBOUNCE_SECONDS
                )

                with self.lock:
                    latest_request = self.last_request

                if (
                    latest_request
                    == request_time
                ):
                    break

                request_time = latest_request

            if self.stop_event.is_set():
                break

            if request_time <= self.last_backup:
                self.change_event.clear()
                continue

            success = backup_ubuntu(
                "filesystem-change"
            )

            if success:
                self.last_backup = time.time()

            self.change_event.clear()


# ============================================================
# WATCHDOG
# ============================================================

class SHLFileWatcher:

    def __init__(self, controller):
        self.controller = controller
        self.observer = None

    def start(self):

        try:
            from watchdog.events import (
                FileSystemEventHandler
            )
            from watchdog.observers import Observer

        except ImportError:
            log(
                "watchdog is not installed; "
                "automatic filesystem backup disabled."
            )
            return

        controller = self.controller

        class Handler(FileSystemEventHandler):

            def _changed(self, path):

                # فایل‌های موقت Restic یا موارد غیرمرتبط
                # نباید Watcher را وارد loop کنند.
                try:
                    resolved = Path(path).resolve()
                except Exception:
                    return

                if str(resolved).startswith(
                    str(RESTORE_DIR.resolve())
                ):
                    return

                controller.request(
                    "filesystem-change"
                )

            def on_created(self, event):
                if not event.is_directory:
                    self._changed(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    self._changed(event.src_path)

            def on_deleted(self, event):
                if not event.is_directory:
                    self._changed(event.src_path)

            def on_moved(self, event):
                self._changed(event.src_path)
                self._changed(event.dest_path)

        handler = Handler()

        self.observer = Observer()

        self.observer.schedule(
            handler,
            str(ROOTFS_DIR),
            recursive=True,
        )

        self.observer.daemon = True
        self.observer.start()

        log(
            "Filesystem watcher started."
        )

    def stop(self):

        if self.observer is not None:

            try:
                self.observer.stop()
                self.observer.join(
                    timeout=5
                )
            except Exception:
                pass


# ============================================================
# SERVICE MANAGER
# ============================================================

def create_service_manager():

    SERVICE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    script = ROOTFS_DIR / "usr" / "local" / "bin" / "shlctl"

    script.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    content = r'''#!/bin/bash

SERVICE_DIR="/etc/shl/services"

case "$1" in
    list)
        echo "===== SHL SERVICES ====="
        for f in "$SERVICE_DIR"/*.json; do
            [ -f "$f" ] || continue
            basename "$f" .json
        done
        ;;

    show)
        NAME="$2"
        cat "$SERVICE_DIR/$NAME.json"
        ;;

    *)
        echo "SHL service manager"
        echo
        echo "Usage:"
        echo "  shlctl list"
        echo "  shlctl show SERVICE"
        ;;
esac
'''

    current = None

    if script.exists():
        try:
            current = script.read_text(
                encoding="utf-8"
            )
        except Exception:
            current = None

    if current != content:
        script.write_text(
            content,
            encoding="utf-8",
        )

    script.chmod(0o755)


def list_services():

    if not SERVICE_DIR.exists():
        return []

    result = []

    for file in sorted(
        SERVICE_DIR.glob("*.json")
    ):

        try:
            data = json.loads(
                file.read_text(
                    encoding="utf-8"
                )
            )

            result.append(data)

        except Exception:
            continue

    return result


def start_service(service):

    name = service.get("name")

    if not name:
        return False, "Service name is missing."

    command = service.get("command")

    if not command:
        return False, "Service command is missing."

    workdir = service.get(
        "working_dir",
        "/root",
    )

    env_data = service.get(
        "environment",
        {},
    )

    env_exports = ""

    for key, value in env_data.items():

        safe_key = re.sub(
            r"[^A-Za-z0-9_]",
            "",
            str(key),
        )

        safe_value = str(value).replace(
            "'",
            "'\"'\"'"
        )

        env_exports += (
            f"export {safe_key}='{safe_value}'; "
        )

    # سرویس داخل Ubuntu اجرا می‌شود.
    shell = (
        "set -a; "
        f"{env_exports}"
        "set +a; "
        f"cd '{workdir}' 2>/dev/null || exit 1; "
        f"exec {command}"
    )

    log(
        f"Starting service: {name}"
    )

    process = subprocess.Popen(
        proot_command(
            [
                "/bin/bash",
                "-lc",
                shell,
            ]
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    return True, str(process.pid)


def start_persistent_services():

    services = list_services()

    if not services:
        return

    for service in services:

        if not service.get(
            "enabled",
            True,
        ):
            continue

        try:
            start_service(service)
        except Exception as exc:
            log(
                f"Could not start service "
                f"{service.get('name')}: {exc}"
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
        try:
            SSHX_PATH.chmod(0o755)
        except Exception:
            pass

        log(
            f"SSHX already installed: "
            f"{SSHX_PATH}"
        )

        return True

    log("Installing SSHX...")

    try:

        request = urllib.request.Request(
            SSHX_INSTALL_URL,
            headers={
                "User-Agent": "SHL-Streamlit/1.0"
            },
        )

        process = subprocess.run(
            [
                "bash",
                "-c",
                (
                    "curl -sSf "
                    f"{SSHX_INSTALL_URL} | "
                    "sh"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
        )

        if process.stdout:
            print(
                process.stdout,
                end="",
                flush=True,
            )

        if process.returncode != 0:
            return False

        # sshx installer معمولاً این مسیر را استفاده می‌کند.
        if not SSHX_PATH.exists():

            candidates = [
                Path.home()
                / ".local"
                / "bin"
                / "sshx",

                Path("/usr/local/bin/sshx"),
            ]

            for candidate in candidates:

                if candidate.exists():
                    if candidate != SSHX_PATH:
                        shutil.copy2(
                            candidate,
                            SSHX_PATH,
                        )
                    break

        if SSHX_PATH.exists():
            SSHX_PATH.chmod(0o755)
            log("SSHX installed.")
            return True

    except Exception as exc:
        log(
            f"SSHX installation error: {exc}"
        )

    return False


def sshx_running():

    if not SSHX_PID_FILE.exists():
        return False, None

    try:
        pid = int(
            SSHX_PID_FILE.read_text().strip()
        )

        os.kill(
            pid,
            0,
        )

        return True, pid

    except Exception:
        try:
            SSHX_PID_FILE.unlink()
        except Exception:
            pass

        return False, None


def start_sshx():

    running, pid = sshx_running()

    if running:
        log(
            f"SSHX already running: PID={pid}"
        )

        return True

    if not SSHX_PATH.exists():

        if not install_sshx():
            log("SSHX executable not found.")
            return False

    SSHX_LOG_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    log("Starting SSHX...")

    log_file = open(
        SSHX_LOG_FILE,
        "a",
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [
            str(SSHX_PATH),
            "run",
        ],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(BASE_DIR),
    )

    SSHX_PID_FILE.write_text(
        str(process.pid),
        encoding="utf-8",
    )

    log(
        f"SSHX started: PID={process.pid}"
    )

    # چند ثانیه برای دریافت لینک
    # منتظر می‌مانیم.
    for _ in range(20):

        time.sleep(0.5)

        if SSHX_LINK_FILE.exists():
            break

        try:
            text = SSHX_LOG_FILE.read_text(
                encoding="utf-8",
                errors="ignore",
            )

            # تلاش برای پیدا کردن لینک sshx
            matches = re.findall(
                r"https?://[^\s]+",
                text,
            )

            for url in matches:

                if "sshx.io" in url:

                    SSHX_LINK_FILE.write_text(
                        url.rstrip(".,)"),
                        encoding="utf-8",
                    )

                    break

        except Exception:
            pass

    return True


# ============================================================
# BOOTSTRAP
# ============================================================

def bootstrap():

    log(
        "========================================"
    )
    log(
        "SHL persistent Ubuntu bootstrap"
    )
    log(
        "========================================"
    )

    log(
        "Detected architecture: "
        + detect_arch()
    )

    # --------------------------------------------------------
    # Restic
    # --------------------------------------------------------

    install_restic()

    r2_settings, missing = get_r2_settings()

    if r2_settings is None:

        log(
            "R2 secrets are missing: "
            + ", ".join(missing)
        )

    else:

        # ----------------------------------------------------
        # Restore FIRST
        # ----------------------------------------------------

        if not ubuntu_exists():

            log(
                "Ubuntu rootfs does not exist. "
                "Trying Restic restore..."
            )

            restored = restore_latest()

            if not restored:
                log(
                    "No usable Restic snapshot found."
                )

        # ----------------------------------------------------
        # Fresh install only if Restore failed
        # ----------------------------------------------------

        if not ubuntu_exists():

            log(
                "Creating fresh Ubuntu environment..."
            )

            install_ubuntu()

            # فقط محیط جدید
            ensure_base_packages()

            configure_ubuntu(
                initial=False
            )

            create_service_manager()

            # اولین Backup
            if r2_settings is not None:
                backup_ubuntu(
                    "initial"
                )

        else:

            log(
                "Existing/restored Ubuntu environment found."
            )

            configure_ubuntu(
                initial=False
            )

            create_service_manager()

            # Restore شده است؛ پکیج‌ها را دوباره نصب نمی‌کنیم.
            # این قسمت باعث حفظ دقیق Snapshot می‌شود.

    # --------------------------------------------------------
    # PRoot
    # --------------------------------------------------------

    install_proot()

    ok, output = test_ubuntu()

    if not ok:
        raise RuntimeError(
            "Ubuntu/PRoot test failed."
        )

    # --------------------------------------------------------
    # Persistent services
    # --------------------------------------------------------

    start_persistent_services()

    # --------------------------------------------------------
    # SSHX
    # --------------------------------------------------------

    start_sshx()

    # --------------------------------------------------------
    # Backup supervisor
    # --------------------------------------------------------

    start_backup_system()

    log(
        "SHL Ubuntu environment is ready."
    )


# ============================================================
# GLOBAL SUPERVISOR
# ============================================================

@st.cache_resource
def start_backup_system():

    controller = BackupController()

    controller.start()

    watcher = SHLFileWatcher(
        controller
    )

    watcher.start()

    # این دو object با cache_resource
    # در rerunهای Streamlit دوباره ساخته نمی‌شوند.
    supervisor = {
        "controller": controller,
        "watcher": watcher,
    }

    return supervisor


# ============================================================
# SHUTDOWN
# ============================================================

def final_backup():

    global shutdown_started

    if shutdown_started:
        return

    shutdown_started = True

    log(
        "Shutdown detected. "
        "Creating final persistent backup..."
    )

    try:
        backup_ubuntu(
            "shutdown"
        )
    except Exception as exc:
        log(
            f"Final backup failed: {exc}"
        )


def signal_handler(signum, frame):

    final_backup()

    # به Streamlit اجازه می‌دهیم shutdown خودش را انجام دهد.
    raise SystemExit(0)


def install_shutdown_handlers():

    try:
        signal.signal(
            signal.SIGTERM,
            signal_handler,
        )
    except Exception:
        pass

    try:
        signal.signal(
            signal.SIGINT,
            signal_handler,
        )
    except Exception:
        pass

    # atexit فقط برای shutdown عادی است.
    atexit.register(
        final_backup
    )


# ============================================================
# RUN BOOTSTRAP ONLY ONCE PER STREAMLIT PROCESS
# ============================================================

@st.cache_resource
def run_bootstrap_once():

    bootstrap()

    return True


install_shutdown_handlers()

try:
    run_bootstrap_once()
except Exception as exc:

    log(
        f"BOOTSTRAP ERROR: {exc}"
    )

    st.error(
        f"SHL bootstrap failed: {exc}"
    )

    st.stop()


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="SHL Persistent Ubuntu",
    page_icon="🐧",
    layout="wide",
)


st.title(
    "🐧 SHL Persistent Ubuntu"
)

st.caption(
    "Ubuntu 22.04.5 + PRoot + SSHX + Restic + Cloudflare R2"
)


# ============================================================
# STATUS
# ============================================================

st.subheader(
    "Environment"
)

col1, col2, col3 = st.columns(3)

with col1:
    st.write(
        "**Ubuntu:**",
        "22.04.5 LTS"
        if ubuntu_exists()
        else "Not found",
    )

with col2:
    st.write(
        "**Architecture:**",
        detect_arch(),
    )

with col3:

    running, pid = sshx_running()

    st.write(
        "**SSHX:**",
        f"Running (PID {pid})"
        if running
        else "Not running",
    )


# ============================================================
# R2 STATUS
# ============================================================

st.subheader(
    "☁️ Persistent Storage"
)

settings, missing = get_r2_settings()

if settings is None:

    st.error(
        "Cloudflare R2 is not configured."
    )

    st.code(
        "\n".join(
            [
                "R2_ACCOUNT_ID = \"...\"",
                "R2_ACCESS_KEY_ID = \"...\"",
                "R2_SECRET_ACCESS_KEY = \"...\"",
                "R2_BUCKET = \"...\"",
                "RESTIC_PASSWORD = \"...\"",
            ]
        ),
        language="toml",
    )

    st.warning(
        "Missing: "
        + ", ".join(missing)
    )

else:

    st.success(
        f"R2 configured — bucket: {settings['bucket']}"
    )

    if BACKUP_STATE.exists():

        try:
            state = json.loads(
                BACKUP_STATE.read_text(
                    encoding="utf-8"
                )
            )

            timestamp = state.get(
                "timestamp",
                0,
            )

            reason = state.get(
                "reason",
                "unknown",
            )

            st.write(
                "Last successful local backup record:",
                time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(timestamp),
                ),
                f"({reason})",
            )

        except Exception:
            pass


# ============================================================
# ACTIONS
# ============================================================

st.subheader(
    "⚙️ Controls"
)

col1, col2, col3 = st.columns(3)


with col1:

    if st.button(
        "🧪 Test Ubuntu",
        use_container_width=True,
    ):

        ok, output = test_ubuntu()

        if ok:
            st.success(
                "Ubuntu test successful."
            )

            st.code(
                output,
                language="text",
            )

        else:
            st.error(
                "Ubuntu test failed."
            )

            st.code(
                output,
                language="text",
            )


with col2:

    if st.button(
        "☁️ Backup Now",
        use_container_width=True,
    ):

        with st.spinner(
            "Creating Restic snapshot..."
        ):

            success = backup_ubuntu(
                "manual"
            )

        if success:
            st.success(
                "Backup completed successfully."
            )

        else:
            st.error(
                "Backup failed."
            )


with col3:

    if st.button(
        "📸 Restic Snapshots",
        use_container_width=True,
    ):

        try:

            env = restic_env()

            code, output = run_command(
                restic_command(
                    [
                        "snapshots",
                        "--tag",
                        "shl-ubuntu",
                    ]
                ),
                timeout=180,
                env=env,
            )

            if code == 0:
                st.code(
                    output,
                    language="text",
                )
            else:
                st.error(
                    "Could not read snapshots."
                )

        except Exception as exc:

            st.error(
                str(exc)
            )


# ============================================================
# SSHX
# ============================================================

st.subheader(
    "🔐 SSHX"
)

if SSHX_LINK_FILE.exists():

    try:
        sshx_link = SSHX_LINK_FILE.read_text(
            encoding="utf-8"
        ).strip()
    except Exception:
        sshx_link = ""

    if sshx_link:

        st.success(
            "SSHX link:"
        )

        st.code(
            sshx_link,
            language="text",
        )

else:

    st.info(
        "SSHX link has not been detected yet."
    )


if SSHX_LOG_FILE.exists():

    with st.expander(
        "SSHX log"
    ):

        try:
            log_text = SSHX_LOG_FILE.read_text(
                encoding="utf-8",
                errors="ignore",
            )

            st.code(
                log_text[-12000:],
                language="text",
            )

        except Exception:
            pass


# ============================================================
# SERVICE LIST
# ============================================================

st.subheader(
    "🔄 Persistent Services"
)

services = list_services()

if not services:

    st.info(
        "No persistent services are registered yet."
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

        st.write(
            f"**{name}** — "
            f"{'enabled' if enabled else 'disabled'}"
        )

        st.code(
            command,
            language="bash",
        )


# ============================================================
# FILESYSTEM INFO
# ============================================================

st.subheader(
    "💾 Persistence Architecture"
)

st.markdown(
    """
**Runtime**

`Streamlit Cloud`

↓

**Userspace Linux**

`PRoot`

↓

**Ubuntu**

`Ubuntu 22.04.5 LTS`

↓

**Persistent storage**

`Restic`

↓

**Cloud storage**

`Cloudflare R2`

### Automatic backup

هر تغییر در فایل‌های Ubuntu توسط `watchdog` تشخیص داده می‌شود.

بعد از حدود **۵ ثانیه بدون تغییر جدید** یک Snapshot ایجاد می‌شود.

همچنین حداکثر فاصله Backup خودکار در نظر گرفته شده است.

### Restore

اگر Runtime جدید باشد و `/tmp/shl-runtime/ubuntu` وجود نداشته باشد:

1. Restic repository بررسی می‌شود.
2. آخرین Snapshot دارای tag `shl-ubuntu` پیدا می‌شود.
3. Ubuntu rootfs از R2 بازیابی می‌شود.
4. سپس PRoot روی همان rootfs اجرا می‌شود.

### نکته

پردازش‌های در حال اجرا قابل Snapshot شدن نیستند.

مثلاً اگر Xray یا Argo در حال اجرا باشد، فایل‌ها و تنظیمات آن‌ها ذخیره می‌شود؛
اما خود Process بعد از نابودی Runtime باید دوباره اجرا شود.
سرویس‌های ثبت‌شده در `/etc/shl/services` برای همین منظور هستند.
"""
)


# ============================================================
# DEBUG
# ============================================================

with st.expander(
    "🔧 Debug information"
):

    st.write(
        "BASE_DIR:",
        str(BASE_DIR),
    )

    st.write(
        "ROOTFS_DIR:",
        str(ROOTFS_DIR),
    )

    st.write(
        "PRoot:",
        str(PROOT_PATH),
    )

    st.write(
        "Restic:",
        str(RESTIC_PATH),
    )

    st.write(
        "SSHX:",
        str(SSHX_PATH),
    )

    st.write(
        "Service directory:",
        str(SERVICE_DIR),
    )

    if RESTIC_PATH.exists():

        code, output = run_command(
            [
                str(RESTIC_PATH),
                "version",
            ],
            timeout=30,
        )

        st.code(
            output,
            language="text",
        )
