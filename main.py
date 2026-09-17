import os
import re
import time
import tarfile
import shutil
import platform
import subprocess
import threading
import urllib.request
from pathlib import Path

import streamlit as st


# ============================================================
# SHL - Persistent Ubuntu Server
# Streamlit -> Debian -> PRoot -> Ubuntu 22.04.5 -> SSHX
#
# نکته:
# Community Cloud filesystem دائمی نیست.
# بنابراین این برنامه Ubuntu را در یک مسیر state نگه می‌دارد
# و در صورت وجود backup آن را restore می‌کند.
#
# برای persistence واقعی بعد از rebuild کامل Streamlit،
# باید STORAGE_DIR را به storage خارجی متصل کنیم.
# ============================================================


# ============================================================
# تنظیمات اصلی
# ============================================================

BASE_DIR = Path("/tmp/shl-runtime")

# این مسیر محل Ubuntu فعال است.
ROOTFS_DIR = BASE_DIR / "ubuntu"

# این مسیر برای state قابل انتقال استفاده می‌شود.
STATE_DIR = BASE_DIR / "persistent-state"

# backup فشرده Ubuntu
ROOTFS_ARCHIVE = STATE_DIR / "ubuntu-rootfs.tar.gz"

PROOT_DIR = BASE_DIR / "proot"
PROOT_PATH = PROOT_DIR / "proot"

LOCK_FILE = BASE_DIR / ".bootstrap.lock"

SSHX_PID_FILE = BASE_DIR / "sshx.pid"
SSHX_LINK_FILE = BASE_DIR / "sshx.link"
SSHX_LOG_FILE = BASE_DIR / "sshx.log"

UBUNTU_SHELL_WRAPPER = BASE_DIR / "ubuntu-shell"


# ============================================================
# آدرس‌ها
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

SSHX_DIR = Path("/home/appuser/.local/bin")
SSHX_PATH = SSHX_DIR / "sshx"


# ============================================================
# پکیج‌های پایه
#
# اینها فقط در صورتی نصب می‌شوند که وجود نداشته باشند.
# بنابراین هر Streamlit rerun باعث apt install مجدد نمی‌شود.
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


bootstrap_lock = threading.Lock()


# ============================================================
# لاگ
# ============================================================

def log(message):
    print(f"[SHL] {message}", flush=True)


# ============================================================
# اجرای command روی Debian اصلی
# ============================================================

