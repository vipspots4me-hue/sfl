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
# SHL - Streamlit → Ubuntu 22.04.5 → PRoot → SSHX
# Persistent runtime version
# ============================================================


# ============================================================
# مسیرهای اصلی
#
# /mount/admin یک storage مربوط به Pod است.
#
# Ubuntu rootfs داخل این مسیر قرار دارد و بنابراین
# restart شدن Streamlit/SSHX باعث حذف Ubuntu نمی‌شود.
# ============================================================

PERSISTENT_BASE = Path("/mount/admin/shl-runtime")

BASE_DIR = PERSISTENT_BASE

ROOTFS_DIR = BASE_DIR / "ubuntu"
PROOT_DIR = BASE_DIR / "proot"
PROOT_PATH = PROOT_DIR / "proot"

LOCK_FILE = BASE_DIR / ".bootstrap.lock"
STATE_FILE = BASE_DIR / ".bootstrap.ok"


# ============================================================
# فایل‌های وضعیت SSHX
# ============================================================

SSHX_PID_FILE = BASE_DIR / "sshx.pid"
SSHX_LINK_FILE = BASE_DIR / "sshx.link"
SSHX_LOG_FILE = BASE_DIR / "sshx.log"

# قفل بین‌پردازشی SSHX
#
# threading.Lock فقط داخل همان Python process کار می‌کند.
# flock برای جلوگیری از اجرای همزمان SSHX در چند
# Streamlit execution استفاده می‌شود.
#
SSHX_START_LOCK_FILE = BASE_DIR / "sshx.start.lock"

UBUNTU_SHELL_WRAPPER = BASE_DIR / "ubuntu-shell"


# ============================================================
# فایل‌های دانلود
# ============================================================

UBUNTU_ARCHIVE = (
    BASE_DIR / "ubuntu-22.04.5-base-amd64.tar.gz"
)

SSHX_ARCHIVE = (
    BASE_DIR / "sshx.tar.gz"
)


# ============================================================
# URLها
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
# مسیر ثابت SSHX
# ============================================================

SSHX_DIR = Path("/home/appuser/.local/bin")
SSHX_PATH = SSHX_DIR / "sshx"


