import os
import re
import time
import tarfile
import shutil
import platform
import subprocess
import threading
import urllib.request
import fcntl
from pathlib import Path

import streamlit as st


# ============================================================
# SHL
# Streamlit → Persistent Storage → Ubuntu 22.04.5 → PRoot → SSHX
#
# IMPORTANT:
# All Ubuntu filesystem changes are made directly inside:
#
#   /mount/admin/shl-runtime/ubuntu/
#
# This directory must be on truly persistent storage.
#
# We NEVER recreate an existing rootfs merely because Streamlit
# restarted.
# ============================================================


# ============================================================
# PERSISTENT STORAGE
# ============================================================

PERSISTENT_BASE = Path("/mount/admin/shl-runtime")

BASE_DIR = PERSISTENT_BASE

ROOTFS_DIR = BASE_DIR / "ubuntu"

BIN_DIR = BASE_DIR / "bin"

PROOT_DIR = BIN_DIR
PROOT_PATH = PROOT_DIR / "proot"

SSHX_DIR = BIN_DIR
SSHX_PATH = SSHX_DIR / "sshx"

WRAPPER_DIR = BASE_DIR / "wrappers"
UBUNTU_SHELL_WRAPPER = WRAPPER_DIR / "ubuntu-shell"

STATE_DIR = BASE_DIR / "state"

BOOTSTRAP_LOCK_FILE = BASE_DIR / ".bootstrap.lock"
SSHX_START_LOCK_FILE = BASE_DIR / "sshx.start.lock"

STATE_FILE = STATE_DIR / "bootstrap.ok"
ROOTFS_STATE_FILE = STATE_DIR / "rootfs.state"

PERSISTENCE_TEST_FILE = ROOTFS_DIR / "root" / ".shl-persistence-test"

SSHX_PID_FILE = STATE_DIR / "sshx.pid"
SSHX_LINK_FILE = STATE_DIR / "sshx.link"
SSHX_LOG_FILE = STATE_DIR / "sshx.log"


# ============================================================
# DOWNLOAD FILES
# ============================================================

DOWNLOAD_DIR = BASE_DIR / "downloads"

UBUNTU_ARCHIVE = (
    DOWNLOAD_DIR /
    "ubuntu-22.04.5-base-amd64.tar.gz"
)

SSHX_ARCHIVE = (
    DOWNLOAD_DIR /
    "sshx-x86_64-unknown-linux-musl.tar.gz"
)


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


# ============================================================
# INTERNAL LOCK
# ============================================================

bootstrap_lock = threading.Lock()


# ============================================================
# LOG
# ============================================================

def log(message):

    print(
        f"[SHL] {message}",
        flush=True,
    )


# ============================================================
# DIRECTORY SETUP
# ============================================================

def prepare_storage():

    try:

        BASE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        DOWNLOAD_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        STATE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        BIN_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        WRAPPER_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        log(
            f"Persistent SHL storage: {BASE_DIR}"
        )

        return True

    except Exception as exc:

        log(
            "Cannot prepare persistent storage: "
            f"{exc}"
        )

        return False


# ============================================================
# COMMAND ON HOST
# ============================================================