def run_command(command, timeout=None, env=None):
    if isinstance(command, str):
        shell_command = command
    else:
        shell_command = " ".join(str(x) for x in command)

    log(f"$ {shell_command}")

    try:
        result = subprocess.run(
            command,
            shell=isinstance(command, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=env,
        )

        if result.stdout:
            print(result.stdout, flush=True)

        return result.returncode, result.stdout or ""

    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        log(f"Command timeout after {timeout} seconds.")
        return 124, output

    except Exception as exc:
        log(f"Command failed: {exc}")
        return 1, str(exc)


# ============================================================
# دانلود فایل
# ============================================================

def download_file(url, destination, label):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    part_file = destination.with_suffix(
        destination.suffix + ".part"
    )

    try:
        if part_file.exists():
            part_file.unlink()
    except Exception:
        pass

    try:
        log(f"Downloading {label}: {url}")

        def progress_hook(block_num, block_size, total_size):
            if total_size and total_size > 0:
                downloaded = block_num * block_size
                percent = min(
                    downloaded * 100.0 / total_size,
                    100.0,
                )

                bucket = int(percent / 5)

                if not hasattr(progress_hook, "last_bucket"):
                    progress_hook.last_bucket = -1

                if (
                    bucket != progress_hook.last_bucket
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

        part_file.replace(destination)

        return True

    except Exception as exc:
        log(f"{label} download failed: {exc}")

        try:
            if part_file.exists():
                part_file.unlink()
        except Exception:
            pass

        return False


# ============================================================
# معماری
# ============================================================

def detect_arch():
    machine = platform.machine().lower()

    log(f"Detected architecture: {machine}")

    if machine in ("x86_64", "amd64"):
        return "amd64"

    if machine in ("aarch64", "arm64"):
        return "arm64"

    return machine


# ============================================================
# Lock
# ============================================================

def acquire_lock(timeout=180):
    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    start = time.time()

    while True:
        try:
            fd = os.open(
                LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )

            os.write(
                fd,
                str(os.getpid()).encode(),
            )

            os.close(fd)

            return True

        except FileExistsError:

            if time.time() - start > timeout:

                log("Bootstrap lock timeout.")

                try:
                    LOCK_FILE.unlink()
                except Exception:
                    pass

                continue

            time.sleep(1)

        except Exception as exc:

            log(
                f"Cannot acquire bootstrap lock: {exc}"
            )

            return False


def release_lock():
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


# ============================================================
# امن extract
# ============================================================

def safe_extract(tar_path, destination):
    destination = Path(destination).resolve()

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
                destination / member.name
            ).resolve()

            if not str(target).startswith(
                str(destination) + os.sep
            ):
                raise RuntimeError(
                    f"Unsafe archive path: "
                    f"{member.name}"
                )

        tar.extractall(destination)


# ============================================================
# ساخت Ubuntu اولیه
# ============================================================

def install_ubuntu():
    bash_path = (
        ROOTFS_DIR /
        "bin" /
        "bash"
    )

    if bash_path.exists():

        log(
            "Ubuntu rootfs already exists."
        )

        return True

    log(
        "Ubuntu rootfs does not exist."
    )

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    archive = (
        BASE_DIR /
        "ubuntu-22.04.5-base-amd64.tar.gz"
    )

    extracting_dir = (
        BASE_DIR /
        "ubuntu.extracting"
    )

    if extracting_dir.exists():

        shutil.rmtree(
            extracting_dir,
            ignore_errors=True,
        )

    if (
        not archive.exists()
        or archive.stat().st_size <= 0
    ):

        if not download_file(
            UBUNTU_URL,
            archive,
            "Ubuntu 22.04.5",
        ):
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
            archive,
            extracting_dir,
        )

        if not (
            extracting_dir /
            "bin" /
            "bash"
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
                    BASE_DIR /
                    "ubuntu.normalized"
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
            extracting_dir /
            "bin" /
            "bash"
        ).exists():

            raise RuntimeError(
                "Ubuntu rootfs does not "
                "contain /bin/bash"
            )

        if ROOTFS_DIR.exists():

            shutil.rmtree(
                ROOTFS_DIR,
                ignore_errors=True,
            )

        extracting_dir.rename(
            ROOTFS_DIR
        )

        configure_ubuntu_files()

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
# تنظیم فایل‌های Ubuntu
# ============================================================