# ============================================================
# Lock داخلی Python
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
# اجرای command روی محیط میزبان
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
# دانلود فایل
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
                f"{label} download produced an empty file"
            )

        part_file.replace(
            destination
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
# تشخیص معماری
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
# آماده‌سازی Storage
# ============================================================

def prepare_storage():

    try:

        BASE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        log(
            f"Persistent SHL storage: {BASE_DIR}"
        )

        return True

    except Exception as exc:

        log(
            "Cannot create persistent storage: "
            f"{exc}"
        )

        return False


# ============================================================
# Bootstrap lock
# ============================================================

def acquire_lock(
    timeout=180,
):

    if not prepare_storage():

        return False

    start = time.time()

    while True:

        try:

            fd = os.open(
                LOCK_FILE,
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
                    "Bootstrap lock timeout."
                )

                try:

                    LOCK_FILE.unlink()

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


def release_lock():

    try:

        LOCK_FILE.unlink()

    except FileNotFoundError:

        pass

    except Exception:

        pass


# ============================================================
# استخراج امن tar
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

            if not str(target).startswith(
                str(destination) + os.sep
            ):

                raise RuntimeError(
                    "Unsafe path inside archive: "
                    f"{member.name}"
                )

        tar.extractall(
            destination
        )


# ============================================================
# نصب Ubuntu
# ============================================================

def install_ubuntu():

    bash_path = (
        ROOTFS_DIR
        / "bin"
        / "bash"
    )

    # --------------------------------------------------------
    # Ubuntu موجود است
    # --------------------------------------------------------

    if bash_path.exists():

        log(
            "Ubuntu rootfs already exists."
        )

        return True

    log(
        "Ubuntu rootfs is not installed."
    )

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    extracting_dir = (
        BASE_DIR
        / "ubuntu.extracting"
    )

    if extracting_dir.exists():

        log(
            "Removing incomplete Ubuntu extraction..."
        )

        shutil.rmtree(
            extracting_dir,
            ignore_errors=True,
        )

    # --------------------------------------------------------
    # دانلود Ubuntu فقط در صورت نیاز
    # --------------------------------------------------------

    if (
        not UBUNTU_ARCHIVE.exists()
        or UBUNTU_ARCHIVE.stat().st_size <= 0
    ):

        if not download_file(
            UBUNTU_URL,
            UBUNTU_ARCHIVE,
            "Ubuntu 22.04.5",
        ):

            return False

    if (
        not UBUNTU_ARCHIVE.exists()
        or UBUNTU_ARCHIVE.stat().st_size <= 0
    ):

        log(
            "Ubuntu archive is invalid."
        )

        return False

    try:

        log(
            "Extracting Ubuntu rootfs..."
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

                temp_root = (
                    BASE_DIR
                    / "ubuntu.normalized"
                )

                if temp_root.exists():

                    shutil.rmtree(
                        temp_root,
                        ignore_errors=True,
                    )

                shutil.move(
                    str(real_root),
                    str(temp_root),
                )

                shutil.rmtree(
                    extracting_dir,
                    ignore_errors=True,
                )

                temp_root.rename(
                    extracting_dir
                )

        if not (
            extracting_dir
            / "bin"
            / "bash"
        ).exists():

            raise RuntimeError(
                "Ubuntu rootfs extraction "
                "did not contain /bin/bash"
            )

        # ----------------------------------------------------
        # جایگزینی rootfs
        # ----------------------------------------------------

        if ROOTFS_DIR.exists():

            shutil.rmtree(
                ROOTFS_DIR,
                ignore_errors=True,
            )

        extracting_dir.rename(
            ROOTFS_DIR
        )

        # ====================================================
        # DNS
        #
        # resolv.conf با PRoot bind نمی‌شود.
        # ====================================================

        etc_dir = (
            ROOTFS_DIR
            / "etc"
        )

        etc_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        resolv = (
            etc_dir
            / "resolv.conf"
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

        try:

            resolv.chmod(
                0o644
            )

        except Exception:

            pass

        # ====================================================
        # hostname
        # ====================================================

        try:

            (
                etc_dir
                / "hostname"
            ).write_text(
                "shl-ubuntu\n"
            )

        except Exception:

            pass

        # ====================================================
        # hosts
        # ====================================================

        try:

            (
                etc_dir
                / "hosts"
            ).write_text(
                "127.0.0.1 localhost\n"
                "127.0.1.1 shl-ubuntu\n"
                "::1 localhost "
                "ip6-localhost "
                "ip6-loopback\n"
            )

        except Exception:

            pass

        log(
            "Ubuntu rootfs installed."
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
# نصب PRoot
# ============================================================

def install_proot():

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
                    "PRoot already installed."
                )

                return True

        except Exception:

            pass

    PROOT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_file = (
        PROOT_PATH.with_suffix(
            ".part"
        )
    )

    if temp_file.exists():

        try:

            temp_file.unlink()

        except Exception:

            pass

    log(
        "Downloading PRoot amd64: "
        f"{PROOT_URL}"
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
            "PRoot installed successfully."
        )

        return True

    except Exception as exc:

        log(
            f"PRoot installation failed: {exc}"
        )

        return False


# ============================================================
# ساخت command مربوط به Ubuntu
#
# resolv.conf دیگر bind نمی‌شود.
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
# اجرای command داخل Ubuntu
# ============================================================

def ubuntu_command(
    command,
    timeout=300,
    env=None,
):

    cmd = get_proot_command(
        command
    )

    log(
        "Ubuntu command: "
        + " ".join(cmd[:-1])
        + " /bin/bash -lc ..."
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
# تست Ubuntu
# ============================================================

def test_ubuntu():

    command = """
echo SHL_UBUNTU_OK
echo "USER=$(id -un)"
echo "UID=$(id -u)"
echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
echo "ARCH=$(uname -m)"
echo "ROOTFS_DNS:"
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
# نصب ابزارهای پایه Ubuntu
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
        "Installing basic Ubuntu tools..."
    )

    command = """
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export LANG=C

apt-get update

apt-get install -y \
    bash \
    ca-certificates \
    curl \
    wget \
    unzip \
    tar \
    gzip \
    procps \
    iproute2 \
    iputils-ping \
    net-tools \
    python3 \
    python3-pip \
    git \
    openssl \
    netcat-openbsd

mkdir -p /root

touch /root/.shl_tools_ready
"""

    code, output = ubuntu_command(
        command,
        timeout=900,
    )

    if code != 0:

        log(
            "Ubuntu tools installation failed."
        )

        return False

    log(
        "Ubuntu tools installed."
    )

    return True


# ============================================================
# پیدا کردن SSHX
# ============================================================

def find_sshx():

    candidates = [
        SSHX_PATH,
        Path("/usr/local/bin/sshx"),
        Path("/usr/bin/sshx"),
    ]

    for candidate in candidates:

        try:

            if (
                candidate.is_file()
                and os.access(
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

            try:

                if (
                    found_path.is_file()
                    and os.access(
                        found_path,
                        os.X_OK,
                    )
                ):

                    return found_path

            except (
                PermissionError,
                OSError,
            ):

                pass

    except (
        PermissionError,
        OSError,
    ):

        pass

    return None


# ============================================================
# نصب SSHX
# ============================================================

def install_sshx():

    existing = find_sshx()

    if existing:

        log(
            f"SSHX already installed: {existing}"
        )

        return existing

    try:

        SSHX_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

    except Exception as exc:

        log(
            "Cannot create SSHX directory: "
            f"{exc}"
        )

        return None

    log(
        "Downloading SSHX..."
    )

    if not download_file(
        SSHX_URL,
        SSHX_ARCHIVE,
        "SSHX",
    ):

        return None

    extract_dir = (
        BASE_DIR
        / "sshx.extract"
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

            log(
                "SSHX binary not found "
                "inside archive."
            )

            return None

        shutil.copy2(
            sshx_binary,
            SSHX_PATH,
        )

        SSHX_PATH.chmod(
            0o755
        )

        log(
            f"SSHX installed: {SSHX_PATH}"
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

        return None


# ============================================================
# ساخت wrapper برای ورود SSHX به Ubuntu
#
# resolv.conf دیگر bind نمی‌شود.
# ============================================================

def create_ubuntu_shell_wrapper():

    BASE_DIR.mkdir(
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

        UBUNTU_SHELL_WRAPPER.write_text(
            wrapper_content
        )

        UBUNTU_SHELL_WRAPPER.chmod(
            0o755
        )

        return True

    except Exception as exc:

        log(
            "Cannot create Ubuntu shell "
            f"wrapper: {exc}"
        )

        return False


# ============================================================
# بررسی زنده بودن process
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
# بررسی اینکه PID واقعاً SSHX است
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
# PID ذخیره‌شده SSHX
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

        # ----------------------------------------------------
        # PID زنده است ولی SSHX نیست
        # ----------------------------------------------------

        if not is_sshx_process(
            pid
        ):

            log(
                f"Stored PID {pid} is not SSHX. "
                "Clearing stale state."
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
# لینک ذخیره‌شده SSHX
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
# استخراج لینک SSHX از log
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
# اجرای SSHX
#
# اصلاحات مهم:
#
# 1. flock برای جلوگیری از چند SSHX همزمان
# 2. بررسی PID + لینک قبل از Start
# 3. پاک کردن log قدیمی قبل از Start
# 4. خواندن فقط log مربوط به همین اجرای جدید
# 5. جلوگیری از استفاده از URL قدیمی
# ============================================================

def start_sshx():

    with bootstrap_lock:

        BASE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        # ====================================================
        # قفل بین‌پردازشی
        # ====================================================

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

        try:

            # =================================================
            # ابتدا SSHX موجود را بررسی کن
            # =================================================

            existing_pid = (
                get_saved_sshx_pid()
            )

            existing_link = (
                get_saved_sshx_link()
            )

            if (
                existing_pid
                and existing_link
            ):

                log(
                    "SSHX already running. "
                    f"PID={existing_pid}"
                )

                log(
                    f"SSHX URL: {existing_link}"
                )

                return existing_link

            # =================================================
            # اگر state قدیمی خراب است پاک شود
            # =================================================

            SSHX_PID_FILE.unlink(
                missing_ok=True
            )

            SSHX_LINK_FILE.unlink(
                missing_ok=True
            )

            # =================================================
            # نصب SSHX
            # =================================================

            sshx_path = install_sshx()

            if not sshx_path:

                return None

            # =================================================
            # ساخت wrapper
            # =================================================

            if not create_ubuntu_shell_wrapper():

                return None

            # =================================================
            # پاک کردن log قبلی
            #
            # جلوگیری از استفاده از URL قدیمی
            # =================================================

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

            env["HOME"] = "/home/appuser"

            # =================================================
            # باز کردن log
            # =================================================

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

                log_file = (
                    subprocess.DEVNULL
                )

            # =================================================
            # اجرای SSHX
            # =================================================

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

                try:

                    if log_file not in (
                        subprocess.DEVNULL,
                        None,
                    ):

                        log_file.close()

                except Exception:

                    pass

                return None

            # =================================================
            # ذخیره PID
            # =================================================

            SSHX_PID_FILE.write_text(
                str(process.pid)
            )

            log(
                f"SSHX process started. PID={process.pid}"
            )

            # =================================================
            # انتظار برای لینک
            # =================================================

            link = None

            deadline = (
                time.time() + 45
            )

            last_log_size = 0

            while time.time() < deadline:

                # ---------------------------------------------
                # process مرده؟
                # ---------------------------------------------

                if process.poll() is not None:

                    log(
                        "SSHX process exited unexpectedly."
                    )

                    break

                # ---------------------------------------------
                # فقط بخش جدید log
                # ---------------------------------------------

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

            # =================================================
            # لینک پیدا شد
            # =================================================

            if link:

                SSHX_LINK_FILE.write_text(
                    link
                )

                log(
                    f"SSHX URL: {link}"
                )

                try:

                    if log_file not in (
                        subprocess.DEVNULL,
                        None,
                    ):

                        log_file.close()

                except Exception:

                    pass

                return link

            # =================================================
            # لینک پیدا نشد
            # =================================================

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

            try:

                if log_file not in (
                    subprocess.DEVNULL,
                    None,
                ):

                    log_file.close()

            except Exception:

                pass

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

            # =================================================
            # آزاد کردن flock
            # =================================================

            try:

                fcntl.flock(
                    start_lock_file.fileno(),
                    fcntl.LOCK_UN,
                )

                start_lock_file.close()

            except Exception:

                pass


# ============================================================
# توقف SSHX
# ============================================================

def stop_sshx():

    with bootstrap_lock:

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

            start_lock_file = None

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
# Bootstrap اصلی
# ============================================================

def bootstrap():

    # --------------------------------------------------------
    # Storage
    # --------------------------------------------------------

    if not prepare_storage():

        log(
            "Persistent storage is unavailable."
        )

        return False

    log(
        f"SHL persistent directory: {BASE_DIR}"
    )

    log(
        f"Ubuntu rootfs: {ROOTFS_DIR}"
    )

    log(
        f"PRoot: {PROOT_PATH}"
    )

    # --------------------------------------------------------
    # اگر قبلاً نصب شده، فقط تست کن
    # --------------------------------------------------------

    if (
        STATE_FILE.exists()
        and ROOTFS_DIR.exists()
        and PROOT_PATH.exists()
    ):

        log(
            "Existing SHL bootstrap detected."
        )

        ok, output = test_ubuntu()

        if ok:

            if not install_ubuntu_tools():

                return False

            log(
                "Existing persistent Ubuntu is ready."
            )

            return True

        log(
            "Existing Ubuntu test failed."
        )

    # --------------------------------------------------------
    # Lock
    # --------------------------------------------------------

    if not acquire_lock():

        return False

    try:

        # ----------------------------------------------------
        # معماری
        # ----------------------------------------------------

        arch = detect_arch()

        if arch != "amd64":

            log(
                "This build currently supports "
                "amd64 only."
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
        # تست
        # ----------------------------------------------------

        ok, output = test_ubuntu()

        if not ok:

            log(
                "Ubuntu/PRoot test failed."
            )

            return False

        # ----------------------------------------------------
        # ابزارها
        # ----------------------------------------------------

        if not install_ubuntu_tools():

            return False

        # ----------------------------------------------------
        # Marker
        # ----------------------------------------------------

        STATE_FILE.write_text(
            "SHL persistent bootstrap OK\n"
        )

        log(
            "SHL bootstrap completed."
        )

        return True

    finally:

        release_lock()


# ============================================================
# وضعیت Ubuntu
# ============================================================

def get_ubuntu_status():

    command = """
echo "USER=$(id -un)"
echo "UID=$(id -u)"
echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
echo "ARCH=$(uname -m)"

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
echo "PERSISTENT ROOT:"
ls -ld /root 2>/dev/null || true
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
# Streamlit UI
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
    "Streamlit → Ubuntu 22.04.5 → PRoot → SSHX"
)


# ============================================================
# Bootstrap
# ============================================================

with st.spinner(
    "در حال آماده‌سازی Ubuntu و PRoot..."
):

    bootstrap_ok = bootstrap()


if not bootstrap_ok:

    st.error(
        "Bootstrap ناموفق بود. "
        "لاگ‌های Streamlit را بررسی کن."
    )

    st.stop()


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
        "با باز کردن لینک بالا باید "
        "مستقیماً وارد Ubuntu 22.04.5 شوید."
    )

else:

    st.error(
        "SSHX نتوانست لینک ایجاد کند."
    )


# ============================================================
# وضعیت SSHX
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
# مسیر Persistence
# ============================================================

st.caption(
    f"Persistent storage: {BASE_DIR}"
)


# ============================================================
# دکمه‌ها
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

            st.code(
                output,
                language="text",
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
# وضعیت کامل Ubuntu
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
