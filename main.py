import os
import re
import sys
import time
import signal
import atexit
import shutil
import tarfile
import bz2
import json
import platform
import threading
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

import streamlit as st


# ============================================================
# SHL PERSISTENT UBUNTU SERVER
#
# Streamlit
#     ↓
# Debian host
#     ↓
# PRoot
#     ↓
# Ubuntu 22.04.5
#     ↓
# SSHX
#
# Persistence:
#
# Ubuntu rootfs
#     ↓
# Restic
#     ↓
# Cloudflare R2
#
# تغییرات Ubuntu به صورت snapshot ذخیره می‌شوند.
# در اجرای بعدی آخرین snapshot restore می‌شود.
#
# نکته:
# process در حال اجرا قابل ذخیره شدن نیست.
# بنابراین فایل‌ها، packageها، configها و dataها حفظ می‌شوند
# و سرویس‌ها بعداً دوباره اجرا می‌شوند.
# ============================================================


# ============================================================
# مسیرها
# ============================================================

BASE_DIR = Path("/tmp/shl-runtime")

ROOTFS_DIR = BASE_DIR / "ubuntu"

PROOT_DIR = BASE_DIR / "proot"
PROOT_PATH = PROOT_DIR / "proot"

RESTIC_DIR = BASE_DIR / "restic"
RESTIC_PATH = RESTIC_DIR / "restic"

SSHX_DIR = Path("/home/appuser/.local/bin")
SSHX_PATH = SSHX_DIR / "sshx"

SSHX_PID_FILE = BASE_DIR / "sshx.pid"
SSHX_LINK_FILE = BASE_DIR / "sshx.link"
SSHX_LOG_FILE = BASE_DIR / "sshx.log"

SHELL_WRAPPER = BASE_DIR / "ubuntu-shell"

LOCK_FILE = BASE_DIR / ".bootstrap.lock"

BACKUP_LOCK = BASE_DIR / ".backup.lock"

BACKUP_STATE = BASE_DIR / "backup-state.json"

RESTIC_CACHE = BASE_DIR / "restic-cache"


# ============================================================
# URLs
# ============================================================

UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/download/"
    "proot-amd64"
)

SSHX_URL = (
    "https://s3.amazonaws.com/sshx/"
    "sshx-x86_64-unknown-linux-musl.tar.gz"
)

RESTIC_API_URL = (
    "https://api.github.com/repos/restic/restic/releases/latest"
)


# ============================================================
# packageهای پایه Ubuntu
# ============================================================

REQUIRED_PACKAGES = [
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
]


# ============================================================
# تنظیمات backup
# ============================================================

BACKUP_DEBOUNCE_SECONDS = 15

BACKUP_MAX_INTERVAL_SECONDS = 120

RESTIC_TAG = "shl-ubuntu"

backup_thread = None
backup_stop_event = threading.Event()
backup_request_event = threading.Event()

backup_mutex = threading.Lock()

bootstrap_mutex = threading.Lock()

shutdown_started = False


# ============================================================
# Log
# ============================================================

def log(message):
    print(
        f"[SHL] {message}",
        flush=True,
    )


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


# ============================================================
# R2 settings
# ============================================================

def get_r2_settings():

    account_id = get_secret(
        "R2_ACCOUNT_ID"
    )

    access_key = get_secret(
        "R2_ACCESS_KEY_ID"
    )

    secret_key = get_secret(
        "R2_SECRET_ACCESS_KEY"
    )

    bucket = get_secret(
        "R2_BUCKET"
    )

    password = get_secret(
        "RESTIC_PASSWORD"
    )

    if not all(
        [
            account_id,
            access_key,
            secret_key,
            bucket,
            password,
        ]
    ):
        return None

    endpoint = (
        f"https://{account_id}"
        f".r2.cloudflarestorage.com"
    )

    repository = (
        f"s3:{endpoint}/{bucket}"
    )

    return {
        "account_id": account_id,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "password": password,
        "endpoint": endpoint,
        "repository": repository,
    }