def run_command(
    command,
    timeout=None,
    env=None,
):

    if isinstance(command, str):

        shell_command = command

    else:

        shell_command = " ".join(
            str(x)
            for x in command
        )

    log(
        f"$ {shell_command}"
    )

    try:

        result = subprocess.run(
            command,
            shell=isinstance(
                command,
                str,
            ),
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

        output = exc.stdout or ""

        log(
            f"Command timeout after {timeout} seconds."
        )

        return (
            124,
            output,
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
# DOWNLOAD
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

    part_file = destination.with_suffix(
        destination.suffix + ".part"
    )

    try:

        if part_file.exists():

            part_file.unlink()

    except Exception:

        pass

    try:

        log(
            f"Downloading {label}: {url}"
        )

        def progress_hook(
            block_num,
            block_size,
            total_size,
        ):

            if total_size and total_size > 0:

                downloaded = (
                    block_num * block_size
                )

                percent = min(
                    downloaded * 100.0 / total_size,
                    100.0,
                )

                bucket = int(
                    percent / 3.5
                )

                if not hasattr(
                    progress_hook,
                    "last_bucket",
                ):

                    progress_hook.last_bucket = -1

                if (
                    bucket
                    != progress_hook.last_bucket
                    or percent >= 100
                ):

                    progress_hook.last_bucket = bucket

                    log(
                        f"{label}: "
                        f"{percent:.1f}%"
                    )

        urllib.request.urlretrieve(
            url,
            str(part_file),
            reporthook=progress_hook,
        )

        if not part_file.exists():

            raise RuntimeError(
                f"{label} download produced no file"
            )

        if part_file.stat().st_size <= 0:

            raise RuntimeError(
                f"{label} download produced empty file"
            )

        part_file.replace(
            destination
        )

        log(
            f"{label} download completed."
        )

        return True

    except Exception as exc:

        log(
            f"{label} download failed: {exc}"
        )

        try:

            if part_file.exists():

                part_file.unlink()

        except Exception:

            pass

        return False


# ============================================================
# ARCHITECTURE
# ============================================================

def detect_arch():

    machine = platform.machine().lower()

    log(
        f"Detected architecture: {machine}"
    )

    if machine in (
        "x86_64",
        "amd64",
    ):

        arch = "amd64"

    elif machine in (
        "aarch64",
        "arm64",
    ):

        arch = "arm64"

    else:

        arch = machine

    log(
        f"Architecture selected: {arch}"
    )

    return arch


# ============================================================
# BOOTSTRAP LOCK
# ============================================================

def acquire_bootstrap_lock(
    timeout=300,
):

    if not prepare_storage():

        return False

    start = time.time()

    while True:

        try:

            fd = os.open(
                BOOTSTRAP_LOCK_FILE,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY,
            )

            os.write(
                fd,
                str(os.getpid()).encode(),
            )

            os.close(
                fd
            )

            return True

        except FileExistsError:

            if (
                time.time() - start
                > timeout
            ):

                log(
                    "Bootstrap lock appears stale."
                )

                try:

                    BOOTSTRAP_LOCK_FILE.unlink()

                except Exception:

                    pass

                continue

            time.sleep(
                1
            )

        except Exception as exc:

            log(
                f"Cannot acquire bootstrap lock: {exc}"
            )

            return False


def release_bootstrap_lock():

    try:

        BOOTSTRAP_LOCK_FILE.unlink()

    except FileNotFoundError:

        pass

    except Exception:

        pass


# ============================================================
# SAFE TAR EXTRACTION
# ============================================================

def safe_extract(
    tar_path,
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
        tar_path,
        "r:gz",
    ) as tar:

        for member in tar.getmembers():

            target = (
                destination
                / member.name
            ).resolve()

            if not (
                str(target) == str(destination)
                or
                str(target).startswith(
                    str(destination) + os.sep
                )
            ):

                raise RuntimeError(
                    "Unsafe path inside archive: "
                    f"{member.name}"
                )

        tar.extractall(
            destination
        )


# ============================================================
# ROOTFS VALIDATION
# ============================================================

def rootfs_exists():

    return (
        ROOTFS_DIR.exists()
        and
        (ROOTFS_DIR / "bin" / "bash").exists()
        and
        (ROOTFS_DIR / "etc" / "os-release").exists()
    )


def rootfs_is_valid():

    if not rootfs_exists():

        return False

    required = [

        ROOTFS_DIR / "bin" / "bash",

        ROOTFS_DIR / "etc" / "os-release",

        ROOTFS_DIR / "etc",

        ROOTFS_DIR / "usr",

        ROOTFS_DIR / "var",

        ROOTFS_DIR / "root",

    ]

    for path in required:

        if not path.exists():

            return False

    return True


# ============================================================
# ROOTFS STATE
# ============================================================

def write_rootfs_state():

    try:

        STATE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        ROOTFS_STATE_FILE.write_text(
            "SHL Ubuntu 22.04.5 persistent rootfs\n"
            f"created_or_verified={int(time.time())}\n"
            f"rootfs={ROOTFS_DIR}\n"
        )

        return True

    except Exception as exc:

        log(
            f"Cannot write rootfs state: {exc}"
        )

        return False


# ============================================================
# PERSISTENCE TEST
# ============================================================

def ensure_persistence_test():

    try:

        test_dir = (
            ROOTFS_DIR / "root"
        )

        test_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        if not PERSISTENCE_TEST_FILE.exists():

            PERSISTENCE_TEST_FILE.write_text(
                "SHL-PERSISTENCE-TEST\n"
            )

            log(
                "Created persistent filesystem test."
            )

        return True

    except Exception as exc:

        log(
            f"Cannot create persistence test: {exc}"
        )

        return False


def check_persistence_test():

    try:

        if not PERSISTENCE_TEST_FILE.exists():

            return False

        content = (
            PERSISTENCE_TEST_FILE
            .read_text(
                errors="ignore"
            )
            .strip()
        )

        return (
            content
            == "SHL-PERSISTENCE-TEST"
        )

    except Exception:

        return False


# ============================================================
# INSTALL UBUNTU
#
# VERY IMPORTANT:
#
# Existing ROOTFS is NEVER deleted just because the Streamlit
# application restarted.
#
# Only an actually missing rootfs will trigger extraction.
# ============================================================

def install_ubuntu():

    if rootfs_is_valid():

        log(
            "Existing Ubuntu rootfs found."
        )

        log(
            f"Using persistent rootfs: {ROOTFS_DIR}"
        )

        write_rootfs_state()

        ensure_persistence_test()

        return True

    log(
        "Ubuntu rootfs does not exist."
    )

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    extracting_dir = (
        BASE_DIR /
        "ubuntu.extracting"
    )

    # --------------------------------------------------------
    # Incomplete previous extraction
    # --------------------------------------------------------

    if extracting_dir.exists():

        log(
            "Removing incomplete Ubuntu extraction..."
        )

        shutil.rmtree(
            extracting_dir,
            ignore_errors=True,
        )

    # --------------------------------------------------------
    # Download Ubuntu only if necessary
    # --------------------------------------------------------

    if (
        not UBUNTU_ARCHIVE.exists()
        or
        UBUNTU_ARCHIVE.stat().st_size <= 0
    ):

        if not download_file(
            UBUNTU_URL,
            UBUNTU_ARCHIVE,
            "Ubuntu 22.04.5",
        ):

            return False

    if (
        not UBUNTU_ARCHIVE.exists()
        or
        UBUNTU_ARCHIVE.stat().st_size <= 0
    ):

        log(
            "Ubuntu archive is invalid."
        )

        return False

    try:

        log(
            "Extracting Ubuntu 22.04.5..."
        )

        extracting_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        safe_extract(
            UBUNTU_ARCHIVE,
            extracting_dir,
        )

        # ----------------------------------------------------
        # Normalize archive
        # ----------------------------------------------------

        if not (
            extracting_dir
            / "bin"
            / "bash"
        ).exists():

            candidates = list(
                extracting_dir.glob(
                    "*/bin/bash"
                )
            )

            if len(candidates) == 1:

                real_root = (
                    candidates[0]
                    .parent
                    .parent
                )

                normalized_dir = (
                    BASE_DIR /
                    "ubuntu.normalized"
                )

                if normalized_dir.exists():

                    shutil.rmtree(
                        normalized_dir,
                        ignore_errors=True,
                    )

                shutil.move(
                    str(real_root),
                    str(normalized_dir),
                )

                shutil.rmtree(
                    extracting_dir,
                    ignore_errors=True,
                )

                normalized_dir.rename(
                    extracting_dir
                )

        # ----------------------------------------------------
        # Validate before installing
        # ----------------------------------------------------

        if not (
            extracting_dir
            / "bin"
            / "bash"
        ).exists():

            raise RuntimeError(
                "Ubuntu rootfs extraction did not "
                "contain /bin/bash"
            )

        # ----------------------------------------------------
        # DNS
        # ----------------------------------------------------

        etc_dir = (
            extracting_dir /
            "etc"
        )

        etc_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        resolv = (
            etc_dir /
            "resolv.conf"
        )

        try:

            if (
                resolv.exists()
                or
                resolv.is_symlink()
            ):

                resolv.unlink()

        except Exception:

            pass

        resolv.write_text(
            "nameserver 1.1.1.1\n"
            "nameserver 8.8.8.8\n"
        )

        try:

            resolv.chmod(
                0o644
            )

        except Exception:

            pass

        # ----------------------------------------------------
        # hostname
        # ----------------------------------------------------

        try:

            (
                etc_dir /
                "hostname"
            ).write_text(
                "shl-ubuntu\n"
            )

        except Exception:

            pass

        # ----------------------------------------------------
        # hosts
        # ----------------------------------------------------

        try:

            (
                etc_dir /
                "hosts"
            ).write_text(
                "127.0.0.1 localhost\n"
                "127.0.1.1 shl-ubuntu\n"
                "::1 localhost "
                "ip6-localhost "
                "ip6-loopback\n"
            )

        except Exception:

            pass

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Never overwrite an existing persistent rootfs.
        # ----------------------------------------------------

        if ROOTFS_DIR.exists():

            if rootfs_is_valid():

                log(
                    "A valid persistent Ubuntu rootfs appeared "
                    "during installation. Keeping it."
                )

                shutil.rmtree(
                    extracting_dir,
                    ignore_errors=True,
                )

                write_rootfs_state()
                ensure_persistence_test()

                return True

            log(
                "Existing rootfs is invalid. "
                "Replacing it with fresh installation."
            )

            shutil.rmtree(
                ROOTFS_DIR,
                ignore_errors=True,
            )

        extracting_dir.rename(
            ROOTFS_DIR
        )

        # ----------------------------------------------------
        # Persistence marker
        # ----------------------------------------------------

        write_rootfs_state()

        ensure_persistence_test()

        log(
            "Ubuntu rootfs installed into persistent storage."
        )

        log(
            f"ROOTFS = {ROOTFS_DIR}"
        )

        return True

    except Exception as exc:

        log(
            f"Ubuntu extraction failed: {exc}"
        )

        if extracting_dir.exists():

            shutil.rmtree(
                extracting_dir,
                ignore_errors=True,
            )

        return False


# ============================================================
# INSTALL PROOT
# ============================================================

def install_proot():

    PROOT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if PROOT_PATH.exists():

        try:

            PROOT_PATH.chmod(
                0o755
            )

            result = subprocess.run(
                [
                    str(PROOT_PATH),
                    "--help",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:

                log(
                    f"Persistent PRoot already installed: "
                    f"{PROOT_PATH}"
                )

                return True

        except Exception:

            pass

    temp_file = (
        PROOT_PATH.with_suffix(
            ".part"
        )
    )

    try:

        if temp_file.exists():

            temp_file.unlink()

    except Exception:

        pass

    log(
        f"Downloading persistent PRoot: {PROOT_URL}"
    )

    if not download_file(
        PROOT_URL,
        temp_file,
        "PRoot amd64",
    ):

        return False

    try:

        temp_file.chmod(
            0o755
        )

        temp_file.replace(
            PROOT_PATH
        )

        PROOT_PATH.chmod(
            0o755
        )

        result = subprocess.run(
            [
                str(PROOT_PATH),
                "--help",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:

            log(
                "PRoot test failed."
            )

            return False

        log(
            "Persistent PRoot installed successfully."
        )

        return True

    except Exception as exc:

        log(
            f"PRoot installation failed: {exc}"
        )

        return False


# ============================================================
# PROOT COMMAND
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

        "/bin/bash",

        "-lc",

        command,
    ]


# ============================================================
# UBUNTU COMMAND
# ============================================================

def ubuntu_command(
    command,
    timeout=300,
    env=None,
):

    cmd = get_proot_command(
        command
    )

    merged_env = os.environ.copy()

    if env:

        merged_env.update(
            env
        )

    merged_env["HOME"] = "/root"
    merged_env["USER"] = "root"
    merged_env["LOGNAME"] = "root"
    merged_env["LANG"] = "C"
    merged_env["LC_ALL"] = "C"

    try:

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=merged_env,
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

        output = exc.stdout or ""

        log(
            "Ubuntu command timeout after "
            f"{timeout} seconds."
        )

        return (
            124,
            output,
        )

    except Exception as exc:

        log(
            f"Ubuntu command failed: {exc}"
        )

        return (
            1,
            str(exc),
        )


# ============================================================
# TEST UBUNTU
# ============================================================

def test_ubuntu():

    command = r"""
echo SHL_UBUNTU_OK
echo "USER=$(id -un)"
echo "UID=$(id -u)"
echo "GID=$(id -g)"
echo "GROUPS=$(id -G)"
echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
echo "ARCH=$(uname -m)"

echo
echo "ROOTFS:"
df -h /

echo
echo "PERSISTENCE TEST:"
if [ -f /root/.shl-persistence-test ]; then
    cat /root/.shl-persistence-test
else
    echo "PERSISTENCE_TEST_MISSING"
fi

echo
echo "DNS:"
cat /etc/resolv.conf 2>/dev/null || true
"""

    code, output = ubuntu_command(
        command,
        timeout=60,
    )

    if code != 0:

        return (
            False,
            output,
        )

    if "SHL_UBUNTU_OK" not in output:

        return (
            False,
            output,
        )

    return (
        True,
        output,
    )


# ============================================================
# INSTALL BASE UBUNTU TOOLS
#
# This installation happens INSIDE the persistent ROOTFS.
#
# Marker is also stored INSIDE Ubuntu:
#
#   /root/.shl_tools_ready
#
# ============================================================

def install_ubuntu_tools():

    marker = (
        ROOTFS_DIR
        / "root"
        / ".shl_tools_ready"
    )

    if marker.exists():

        log(
            "Ubuntu basic tools already installed."
        )

        return True

    log(
        "Installing basic Ubuntu tools "
        "inside persistent rootfs..."
    )

    command = r"""
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export LANG=C

mkdir -p /var/lib/apt/lists/partial
mkdir -p /var/cache/apt/archives/partial

apt-get update

apt-get install -y \
    bash \
    ca-certificates \
    curl \
    wget \
    unzip \
    tar \
    gzip \
    xz-utils \
    bzip2 \
    procps \
    iproute2 \
    iputils-ping \
    net-tools \
    dnsutils \
    python3 \
    python3-pip \
    git \
    openssl \
    netcat-openbsd \
    nano \
    vim-tiny

mkdir -p /root

echo "SHL Ubuntu base tools installed at $(date -u)" \
    > /root/.shl_tools_ready
"""

    code, output = ubuntu_command(
        command,
        timeout=1200,
    )

    if code != 0:

        log(
            "Ubuntu tools installation failed."
        )

        return False

    log(
        "Ubuntu tools installed permanently "
        "inside rootfs."
    )

    return True


# ============================================================
# FIND SSHX
# ============================================================

def find_sshx():

    candidates = [

        SSHX_PATH,

        Path("/usr/local/bin/sshx"),

        Path("/usr/bin/sshx"),

        Path("/home/appuser/.local/bin/sshx"),

    ]

    for candidate in candidates:

        try:

            if (
                candidate.is_file()
                and
                os.access(
                    candidate,
                    os.X_OK,
                )
            ):

                return candidate

        except (
            PermissionError,
            OSError,
        ):

            continue

    try:

        found = shutil.which(
            "sshx"
        )

        if found:

            found_path = Path(
                found
            )

            if (
                found_path.is_file()
                and
                os.access(
                    found_path,
                    os.X_OK,
                )
            ):

                return found_path

    except Exception:

        pass

    return None


# ============================================================
# INSTALL SSHX INTO PERSISTENT STORAGE
# ============================================================

def install_sshx():

    existing = find_sshx()

    if existing:

        log(
            f"Persistent SSHX already installed: {existing}"
        )

        return existing

    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        not SSHX_ARCHIVE.exists()
        or
        SSHX_ARCHIVE.stat().st_size <= 0
    ):

        if not download_file(
            SSHX_URL,
            SSHX_ARCHIVE,
            "SSHX",
        ):

            return None

    extract_dir = (
        BASE_DIR /
        "sshx.extracting"
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

    try:

        with tarfile.open(
            SSHX_ARCHIVE,
            "r:gz",
        ) as tar:

            # SSHX archive is trusted source, but still use
            # path validation instead of blind extraction.

            for member in tar.getmembers():

                target = (
                    extract_dir /
                    member.name
                ).resolve()

                if not (
                    str(target) == str(extract_dir.resolve())
                    or
                    str(target).startswith(
                        str(extract_dir.resolve())
                        + os.sep
                    )
                ):

                    raise RuntimeError(
                        "Unsafe SSHX archive path: "
                        f"{member.name}"
                    )

            tar.extractall(
                extract_dir
            )

        sshx_binary = None

        for path in extract_dir.rglob(
            "sshx"
        ):

            if path.is_file():

                sshx_binary = path

                break

        if sshx_binary is None:

            raise RuntimeError(
                "SSHX binary not found inside archive."
            )

        temp_binary = (
            SSHX_PATH.with_suffix(
                ".part"
            )
        )

        shutil.copy2(
            sshx_binary,
            temp_binary,
        )

        temp_binary.chmod(
            0o755
        )

        temp_binary.replace(
            SSHX_PATH
        )

        SSHX_PATH.chmod(
            0o755
        )

        shutil.rmtree(
            extract_dir,
            ignore_errors=True,
        )

        log(
            f"SSHX installed permanently: {SSHX_PATH}"
        )

        try:

            result = subprocess.run(
                [
                    str(SSHX_PATH),
                    "--version",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=15,
            )

            if result.stdout:

                print(
                    result.stdout,
                    flush=True,
                )

        except Exception as exc:

            log(
                f"SSHX version test warning: {exc}"
            )

        return SSHX_PATH

    except Exception as exc:

        log(
            f"SSHX installation failed: {exc}"
        )

        shutil.rmtree(
            extract_dir,
            ignore_errors=True,
        )

        return None


# ============================================================
# UBUNTU SHELL WRAPPER
# ============================================================

def create_ubuntu_shell_wrapper():

    WRAPPER_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    wrapper_content = f"""#!/bin/bash

exec "{PROOT_PATH}" \\
    -r "{ROOTFS_DIR}" \\
    -0 \\
    -w /root \\
    -b /dev \\
    -b /dev/pts \\
    -b /proc \\
    -b /sys \\
    /bin/bash -l
"""

    try:

        temp_wrapper = (
            UBUNTU_SHELL_WRAPPER.with_suffix(
                ".part"
            )
        )

        temp_wrapper.write_text(
            wrapper_content
        )

        temp_wrapper.chmod(
            0o755
        )

        temp_wrapper.replace(
            UBUNTU_SHELL_WRAPPER
        )

        return True

    except Exception as exc:

        log(
            f"Cannot create Ubuntu shell wrapper: {exc}"
        )

        return False


# ============================================================
# PROCESS CHECK
# ============================================================

def is_process_alive(
    pid,
):

    try:

        os.kill(
            int(pid),
            0,
        )

        return True

    except ProcessLookupError:

        return False

    except PermissionError:

        return True

    except Exception:

        return False


# ============================================================
# IS SSHX PROCESS?
# ============================================================

def is_sshx_process(
    pid,
):

    try:

        proc_cmdline = Path(
            f"/proc/{int(pid)}/cmdline"
        )

        if not proc_cmdline.exists():

            return False

        raw = proc_cmdline.read_bytes()

        command_line = (
            raw
            .replace(
                b"\x00",
                b" ",
            )
            .decode(
                errors="ignore"
            )
            .strip()
        )

        if not command_line:

            return False

        return (
            "sshx" in command_line.lower()
        )

    except Exception:

        return False


# ============================================================
# SAVED SSHX PID
# ============================================================

def get_saved_sshx_pid():

    try:

        if not SSHX_PID_FILE.exists():

            return None

        text = (
            SSHX_PID_FILE
            .read_text()
            .strip()
        )

        if not text:

            return None

        pid = int(
            text
        )

        if not is_process_alive(
            pid
        ):

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            return None

        if not is_sshx_process(
            pid
        ):

            log(
                f"Stored PID {pid} is not SSHX."
            )

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            SSHX_LINK_FILE.unlink(
                missing_ok=True
            )

            return None

        return pid

    except Exception:

        return None


# ============================================================
# SAVED SSHX LINK
# ============================================================

def get_saved_sshx_link():

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


# ============================================================
# EXTRACT SSHX LINK
# ============================================================

def extract_sshx_link(
    text,
):

    if not text:

        return None

    pattern = (
        r"https://sshx\.io/s/"
        r"[A-Za-z0-9_-]+"
        r"#"
        r"[A-Za-z0-9_-]+"
    )

    match = re.search(
        pattern,
        text,
    )

    if match:

        return match.group(0)

    return None


# ============================================================
# START SSHX
# ============================================================

def start_sshx():

    with bootstrap_lock:

        BASE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        STATE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:

            start_lock_file = open(
                SSHX_START_LOCK_FILE,
                "w",
            )

            fcntl.flock(
                start_lock_file.fileno(),
                fcntl.LOCK_EX,
            )

        except Exception as exc:

            log(
                f"Cannot acquire SSHX start lock: {exc}"
            )

            return None

        log_file = None

        try:

            # ------------------------------------------------
            # Existing SSHX
            # ------------------------------------------------

            existing_pid = (
                get_saved_sshx_pid()
            )

            existing_link = (
                get_saved_sshx_link()
            )

            if (
                existing_pid
                and
                existing_link
            ):

                log(
                    "SSHX already running."
                )

                log(
                    f"PID={existing_pid}"
                )

                log(
                    f"URL={existing_link}"
                )

                return existing_link

            # ------------------------------------------------
            # Clear stale state
            # ------------------------------------------------

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            SSHX_LINK_FILE.unlink(
                missing_ok=True
            )

            # ------------------------------------------------
            # SSHX
            # ------------------------------------------------

            sshx_path = install_sshx()

            if not sshx_path:

                return None

            # ------------------------------------------------
            # Wrapper
            # ------------------------------------------------

            if not create_ubuntu_shell_wrapper():

                return None

            # ------------------------------------------------
            # Clear current log
            # ------------------------------------------------

            try:

                SSHX_LOG_FILE.write_text(
                    ""
                )

            except Exception as exc:

                log(
                    f"Cannot clear SSHX log: {exc}"
                )

                return None

            log(
                "Starting SSHX..."
            )

            env = os.environ.copy()

            env["SHELL"] = str(
                UBUNTU_SHELL_WRAPPER
            )

            env["TERM"] = env.get(
                "TERM",
                "xterm-256color",
            )

            env["HOME"] = "/root"

            env["USER"] = "root"

            env["LOGNAME"] = "root"

            # ------------------------------------------------
            # SSHX log
            # ------------------------------------------------

            try:

                log_file = open(
                    SSHX_LOG_FILE,
                    "a",
                    buffering=1,
                )

                log_file.write(
                    "===== SSHX START =====\n"
                )

                log_file.flush()

            except Exception:

                log_file = subprocess.DEVNULL

            # ------------------------------------------------
            # Start SSHX
            # ------------------------------------------------

            try:

                process = subprocess.Popen(
                    [
                        str(sshx_path),
                        "--quiet",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )

            except Exception as exc:

                log(
                    f"SSHX process creation failed: {exc}"
                )

                return None

            # ------------------------------------------------
            # Save PID
            # ------------------------------------------------

            SSHX_PID_FILE.write_text(
                str(process.pid)
            )

            log(
                f"SSHX process started. PID={process.pid}"
            )

            # ------------------------------------------------
            # Wait for URL
            # ------------------------------------------------

            link = None

            deadline = (
                time.time() + 45
            )

            last_log_size = 0

            while time.time() < deadline:

                if process.poll() is not None:

                    log(
                        "SSHX process exited unexpectedly."
                    )

                    break

                try:

                    current_size = (
                        SSHX_LOG_FILE.stat().st_size
                    )

                    if current_size > last_log_size:

                        with open(
                            SSHX_LOG_FILE,
                            "r",
                            errors="ignore",
                        ) as f:

                            f.seek(
                                last_log_size
                            )

                            new_text = f.read()

                        last_log_size = (
                            current_size
                        )

                        if new_text:

                            print(
                                new_text,
                                end="",
                                flush=True,
                            )

                            detected = (
                                extract_sshx_link(
                                    new_text
                                )
                            )

                            if detected:

                                link = detected

                                break

                except Exception:

                    pass

                time.sleep(
                    0.5
                )

            # ------------------------------------------------
            # URL found
            # ------------------------------------------------

            if link:

                SSHX_LINK_FILE.write_text(
                    link
                )

                log(
                    f"SSHX URL: {link}"
                )

                return link

            # ------------------------------------------------
            # URL not found
            # ------------------------------------------------

            if process.poll() is None:

                try:

                    process.terminate()

                except Exception:

                    pass

            try:

                process.wait(
                    timeout=5
                )

            except Exception:

                try:

                    process.kill()

                except Exception:

                    pass

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            SSHX_LINK_FILE.unlink(
                missing_ok=True
            )

            log(
                "SSHX URL was not detected."
            )

            return None

        except Exception as exc:

            log(
                f"SSHX start failed: {exc}"
            )

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            SSHX_LINK_FILE.unlink(
                missing_ok=True
            )

            return None

        finally:

            try:

                if (
                    log_file is not None
                    and
                    log_file is not subprocess.DEVNULL
                ):

                    log_file.close()

            except Exception:

                pass

            try:

                fcntl.flock(
                    start_lock_file.fileno(),
                    fcntl.LOCK_UN,
                )

                start_lock_file.close()

            except Exception:

                pass


# ============================================================
# STOP SSHX
# ============================================================

def stop_sshx():

    with bootstrap_lock:

        start_lock_file = None

        try:

            start_lock_file = open(
                SSHX_START_LOCK_FILE,
                "w",
            )

            fcntl.flock(
                start_lock_file.fileno(),
                fcntl.LOCK_EX,
            )

        except Exception:

            pass

        try:

            pid = get_saved_sshx_pid()

            if pid:

                log(
                    f"Stopping SSHX PID={pid}"
                )

                try:

                    os.kill(
                        pid,
                        15,
                    )

                except Exception:

                    pass

                for _ in range(20):

                    if not is_process_alive(
                        pid
                    ):

                        break

                    time.sleep(
                        0.1
                    )

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            SSHX_LINK_FILE.unlink(
                missing_ok=True
            )

            log(
                "SSHX state cleared."
            )

        finally:

            if start_lock_file:

                try:

                    fcntl.flock(
                        start_lock_file.fileno(),
                        fcntl.LOCK_UN,
                    )

                    start_lock_file.close()

                except Exception:

                    pass


# ============================================================
# PERSISTENCE DIAGNOSTICS
# ============================================================

def get_persistence_status():

    result = {

        "storage_exists": BASE_DIR.exists(),

        "rootfs_exists": ROOTFS_DIR.exists(),

        "rootfs_valid": rootfs_is_valid(),

        "rootfs_state": ROOTFS_STATE_FILE.exists(),

        "persistence_test": check_persistence_test(),

        "proot_exists": PROOT_PATH.exists(),

        "sshx_exists": SSHX_PATH.exists(),

    }

    return result


# ============================================================
# FULL BOOTSTRAP
# ============================================================

def bootstrap():

    if not prepare_storage():

        log(
            "Persistent storage is unavailable."
        )

        return False

    log(
        "================================================"
    )

    log(
        "SHL persistent bootstrap"
    )

    log(
        f"BASE_DIR  = {BASE_DIR}"
    )

    log(
        f"ROOTFS    = {ROOTFS_DIR}"
    )

    log(
        f"PROOT     = {PROOT_PATH}"
    )

    log(
        f"SSHX      = {SSHX_PATH}"
    )

    log(
        "================================================"
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # If rootfs exists, DO NOT extract it again.
    # --------------------------------------------------------

    if rootfs_is_valid():

        log(
            "Existing persistent Ubuntu detected."
        )

        log(
            "No Ubuntu extraction will be performed."
        )

        ensure_persistence_test()

        # ----------------------------------------------------
        # PRoot
        # ----------------------------------------------------

        if not install_proot():

            return False

        # ----------------------------------------------------
        # Test Ubuntu
        # ----------------------------------------------------

        ok, output = test_ubuntu()

        if not ok:

            log(
                "Existing Ubuntu test failed."
            )

            print(
                output,
                flush=True,
            )

            return False

        # ----------------------------------------------------
        # Tools
        # ----------------------------------------------------

        if not install_ubuntu_tools():

            return False

        write_rootfs_state()

        STATE_FILE.write_text(
            "SHL persistent bootstrap OK\n"
            f"verified={int(time.time())}\n"
        )

        log(
            "Existing persistent Ubuntu is ready."
        )

        return True

    # --------------------------------------------------------
    # Bootstrap lock
    # --------------------------------------------------------

    if not acquire_bootstrap_lock():

        return False

    try:

        # ----------------------------------------------------
        # Another process may have completed installation
        # while we were waiting for lock.
        # ----------------------------------------------------

        if rootfs_is_valid():

            log(
                "Ubuntu was installed by another process."
            )

            ensure_persistence_test()

            return True

        # ----------------------------------------------------
        # Architecture
        # ----------------------------------------------------

        arch = detect_arch()

        if arch != "amd64":

            log(
                "This build currently supports amd64 only."
            )

            return False

        # ----------------------------------------------------
        # Ubuntu
        # ----------------------------------------------------

        if not install_ubuntu():

            log(
                "Ubuntu installation failed."
            )

            return False

        # ----------------------------------------------------
        # PRoot
        # ----------------------------------------------------

        if not install_proot():

            log(
                "PRoot installation failed."
            )

            return False

        # ----------------------------------------------------
        # Test
        # ----------------------------------------------------

        ok, output = test_ubuntu()

        if not ok:

            log(
                "Ubuntu/PRoot test failed."
            )

            print(
                output,
                flush=True,
            )

            return False

        # ----------------------------------------------------
        # Tools
        # ----------------------------------------------------

        if not install_ubuntu_tools():

            return False

        # ----------------------------------------------------
        # Persistence
        # ----------------------------------------------------

        ensure_persistence_test()

        write_rootfs_state()

        STATE_FILE.write_text(
            "SHL persistent bootstrap OK\n"
            f"created={int(time.time())}\n"
        )

        log(
            "SHL persistent bootstrap completed."
        )

        return True

    finally:

        release_bootstrap_lock()


# ============================================================
# UBUNTU STATUS
# ============================================================

def get_ubuntu_status():

    command = r"""
echo "=============================="
echo "SHL UBUNTU STATUS"
echo "=============================="

echo
echo "USER:"
id

echo
echo "OS:"
grep PRETTY_NAME /etc/os-release 2>/dev/null || true

echo
echo "KERNEL:"
uname -a

echo
echo "ROOT:"
df -h /

echo
echo "MEMORY:"
free -h

echo
echo "DNS:"
cat /etc/resolv.conf 2>/dev/null || true

echo
echo "PERSISTENCE:"
if [ -f /root/.shl-persistence-test ]; then
    echo "PERSISTENCE TEST: OK"
    cat /root/.shl-persistence-test
else
    echo "PERSISTENCE TEST: MISSING"
fi

echo
echo "BASE TOOLS:"
if [ -f /root/.shl_tools_ready ]; then
    echo "BASE TOOLS: INSTALLED"
else
    echo "BASE TOOLS: NOT INSTALLED"
fi

echo
echo "GROUP FILE:"
ls -l /etc/group 2>/dev/null || true

echo
echo "ROOT:"
ls -ld /root 2>/dev/null || true

echo
echo "=============================="
"""

    code, output = ubuntu_command(
        command,
        timeout=60,
    )

    return (
        code,
        output,
    )


# ============================================================
# STREAMLIT PAGE
# ============================================================

st.set_page_config(
    page_title="SHL Ubuntu Server",
    page_icon="🖥️",
    layout="wide",
)


st.title(
    "🖥️ SHL Ubuntu Server"
)

st.caption(
    "Streamlit → Persistent Ubuntu 22.04.5 → PRoot → SSHX"
)


# ============================================================
# BOOTSTRAP
# ============================================================

with st.spinner(
    "در حال آماده‌سازی Ubuntu دائمی و PRoot..."
):

    bootstrap_ok = bootstrap()


if not bootstrap_ok:

    st.error(
        "Bootstrap ناموفق بود."
    )

    st.code(
        f"""
BASE_DIR:
{BASE_DIR}

ROOTFS:
{ROOTFS_DIR}

PROOT:
{PROOT_PATH}

SSHX:
{SSHX_PATH}
""",
        language="text",
    )

    st.stop()


# ============================================================
# PERSISTENCE STATUS
# ============================================================

pstatus = get_persistence_status()

if pstatus["rootfs_valid"]:

    if pstatus["persistence_test"]:

        st.success(
            "Ubuntu rootfs روی storage پایدار قرار دارد "
            "و تست persistence موفق است."
        )

    else:

        st.warning(
            "Ubuntu موجود است ولی فایل تست persistence "
            "پیدا نشد."
        )

else:

    st.error(
        "Persistent Ubuntu rootfs معتبر نیست."
    )


# ============================================================
# SSHX
# ============================================================

sshx_link = start_sshx()


st.success(
    "Ubuntu 22.04.5 و PRoot آماده هستند."
)


if sshx_link:

    st.subheader(
        "🔗 SSHX"
    )

    st.code(
        sshx_link,
        language="text",
    )

    st.markdown(
        f"**SSHX:** {sshx_link}"
    )

    st.info(
        "با باز کردن لینک بالا باید مستقیماً وارد "
        "Ubuntu 22.04.5 دائمی شوید."
    )

else:

    st.error(
        "SSHX نتوانست لینک ایجاد کند."
    )


# ============================================================
# SSHX STATUS
# ============================================================

pid = get_saved_sshx_pid()

if pid:

    st.caption(
        f"SSHX PID: {pid}"
    )

else:

    st.caption(
        "SSHX process: not detected"
    )


# ============================================================
# PERSISTENT PATHS
# ============================================================

with st.expander(
    "💾 مسیرهای Persistent"
):

    st.code(
        f"""
Persistent base:
{BASE_DIR}

Ubuntu rootfs:
{ROOTFS_DIR}

PRoot:
{PROOT_PATH}

SSHX:
{SSHX_PATH}

Wrapper:
{UBUNTU_SHELL_WRAPPER}

State:
{STATE_DIR}

Ubuntu archive:
{UBUNTU_ARCHIVE}
""",
        language="text",
    )


# ============================================================
# BUTTONS
# ============================================================

col1, col2 = st.columns(
    2
)


with col1:

    if st.button(
        "🔄 تست Ubuntu",
        use_container_width=True,
    ):

        code, output = (
            get_ubuntu_status()
        )

        if code == 0:

            st.success(
                "Ubuntu سالم است."
            )

        else:

            st.error(
                "تست Ubuntu ناموفق بود."
            )

        st.code(
            output,
            language="text",
        )


with col2:

    if st.button(
        "♻️ ساخت SSHX جدید",
        use_container_width=True,
    ):

        stop_sshx()

        st.rerun()


# ============================================================
# PERSISTENCE TEST INFORMATION
# ============================================================

with st.expander(
    "🧪 تست دائمی بودن سیستم‌عامل"
):

    st.write(
        "فایل زیر داخل خود Ubuntu rootfs ذخیره شده است:"
    )

    st.code(
        "/root/.shl-persistence-test",
        language="text",
    )

    st.write(
        "اگر بعد از Restart/Re-run Streamlit این فایل "
        "باقی بماند، rootfs از همان storage قبلی "
        "دوباره استفاده شده است."
    )

    st.code(
        "cat /root/.shl-persistence-test",
        language="bash",
    )


# ============================================================
# FULL UBUNTU STATUS
# ============================================================

with st.expander(
    "📊 وضعیت کامل Ubuntu"
):

    code, output = (
        get_ubuntu_status()
    )

    if output:

        st.code(
            output,
            language="text",
        )

    if code == 0:

        st.success(
            "Ubuntu command OK"
        )

    else:

        st.error(
            "Ubuntu command failed"
        )


# ============================================================
# FINAL STORAGE STATUS
# ============================================================

with st.expander(
    "🔐 وضعیت Storage"
):

    for key, value in pstatus.items():

        st.write(
            f"**{key}:** {value}"
        )

    st.code(
        str(BASE_DIR),
        language="text",
    )