def configure_ubuntu_files():

    etc_dir = ROOTFS_DIR / "etc"

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
        etc_dir /
        "hostname"
    ).write_text(
        "shl-ubuntu\n"
    )

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
        temp_file.unlink()

    if not download_file(
        PROOT_URL,
        temp_file,
        "PRoot",
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
# دستور داخل Ubuntu
# ============================================================

def get_proot_command(command):

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
        merged_env.update(env)

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

        return (
            124,
            exc.stdout or "",
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
"""

    code, output = ubuntu_command(
        command,
        timeout=60,
    )

    if code != 0:
        return False, output

    return (
        "SHL_UBUNTU_OK" in output,
        output,
    )


# ============================================================
# بررسی و نصب packageهای لازم
# ============================================================

def ensure_ubuntu_packages():

    packages = " ".join(
        REQUIRED_PACKAGES
    )

    command = f"""
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export LANG=C

MISSING=""

for PKG in {packages}; do

    if ! dpkg-query -W \
        -f='${{Status}}' \
        "$PKG" 2>/dev/null \
        | grep -q "install ok installed"
    then
        MISSING="$MISSING $PKG"
    fi

done

if [ -n "$MISSING" ]; then

    echo "================================"
    echo "Installing missing packages:"
    echo "$MISSING"
    echo "================================"

    apt-get update

    apt-get install -y $MISSING

else

    echo "All required packages already installed."

fi
"""

    code, output = ubuntu_command(
        command,
        timeout=900,
    )

    if code != 0:

        log(
            "Ubuntu package installation failed."
        )

        return False

    return True


# ============================================================
# ساخت shell مخصوص SSHX
# ============================================================

def create_ubuntu_shell_wrapper():

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

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

    try:

        UBUNTU_SHELL_WRAPPER.write_text(
            content
        )

        UBUNTU_SHELL_WRAPPER.chmod(
            0o755
        )

        return True

    except Exception as exc:

        log(
            f"Cannot create shell wrapper: {exc}"
        )

        return False


# ============================================================
# SSHX
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

        log(
            f"SSHX already installed: {existing}"
        )

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

    extract_dir = (
        BASE_DIR /
        "sshx.extract"
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
            archive,
            "r:gz",
        ) as tar:

            tar.extractall(
                extract_dir
            )

        binary = None

        for path in extract_dir.rglob(
            "sshx"
        ):

            if path.is_file():

                binary = path
                break

        if binary is None:

            log(
                "SSHX binary not found."
            )

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
            f"SSHX installation failed: {exc}"
        )

        return None


# ============================================================
# SSHX PID
# ============================================================

def is_process_alive(pid):

    try:

        os.kill(
            int(pid),
            0,
        )

        return True

    except Exception:

        return False


def get_saved_sshx_pid():

    try:

        if not SSHX_PID_FILE.exists():
            return None

        pid = int(
            SSHX_PID_FILE
            .read_text()
            .strip()
        )

        if is_process_alive(pid):
            return pid

        SSHX_PID_FILE.unlink(
            missing_ok=True
        )

    except Exception:
        pass

    return None


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


# ============================================================
# Start SSHX
# ============================================================

def start_sshx():

    existing_pid = (
        get_saved_sshx_pid()
    )

    existing_link = (
        get_saved_sshx_link()
    )

    if existing_pid and existing_link:

        log(
            f"SSHX already running: "
            f"PID={existing_pid}"
        )

        return existing_link

    sshx_path = install_sshx()

    if not sshx_path:
        return None

    if not create_ubuntu_shell_wrapper():
        return None

    env = os.environ.copy()

    env["SHELL"] = str(
        UBUNTU_SHELL_WRAPPER
    )

    env["TERM"] = (
        "xterm-256color"
    )

    env["HOME"] = (
        "/home/appuser"
    )

    try:

        log_file = open(
            SSHX_LOG_FILE,
            "a",
            buffering=1,
        )

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

        SSHX_PID_FILE.write_text(
            str(process.pid)
        )

        deadline = (
            time.time() + 45
        )

        position = 0

        while time.time() < deadline:

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

                    new_text = (
                        text[position:]
                    )

                    position = len(text)

                    link = (
                        extract_sshx_link(
                            new_text
                        )
                    )

                    if link:

                        SSHX_LINK_FILE.write_text(
                            link
                        )

                        log(
                            f"SSHX URL: {link}"
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
# Persistent state
#
# این قسمت rootfs را به archive تبدیل می‌کند.
#
# توجه:
# اگر STATE_DIR روی filesystem موقت Streamlit باشد،
# archive هم موقت خواهد بود.
#
# برای persistence واقعی باید STATE_DIR را به storage خارجی
# منتقل کنیم.
# ============================================================

def create_rootfs_backup():

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_archive = (
        STATE_DIR /
        "ubuntu-rootfs.tmp.tar.gz"
    )

    try:

        if temp_archive.exists():
            temp_archive.unlink()

        log(
            "Creating Ubuntu persistent backup..."
        )

        with tarfile.open(
            temp_archive,
            "w:gz",
        ) as tar:

            tar.add(
                ROOTFS_DIR,
                arcname="ubuntu",
                recursive=True,
            )

        temp_archive.replace(
            ROOTFS_ARCHIVE
        )

        log(
            "Ubuntu backup created."
        )

        return True

    except Exception as exc:

        log(
            f"Backup failed: {exc}"
        )

        try:
            temp_archive.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        return False


def restore_rootfs_backup():

    if not ROOTFS_ARCHIVE.exists():
        return False

    log(
        "Persistent Ubuntu backup found."
    )

    restoring = (
        BASE_DIR /
        "ubuntu.restoring"
    )

    try:

        if restoring.exists():

            shutil.rmtree(
                restoring,
                ignore_errors=True,
            )

        restoring.mkdir(
            parents=True,
            exist_ok=True,
        )

        with tarfile.open(
            ROOTFS_ARCHIVE,
            "r:gz",
        ) as tar:

            safe_extract(
                ROOTFS_ARCHIVE,
                BASE_DIR,
            )

        restored_root = (
            BASE_DIR /
            "ubuntu"
        )

        if not (
            restored_root /
            "bin" /
            "bash"
        ).exists():

            raise RuntimeError(
                "Backup does not contain "
                "valid Ubuntu rootfs."
            )

        configure_ubuntu_files()

        log(
            "Ubuntu restored."
        )

        return True

    except Exception as exc:

        log(
            f"Restore failed: {exc}"
        )

        return False


# ============================================================
# Bootstrap
# ============================================================

def bootstrap():

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    arch = detect_arch()

    if arch != "amd64":

        log(
            "Only amd64 is currently supported."
        )

        return False

    # --------------------------------------------------------
    # اگر Ubuntu قبلی وجود نداشته باشد،
    # ابتدا backup را امتحان می‌کنیم.
    # --------------------------------------------------------

    if not (
        ROOTFS_DIR /
        "bin" /
        "bash"
    ).exists():

        restored = (
            restore_rootfs_backup()
        )

        if not restored:

            if not install_ubuntu():
                return False

    # --------------------------------------------------------
    # PRoot
    # --------------------------------------------------------

    if not install_proot():
        return False

    # --------------------------------------------------------
    # تست Ubuntu
    # --------------------------------------------------------

    ok, output = test_ubuntu()

    if not ok:

        log(
            "Ubuntu test failed."
        )

        return False

    # --------------------------------------------------------
    # packageها
    # --------------------------------------------------------

    if not ensure_ubuntu_packages():
        return False

    # --------------------------------------------------------
    # backup اولیه
    # --------------------------------------------------------

    if not ROOTFS_ARCHIVE.exists():

        create_rootfs_backup()

    log(
        "SHL Ubuntu environment is ready."
    )

    return True


# ============================================================
# Streamlit UI
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
    "Streamlit → PRoot → Ubuntu 22.04.5 → SSHX"
)


# ============================================================
# Bootstrap
# ============================================================

with st.spinner(
    "در حال آماده‌سازی Ubuntu..."
):

    if not bootstrap():

        st.error(
            "Ubuntu bootstrap failed."
        )

        st.stop()


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
        "SSHX آماده است."
    )

else:

    st.error(
        "SSHX link could not be created."
    )


# ============================================================
# وضعیت
# ============================================================

col1, col2, col3 = st.columns(3)


with col1:

    if st.button(
        "🔄 تست Ubuntu",
        use_container_width=True,
    ):

        code, output = test_ubuntu()

        st.code(
            output,
            language="text",
        )

        if code == 0:
            st.success(
                "Ubuntu OK"
            )
        else:
            st.error(
                "Ubuntu FAILED"
            )


with col2:

    if st.button(
        "📦 بررسی Packageها",
        use_container_width=True,
    ):

        if ensure_ubuntu_packages():

            st.success(
                "تمام packageهای مورد نیاز موجود هستند."
            )

        else:

            st.error(
                "Package check failed."
            )


with col3:

    if st.button(
        "💾 ذخیره Ubuntu",
        use_container_width=True,
    ):

        if create_rootfs_backup():

            st.success(
                "Ubuntu backup ساخته شد."
            )

        else:

            st.error(
                "Backup failed."
            )


# ============================================================
# اطلاعات کامل
# ============================================================

with st.expander(
    "📊 وضعیت کامل Ubuntu"
):

    code, output = ubuntu_command(
        """
echo "===== USER ====="
id

echo
echo "===== OS ====="
cat /etc/os-release

echo
echo "===== KERNEL ====="
uname -a

echo
echo "===== DISK ====="
df -h /

echo
echo "===== MEMORY ====="
free -h

echo
echo "===== PACKAGE TEST ====="
command -v neofetch || true
command -v python3 || true
command -v git || true
command -v curl || true
""",
        timeout=60,
    )

    st.code(
        output,
        language="text",
    )