# ============================================================
# subprocess
# ============================================================

def run_command(
    command,
    timeout=None,
    env=None,
):

    if isinstance(command, str):

        shell = True
        printable = command

    else:

        shell = False
        printable = " ".join(
            str(x)
            for x in command
        )

    log(
        f"$ {printable}"
    )

    try:

        result = subprocess.run(
            command,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=env,
        )

        if result.stdout:
            print(
                result.stdout,
                flush=True,
            )

        return (
            result.returncode,
            result.stdout or "",
        )

    except subprocess.TimeoutExpired as exc:

        return (
            124,
            exc.stdout or "",
        )

    except Exception as exc:

        log(
            f"Command failed: {exc}"
        )

        return (
            1,
            str(exc),
        )


# ============================================================
# Download
# ============================================================

def download_file(
    url,
    destination,
    label,
):

    destination = Path(
        destination
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = destination.with_suffix(
        destination.suffix + ".part"
    )

    try:

        if temp.exists():
            temp.unlink()

    except Exception:
        pass

    try:

        log(
            f"Downloading {label}"
        )

        urllib.request.urlretrieve(
            url,
            str(temp),
        )

        if not temp.exists():
            return False

        if temp.stat().st_size <= 0:
            return False

        temp.replace(
            destination
        )

        return True

    except Exception as exc:

        log(
            f"{label} download failed: {exc}"
        )

        try:
            temp.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        return False


# ============================================================
# Architecture
# ============================================================

def detect_arch():

    machine = (
        platform.machine()
        .lower()
    )

    log(
        f"Architecture: {machine}"
    )

    if machine in (
        "x86_64",
        "amd64",
    ):
        return "amd64"

    if machine in (
        "aarch64",
        "arm64",
    ):
        return "arm64"

    return machine


# ============================================================
# Lock
# ============================================================

def acquire_lock(
    path,
    timeout=180,
):

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    start = time.time()

    while True:

        try:

            fd = os.open(
                path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY,
            )

            os.write(
                fd,
                str(os.getpid()).encode(),
            )

            os.close(fd)

            return True

        except FileExistsError:

            if (
                time.time() - start
                > timeout
            ):

                try:
                    path.unlink()
                except Exception:
                    pass

                continue

            time.sleep(1)

        except Exception as exc:

            log(
                f"Lock failed: {exc}"
            )

            return False


def release_lock(path):

    try:
        path.unlink()
    except Exception:
        pass


# ============================================================
# Ubuntu extraction
# ============================================================

def safe_extract(
    archive,
    destination,
):

    destination = Path(
        destination
    ).resolve()

    destination.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tarfile.open(
        archive,
        "r:gz",
    ) as tar:

        for member in tar.getmembers():

            target = (
                destination /
                member.name
            ).resolve()

            if not str(
                target
            ).startswith(
                str(destination)
                + os.sep
            ):

                raise RuntimeError(
                    "Unsafe archive path"
                )

        tar.extractall(
            destination
        )


# ============================================================
# Configure Ubuntu
# ============================================================

def configure_ubuntu():

    etc = (
        ROOTFS_DIR /
        "etc"
    )

    etc.mkdir(
        parents=True,
        exist_ok=True,
    )

    resolv = (
        etc /
        "resolv.conf"
    )

    try:

        if (
            resolv.exists()
            or resolv.is_symlink()
        ):
            resolv.unlink()

    except Exception:
        pass

    resolv.write_text(
        "nameserver 1.1.1.1\n"
        "nameserver 8.8.8.8\n"
    )

    (
        etc /
        "hostname"
    ).write_text(
        "shl-ubuntu\n"
    )

    (
        etc /
        "hosts"
    ).write_text(
        "127.0.0.1 localhost\n"
        "127.0.1.1 shl-ubuntu\n"
        "::1 localhost "
        "ip6-localhost "
        "ip6-loopback\n"
    )


# ============================================================
# Install Ubuntu
# ============================================================

def install_ubuntu():

    if (
        ROOTFS_DIR /
        "bin" /
        "bash"
    ).exists():

        log(
            "Ubuntu rootfs already exists."
        )

        return True

    archive = (
        BASE_DIR /
        "ubuntu-base.tar.gz"
    )

    extracting = (
        BASE_DIR /
        "ubuntu.extracting"
    )

    if not archive.exists():

        if not download_file(
            UBUNTU_URL,
            archive,
            "Ubuntu 22.04.5",
        ):
            return False

    if extracting.exists():

        shutil.rmtree(
            extracting,
            ignore_errors=True,
        )

    extracting.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        safe_extract(
            archive,
            extracting,
        )

        bash = (
            extracting /
            "bin" /
            "bash"
        )

        if not bash.exists():

            candidates = list(
                extracting.glob(
                    "*/bin/bash"
                )
            )

            if len(candidates) == 1:

                real_root = (
                    candidates[0]
                    .parent
                    .parent
                )

                normalized = (
                    BASE_DIR /
                    "ubuntu.normalized"
                )

                if normalized.exists():

                    shutil.rmtree(
                        normalized,
                        ignore_errors=True,
                    )

                shutil.move(
                    str(real_root),
                    str(normalized),
                )

                shutil.rmtree(
                    extracting,
                    ignore_errors=True,
                )

                normalized.rename(
                    extracting
                )

        if not (
            extracting /
            "bin" /
            "bash"
        ).exists():

            raise RuntimeError(
                "Ubuntu /bin/bash missing"
            )

        if ROOTFS_DIR.exists():

            shutil.rmtree(
                ROOTFS_DIR,
                ignore_errors=True,
            )

        extracting.rename(
            ROOTFS_DIR
        )

        configure_ubuntu()

        log(
            "Ubuntu installed."
        )

        return True

    except Exception as exc:

        log(
            f"Ubuntu install failed: {exc}"
        )

        return False


# ============================================================
# PRoot
# ============================================================

def install_proot():

    if PROOT_PATH.exists():

        try:

            PROOT_PATH.chmod(
                0o755
            )

            code, _ = run_command(
                [
                    str(PROOT_PATH),
                    "--help",
                ],
                timeout=10,
            )

            if code == 0:
                return True

        except Exception:
            pass

    PROOT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = (
        PROOT_PATH.with_suffix(
            ".part"
        )
    )

    if not download_file(
        PROOT_URL,
        temp,
        "PRoot",
    ):
        return False

    try:

        temp.chmod(
            0o755
        )

        temp.replace(
            PROOT_PATH
        )

        PROOT_PATH.chmod(
            0o755
        )

        code, _ = run_command(
            [
                str(PROOT_PATH),
                "--help",
            ],
            timeout=10,
        )

        return code == 0

    except Exception as exc:

        log(
            f"PRoot failed: {exc}"
        )

        return False


# ============================================================
# PRoot command
# ============================================================

def get_proot_command(
    command,
):

    return [
        str(PROOT_PATH),

        "-r",
        str(ROOTFS_DIR),

        "-0",

        "-w",
        "/root",

        "-b",
        "/dev",

        "-b",
        "/dev/pts",

        "-b",
        "/proc",

        "-b",
        "/sys",

        "-b",
        f"{ROOTFS_DIR}/etc/resolv.conf:"
        "/etc/resolv.conf",

        "/bin/bash",

        "-lc",

        command,
    ]


# ============================================================
# Ubuntu command
# ============================================================

def ubuntu_command(
    command,
    timeout=300,
):

    env = os.environ.copy()

    env["HOME"] = "/root"
    env["USER"] = "root"
    env["LOGNAME"] = "root"
    env["LANG"] = "C"
    env["LC_ALL"] = "C"

    return run_command(
        get_proot_command(
            command
        ),
        timeout=timeout,
        env=env,
    )


# ============================================================
# Test Ubuntu
# ============================================================

def test_ubuntu():

    code, output = ubuntu_command(
        """
echo SHL_UBUNTU_OK
id
cat /etc/os-release
uname -m
""",
        timeout=60,
    )

    return (
        code == 0
        and "SHL_UBUNTU_OK"
        in output
    )


# ============================================================
# Install packages
# ============================================================

def ensure_packages():

    packages = " ".join(
        REQUIRED_PACKAGES
    )

    command = f"""
export DEBIAN_FRONTEND=noninteractive
export LANG=C
export LC_ALL=C

MISSING=""

for PKG in {packages}; do

    if ! dpkg-query \
        -W \
        -f='${{Status}}' \
        "$PKG" \
        2>/dev/null \
        | grep -q "install ok installed"
    then
        MISSING="$MISSING $PKG"
    fi

done

if [ -n "$MISSING" ]; then

    echo "Installing:$MISSING"

    apt-get update

    apt-get install -y $MISSING

else

    echo "All required packages installed."

fi
"""

    code, _ = ubuntu_command(
        command,
        timeout=900,
    )

    return code == 0


# ============================================================
# Restic latest release
# ============================================================

def install_restic():

    if RESTIC_PATH.exists():

        try:

            code, output = run_command(
                [
                    str(RESTIC_PATH),
                    "version",
                ],
                timeout=15,
            )

            if code == 0:

                log(
                    f"Restic ready: {output.strip()}"
                )

                return True

        except Exception:
            pass

    RESTIC_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        request = urllib.request.Request(
            RESTIC_API_URL,
            headers={
                "User-Agent":
                    "SHL-Persistent-Server",
                "Accept":
                    "application/vnd.github+json",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:

            data = json.loads(
                response.read().decode()
            )

        tag = data["tag_name"]

        version = tag.lstrip("v")

        asset_name = (
            f"restic_{version}"
            "_linux_amd64.bz2"
        )

        asset_url = None

        for asset in data.get(
            "assets",
            [],
        ):

            if (
                asset.get("name")
                == asset_name
            ):

                asset_url = (
                    asset.get(
                        "browser_download_url"
                    )
                )

                break

        if not asset_url:

            raise RuntimeError(
                "Restic amd64 asset not found"
            )

        compressed = (
            RESTIC_DIR /
            f"{asset_name}"
        )

        if not download_file(
            asset_url,
            compressed,
            "Restic",
        ):
            return False

        log(
            "Extracting Restic..."
        )

        binary = (
            bz2.open(
                compressed,
                "rb",
            )
            .read()
        )

        temp_binary = (
            RESTIC_DIR /
            "restic.tmp"
        )

        temp_binary.write_bytes(
            binary
        )

        temp_binary.chmod(
            0o755
        )

        temp_binary.replace(
            RESTIC_PATH
        )

        code, output = run_command(
            [
                str(RESTIC_PATH),
                "version",
            ],
            timeout=15,
        )

        if code != 0:
            return False

        log(
            f"Restic installed: {output.strip()}"
        )

        return True

    except Exception as exc:

        log(
            f"Restic installation failed: {exc}"
        )

        return False


# ============================================================
# Restic environment
# ============================================================

def restic_env():

    cfg = get_r2_settings()

    if not cfg:
        return None

    env = os.environ.copy()

    env["RESTIC_REPOSITORY"] = (
        cfg["repository"]
    )

    env["RESTIC_PASSWORD"] = (
        cfg["password"]
    )

    env["AWS_ACCESS_KEY_ID"] = (
        cfg["access_key"]
    )

    env["AWS_SECRET_ACCESS_KEY"] = (
        cfg["secret_key"]
    )

    env["AWS_DEFAULT_REGION"] = "auto"

    env["RESTIC_CACHE_DIR"] = (
        str(RESTIC_CACHE)
    )

    return env


# ============================================================
# Restic repository
# ============================================================

def ensure_restic_repository():

    env = restic_env()

    if not env:
        return False, "R2 secrets are missing."

    RESTIC_CACHE.mkdir(
        parents=True,
        exist_ok=True,
    )

    code, output = run_command(
        [
            str(RESTIC_PATH),
            "snapshots",
            "--last",
        ],
        timeout=120,
        env=env,
    )

    if code == 0:
        return True, output

    log(
        "Restic repository does not exist."
    )

    code, output = run_command(
        [
            str(RESTIC_PATH),
            "init",
        ],
        timeout=180,
        env=env,
    )

    if code != 0:

        return (
            False,
            output,
        )

    return True, output


# ============================================================
# Backup Ubuntu
# ============================================================

def backup_ubuntu(
    reason="change",
):

    global backup_mutex

    if not ROOTFS_DIR.exists():
        return False

    env = restic_env()

    if not env:

        log(
            "R2/Restic secrets are missing."
        )

        return False

    with backup_mutex:

        if not acquire_lock(
            BACKUP_LOCK,
            timeout=30,
        ):
            return False

        try:

            log(
                f"Starting backup: {reason}"
            )

            code, output = run_command(
                [
                    str(RESTIC_PATH),
                    "backup",
                    str(ROOTFS_DIR),
                    "--tag",
                    RESTIC_TAG,
                    "--tag",
                    reason,
                    "--compression",
                    "auto",
                ],
                timeout=1800,
                env=env,
            )

            if code != 0:

                log(
                    "Backup failed."
                )

                return False

            BACKUP_STATE.write_text(
                json.dumps(
                    {
                        "time": time.time(),
                        "reason": reason,
                    },
                    indent=2,
                )
            )

            log(
                "Backup completed."
            )

            return True

        finally:

            release_lock(
                BACKUP_LOCK
            )


# ============================================================
# Restore latest Ubuntu
# ============================================================

def restore_latest():

    env = restic_env()

    if not env:
        return False

    if not install_restic():
        return False

    ok, output = (
        ensure_restic_repository()
    )

    if not ok:
        return False

    # اگر rootfs فعلی سالم باشد، restore نمی‌کنیم.
    if (
        ROOTFS_DIR /
        "bin" /
        "bash"
    ).exists():

        log(
            "Existing Ubuntu rootfs found."
        )

        return True

    log(
        "No Ubuntu rootfs. Restoring latest snapshot..."
    )

    temp_restore = (
        BASE_DIR /
        "restore"
    )

    if temp_restore.exists():

        shutil.rmtree(
            temp_restore,
            ignore_errors=True,
        )

    temp_restore.mkdir(
        parents=True,
        exist_ok=True,
    )

    code, output = run_command(
        [
            str(RESTIC_PATH),
            "restore",
            "latest",
            "--target",
            str(temp_restore),
            "--tag",
            RESTIC_TAG,
        ],
        timeout=1800,
        env=env,
    )

    if code != 0:

        log(
            "No usable backup found."
        )

        return False

    restored = (
        temp_restore /
        "tmp" /
        "shl-runtime" /
        "ubuntu"
    )

    if not restored.exists():

        restored = (
            temp_restore /
            "tmp" /
            "shl-runtime"
        )

        if (
            restored /
            "bin" /
            "bash"
        ).exists():

            pass

        else:

            # Restic may preserve the absolute
            # path differently.
            candidates = list(
                temp_restore.rglob(
                    "bin/bash"
                )
            )

            if not candidates:

                log(
                    "Restored Ubuntu rootfs not found."
                )

                return False

            restored = (
                candidates[0]
                .parent
                .parent
                .parent
            )

    if ROOTFS_DIR.exists():

        shutil.rmtree(
            ROOTFS_DIR,
            ignore_errors=True,
        )

    shutil.move(
        str(restored),
        str(ROOTFS_DIR),
    )

    configure_ubuntu()

    log(
        "Latest Ubuntu snapshot restored."
    )

    return True


# ============================================================
# SSHX
# ============================================================

def find_sshx():

    for candidate in [
        SSHX_PATH,
        Path("/usr/local/bin/sshx"),
        Path("/usr/bin/sshx"),
    ]:

        try:

            if (
                candidate.is_file()
                and os.access(
                    candidate,
                    os.X_OK,
                )
            ):
                return candidate

        except Exception:
            pass

    found = shutil.which(
        "sshx"
    )

    if found:
        return Path(found)

    return None


def install_sshx():

    existing = find_sshx()

    if existing:

        return existing

    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    archive = (
        BASE_DIR /
        "sshx.tar.gz"
    )

    if not download_file(
        SSHX_URL,
        archive,
        "SSHX",
    ):
        return None

    extract = (
        BASE_DIR /
        "sshx.extract"
    )

    if extract.exists():

        shutil.rmtree(
            extract,
            ignore_errors=True,
        )

    extract.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        with tarfile.open(
            archive,
            "r:gz",
        ) as tar:

            tar.extractall(
                extract
            )

        binary = None

        for item in extract.rglob(
            "sshx"
        ):

            if item.is_file():

                binary = item
                break

        if binary is None:
            return None

        shutil.copy2(
            binary,
            SSHX_PATH,
        )

        SSHX_PATH.chmod(
            0o755
        )

        return SSHX_PATH

    except Exception as exc:

        log(
            f"SSHX failed: {exc}"
        )

        return None


# ============================================================
# SSHX wrapper
# ============================================================

def create_shell_wrapper():

    content = f"""#!/bin/bash

exec "{PROOT_PATH}" \\
-r "{ROOTFS_DIR}" \\
-0 \\
-w /root \\
-b /dev \\
-b /dev/pts \\
-b /proc \\
-b /sys \\
-b "{ROOTFS_DIR}/etc/resolv.conf:/etc/resolv.conf" \\
/bin/bash -l
"""

    SHELL_WRAPPER.write_text(
        content
    )

    SHELL_WRAPPER.chmod(
        0o755
    )


# ============================================================
# SSHX process
# ============================================================

def get_sshx_pid():

    try:

        if not SSHX_PID_FILE.exists():
            return None

        pid = int(
            SSHX_PID_FILE
            .read_text()
            .strip()
        )

        os.kill(
            pid,
            0,
        )

        return pid

    except Exception:

        return None


def get_sshx_link():

    try:

        if not SSHX_LINK_FILE.exists():
            return None

        link = (
            SSHX_LINK_FILE
            .read_text()
            .strip()
        )

        if re.fullmatch(
            r"https://sshx\.io/s/"
            r"[A-Za-z0-9_-]+"
            r"#"
            r"[A-Za-z0-9_-]+",
            link,
        ):

            return link

    except Exception:
        pass

    return None


def extract_sshx_link(text):

    if not text:
        return None

    match = re.search(
        r"https://sshx\.io/s/"
        r"[A-Za-z0-9_-]+"
        r"#"
        r"[A-Za-z0-9_-]+",
        text,
    )

    if match:
        return match.group(0)

    return None


def start_sshx():

    pid = get_sshx_pid()
    link = get_sshx_link()

    if pid and link:

        return link

    sshx = install_sshx()

    if not sshx:
        return None

    create_shell_wrapper()

    env = os.environ.copy()

    env["SHELL"] = (
        str(SHELL_WRAPPER)
    )

    env["HOME"] = (
        "/home/appuser"
    )

    env["TERM"] = (
        "xterm-256color"
    )

    try:

        log_file = open(
            SSHX_LOG_FILE,
            "a",
            buffering=1,
        )

        process = subprocess.Popen(
            [
                str(sshx),
                "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

        SSHX_PID_FILE.write_text(
            str(process.pid)
        )

        position = 0
        deadline = (
            time.time() + 45
        )

        while (
            time.time()
            < deadline
        ):

            if process.poll() is not None:
                break

            try:

                text = (
                    SSHX_LOG_FILE
                    .read_text(
                        errors="ignore"
                    )
                )

                if len(text) > position:

                    new = (
                        text[position:]
                    )

                    position = len(text)

                    link = (
                        extract_sshx_link(
                            new
                        )
                    )

                    if link:

                        SSHX_LINK_FILE.write_text(
                            link
                        )

                        return link

            except Exception:
                pass

            time.sleep(0.5)

    except Exception as exc:

        log(
            f"SSHX start failed: {exc}"
        )

    return None


# ============================================================
# Change watcher
# ============================================================

def request_backup(reason="change"):

    try:

        state = {}

        if BACKUP_STATE.exists():

            state = json.loads(
                BACKUP_STATE.read_text()
            )

        state["requested"] = time.time()
        state["reason"] = reason

        BACKUP_STATE.write_text(
            json.dumps(
                state,
                indent=2,
            )
        )

    except Exception:
        pass

    backup_request_event.set()


def backup_worker():

    last_backup = 0

    while not backup_stop_event.is_set():

        now = time.time()

        should_backup = False

        if backup_request_event.is_set():

            if (
                now - last_backup
                >= BACKUP_DEBOUNCE_SECONDS
            ):

                should_backup = True

        if (
            now - last_backup
            >= BACKUP_MAX_INTERVAL_SECONDS
        ):

            should_backup = True

        if should_backup:

            backup_request_event.clear()

            if backup_ubuntu(
                "auto"
            ):

                last_backup = time.time()

        backup_stop_event.wait(
            3
        )


def start_backup_worker():

    global backup_thread

    if backup_thread is not None:
        return

    backup_thread = threading.Thread(
        target=backup_worker,
        name="shl-backup-worker",
        daemon=True,
    )

    backup_thread.start()


# ============================================================
# Watchdog
# ============================================================

def start_filesystem_watcher():

    try:

        from watchdog.observers import Observer
        from watchdog.events import (
            FileSystemEventHandler,
        )

    except Exception as exc:

        log(
            f"watchdog unavailable: {exc}"
        )

        return None

    class Handler(
        FileSystemEventHandler
    ):

        def on_any_event(
            self,
            event,
        ):

            if event.is_directory:
                return

            request_backup(
                "filesystem-change"
            )

    handler = Handler()

    observer = Observer()

    observer.schedule(
        handler,
        str(ROOTFS_DIR),
        recursive=True,
    )

    observer.start()

    log(
        "Filesystem watcher started."
    )

    return observer


# ============================================================
# Shutdown
# ============================================================

def shutdown_handler(
    signum=None,
    frame=None,
):

    global shutdown_started

    if shutdown_started:
        return

    shutdown_started = True

    log(
        "Shutdown detected."
    )

    backup_stop_event.set()

    # آخرین backup
    backup_ubuntu(
        "shutdown"
    )


def register_shutdown():

    atexit.register(
        shutdown_handler
    )

    try:

        signal.signal(
            signal.SIGTERM,
            shutdown_handler,
        )

    except Exception:
        pass

    try:

        signal.signal(
            signal.SIGINT,
            shutdown_handler,
        )

    except Exception:
        pass


# ============================================================
# Bootstrap کامل
# ============================================================

def bootstrap():

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    arch = detect_arch()

    if arch != "amd64":

        log(
            "Only amd64 supported."
        )

        return False

    # --------------------------------------------------------
    # Restic
    # --------------------------------------------------------

    if not install_restic():

        log(
            "Restic unavailable."
        )

        return False

    # --------------------------------------------------------
    # بررسی R2
    # --------------------------------------------------------

    cfg = get_r2_settings()

    if not cfg:

        log(
            "R2 secrets are missing."
        )

        return False

    # --------------------------------------------------------
    # repository
    # --------------------------------------------------------

    ok, output = (
        ensure_restic_repository()
    )

    if not ok:

        log(
            "Restic repository unavailable."
        )

        return False

    # --------------------------------------------------------
    # restore
    # --------------------------------------------------------

    if not (
        ROOTFS_DIR /
        "bin" /
        "bash"
    ).exists():

        restored = (
            restore_latest()
        )

        if not restored:

            log(
                "No previous Ubuntu snapshot."
            )

            if not install_ubuntu():
                return False

    # --------------------------------------------------------
    # PRoot
    # --------------------------------------------------------

    if not install_proot():
        return False

    # --------------------------------------------------------
    # Ubuntu test
    # --------------------------------------------------------

    if not test_ubuntu():

        log(
            "Ubuntu test failed."
        )

        return False

    # --------------------------------------------------------
    # packages
    # --------------------------------------------------------

    if not ensure_packages():

        log(
            "Package setup failed."
        )

        return False

    # --------------------------------------------------------
    # اولین backup
    # --------------------------------------------------------

    if not backup_ubuntu(
        "startup"
    ):

        log(
            "Initial backup failed."
        )

        return False

    return True


# ============================================================
# Streamlit
# ============================================================

st.set_page_config(
    page_title="SHL Persistent Ubuntu",
    page_icon="🖥️",
    layout="wide",
)


st.title(
    "🖥️ SHL Persistent Ubuntu Server"
)

st.caption(
    "Ubuntu 22.04.5 + PRoot + SSHX + Restic + Cloudflare R2"
)


# ============================================================
# Bootstrap
# ============================================================

with st.spinner(
    "در حال آماده‌سازی سرور..."
):

    if not bootstrap():

        st.error(
            "Bootstrap failed."
        )

        st.stop()


# ============================================================
# watcher
# ============================================================

register_shutdown()

start_backup_worker()

observer = start_filesystem_watcher()


# ============================================================
# SSHX
# ============================================================

sshx_link = start_sshx()


if sshx_link:

    st.subheader(
        "🔗 SSHX"
    )

    st.code(
        sshx_link,
        language="text",
    )

    st.success(
        "Ubuntu server آماده است."
    )

else:

    st.error(
        "SSHX failed."
    )


# ============================================================
# وضعیت
# ============================================================

col1, col2, col3 = st.columns(
    3
)


with col1:

    if st.button(
        "🔄 تست Ubuntu",
        use_container_width=True,
    ):

        code, output = (
            ubuntu_command(
                """
echo "===== USER ====="
id

echo
echo "===== OS ====="
cat /etc/os-release

echo
echo "===== NEofetch ====="
command -v neofetch || true

echo
echo "===== DISK ====="
df -h /

echo
echo "===== MEMORY ====="
free -h
""",
                timeout=60,
            )
        )

        st.code(
            output,
            language="text",
        )


with col2:

    if st.button(
        "💾 Backup الآن",
        use_container_width=True,
    ):

        if backup_ubuntu(
            "manual"
        ):

            st.success(
                "آخرین وضعیت Ubuntu ذخیره شد."
            )

        else:

            st.error(
                "Backup failed."
            )


with col3:

    if st.button(
        "📋 Snapshotها",
        use_container_width=True,
    ):

        env = restic_env()

        if env:

            code, output = (
                run_command(
                    [
                        str(RESTIC_PATH),
                        "snapshots",
                        "--tag",
                        RESTIC_TAG,
                    ],
                    timeout=120,
                    env=env,
                )
            )

            st.code(
                output,
                language="text",
            )


# ============================================================
# سرویس‌ها
# ============================================================

with st.expander(
    "⚙️ سرویس‌ها"
):

    st.info(
        "Processهای در حال اجرا بعد از reboot "
        "باقی نمی‌مانند؛ اما فایل‌ها و configها "
        "ذخیره می‌شوند. برای Xray/Argo/SPMA "
        "باید command استارت آنها را به service manager "
        "اضافه کنیم."
    )
