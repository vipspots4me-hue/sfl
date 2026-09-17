cd /mount/src/sfl

cat > main.py <<'PY'
import os
import re
import sys
import time
import tarfile
import shutil
import signal
import hashlib
import platform
import subprocess
import threading
import urllib.request
from pathlib import Path

import streamlit as st


BASE_DIR = Path("/tmp/shl-runtime")
ROOTFS_DIR = BASE_DIR / "ubuntu"
PROOT_DIR = BASE_DIR / "proot"
PROOT_PATH = PROOT_DIR / "proot"
LOCK_FILE = BASE_DIR / ".bootstrap.lock"
STATE_FILE = BASE_DIR / ".bootstrap.ok"

UBUNTU_VERSION = "22.04.5"
UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)
PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/download/proot-amd64"
)

SSHX_DIR = Path.home() / ".local" / "bin"
SSHX_PATH = SSHX_DIR / "sshx"
SSHX_URL = "https://s3.amazonaws.com/sshx/sshx-x86_64-unknown-linux-musl.tar.gz"

_lock = threading.Lock()
_sshx_process = None
_sshx_link = None


def log(msg):
    print(f"[SHL] {msg}", flush=True)


def run(cmd, cwd=None, timeout=None, env=None):
    log("$ " + " ".join(map(str, cmd)))
    return subprocess.run(
        [str(x) for x in cmd],
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def download(url, destination, label):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")

    if temp.exists():
        temp.unlink()

    log(f"Downloading {label}: {url}")

    def progress(block, block_size, total):
        if total > 0:
            percent = min(100, block * block_size * 100 / total)
            print(f"[SHL] {label}: {percent:.1f}%", flush=True)

    urllib.request.urlretrieve(url, temp, progress)

    if not temp.exists() or temp.stat().st_size == 0:
        raise RuntimeError(f"{label}: empty download")

    temp.replace(destination)
    return destination


def detect_arch():
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture: {machine}")

    log(f"Detected architecture: {machine}")
    log(f"Architecture selected: {arch}")
    return arch


def acquire_lock():
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    for _ in range(120):
        try:
            fd = os.open(
                LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(0.5)

    return False


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def safe_extract(tar_path, destination):
    destination = Path(destination).resolve()

    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not str(target).startswith(str(destination) + os.sep):
                raise RuntimeError("Unsafe archive path detected")
        tar.extractall(destination)


def install_rootfs():
    BASE_DIR.mkdir(parents=True, exist_ok=True)

    if ROOTFS_DIR.exists() and (ROOTFS_DIR / "bin/bash").exists():
        log("Ubuntu rootfs already exists.")
        return True

    log("Ubuntu rootfs is not installed.")

    archive = BASE_DIR / f"ubuntu-{UBUNTU_VERSION}.tar.gz"

    if archive.exists() and archive.stat().st_size == 0:
        archive.unlink()

    try:
        download(
            UBUNTU_URL,
            archive,
            f"Ubuntu {UBUNTU_VERSION}",
        )

        temp_root = BASE_DIR / "ubuntu.extracting"

        if temp_root.exists():
            shutil.rmtree(temp_root)

        temp_root.mkdir(parents=True)

        log("Extracting Ubuntu rootfs...")
        safe_extract(archive, temp_root)

        if not (temp_root / "bin/bash").exists():
            raise RuntimeError("Extracted Ubuntu rootfs is incomplete")

        if ROOTFS_DIR.exists():
            shutil.rmtree(ROOTFS_DIR)

        temp_root.rename(ROOTFS_DIR)

        resolv = ROOTFS_DIR / "etc/resolv.conf"
        resolv.parent.mkdir(parents=True, exist_ok=True)

        try:
            if resolv.is_symlink() or resolv.exists():
                resolv.unlink()
        except Exception:
            pass

        resolv.write_text(
            "nameserver 1.1.1.1\n"
            "nameserver 1.0.0.1\n"
        )

        (ROOTFS_DIR / "etc/hostname").write_text("shl-ubuntu\n")

        hosts = ROOTFS_DIR / "etc/hosts"
        hosts.write_text(
            "127.0.0.1 localhost\n"
            "127.0.1.1 shl-ubuntu\n"
            "::1 localhost ip6-localhost ip6-loopback\n"
        )

        log("Ubuntu rootfs installed successfully.")
        return True

    except Exception as e:
        log(f"Ubuntu rootfs installation failed: {e}")

        temp_root = BASE_DIR / "ubuntu.extracting"
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)

        if archive.exists() and archive.stat().st_size == 0:
            archive.unlink(missing_ok=True)

        return False


def install_proot():
    if PROOT_PATH.exists() and os.access(PROOT_PATH, os.X_OK):
        log("PRoot already installed.")
        return True

    PROOT_DIR.mkdir(parents=True, exist_ok=True)

    temp = PROOT_DIR / "proot.part"

    if temp.exists():
        temp.unlink()

    try:
        download(
            PROOT_URL,
            temp,
            "PRoot amd64",
        )

        if temp.stat().st_size < 100000:
            raise RuntimeError("PRoot binary is unexpectedly small")

        temp.chmod(0o755)
        temp.replace(PROOT_PATH)

        result = run(
            [PROOT_PATH, "--help"],
            timeout=20,
        )

        if result.returncode not in (0, 1):
            raise RuntimeError(
                "PRoot executable test failed:\n" + result.stdout
            )

        log("PRoot installed successfully.")
        return True

    except Exception as e:
        log(f"PRoot installation failed: {e}")
        temp.unlink(missing_ok=True)
        return False


def ubuntu_command(command, timeout=None):
    if not ROOTFS_DIR.exists():
        raise RuntimeError("Ubuntu rootfs missing")

    cmd = [
        PROOT_PATH,
        "-r", ROOTFS_DIR,
        "-0",
        "-w", "/root",
        "-b", "/dev",
        "-b", "/dev/pts",
        "-b", "/proc",
        "-b", "/sys",
        "-b", f"{ROOTFS_DIR}/etc/resolv.conf:/etc/resolv.conf",
        "/bin/bash",
        "-lc",
        command,
    ]

    result = run(cmd, timeout=timeout)
    return result


def test_ubuntu():
    result = ubuntu_command(
        """
echo SHL_UBUNTU_OK
echo "USER=$(id -un)"
echo "UID=$(id -u)"
echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
echo "ARCH=$(uname -m)"
""",
        timeout=60,
    )

    print(result.stdout, end="", flush=True)

    if result.returncode != 0 or "SHL_UBUNTU_OK" not in result.stdout:
        raise RuntimeError("Ubuntu/PRoot test failed")

    return True


def install_tools():
    marker = ROOTFS_DIR / "root/.shl_tools_ready"

    if marker.exists():
        log("Ubuntu basic tools already installed.")
        return True

    log("Installing basic Ubuntu tools...")

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

    result = ubuntu_command(command, timeout=600)

    print(result.stdout, end="", flush=True)

    if result.returncode != 0:
        raise RuntimeError("Ubuntu tools installation failed")

    log("Ubuntu tools installed.")
    return True


def find_sshx():
    candidates = [
        SSHX_PATH,
        Path("/usr/local/bin/sshx"),
        Path("/usr/bin/sshx"),
        Path("/root/.local/bin/sshx"),
    ]

    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return path

    found = shutil.which("sshx")
    if found:
        return Path(found)

    return None


def install_sshx():
    global SSHX_PATH

    found = find_sshx()
    if found:
        SSHX_PATH = found
        log(f"SSHX already installed: {SSHX_PATH}")
        return SSHX_PATH

    SSHX_DIR.mkdir(parents=True, exist_ok=True)

    archive = BASE_DIR / "sshx.tar.gz"

    log("Downloading SSHX...")

    try:
        download(
            SSHX_URL,
            archive,
            "SSHX",
        )

        extract_dir = BASE_DIR / "sshx-extract"

        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        extract_dir.mkdir()

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extract_dir)

        binary = None

        for p in extract_dir.rglob("sshx"):
            if p.is_file():
                binary = p
                break

        if binary is None:
            raise RuntimeError("sshx binary not found in archive")

        shutil.copy2(binary, SSHX_PATH)
        SSHX_PATH.chmod(0o755)

        log(f"SSHX installed: {SSHX_PATH}")
        return SSHX_PATH

    except Exception as e:
        log(f"SSHX installation failed: {e}")
        raise


def start_sshx():
    global _sshx_process
    global _sshx_link

    with _lock:
        if _sshx_process is not None and _sshx_process.poll() is None:
            return _sshx_link

        path = install_sshx()

        log("Starting SSHX...")

        env = os.environ.copy()
        env["PATH"] = f"{path.parent}:{env.get('PATH', '')}"

        _sshx_process = subprocess.Popen(
            [str(path), "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )

        link = None
        start = time.time()

        while time.time() - start < 30:
            line = _sshx_process.stdout.readline()

            if line:
                line = line.rstrip()
                log("[SSHX] " + line)

                match = re.search(
                    r"https://sshx\.io/s/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+",
                    line,
                )

                if match:
                    link = match.group(0)
                    break

            if _sshx_process.poll() is not None:
                break

        if not link:
            raise RuntimeError("SSHX URL was not generated")

        _sshx_link = link
        log(f"SSHX URL: {_sshx_link}")

        return _sshx_link


def bootstrap():
    if STATE_FILE.exists():
        if ROOTFS_DIR.exists() and PROOT_PATH.exists():
            try:
                test_ubuntu()
                install_tools()
                return True
            except Exception:
                log("Existing bootstrap state is invalid; rebuilding.")

        STATE_FILE.unlink(missing_ok=True)

    if not acquire_lock():
        log("Another bootstrap process is running. Waiting...")

        for _ in range(180):
            if STATE_FILE.exists():
                return True
            time.sleep(1)

        raise RuntimeError("Bootstrap lock timeout")

    try:
        if STATE_FILE.exists():
            return True

        arch = detect_arch()

        if arch != "amd64":
            raise RuntimeError("This build currently supports amd64 only")

        if not install_rootfs():
            raise RuntimeError("Ubuntu rootfs could not be installed")

        if not install_proot():
            raise RuntimeError("PRoot could not be installed")

        test_ubuntu()
        install_tools()

        STATE_FILE.write_text(
            f"ok\n"
            f"pid={os.getpid()}\n"
            f"time={time.time()}\n"
        )

        return True

    finally:
        release_lock()


st.set_page_config(
    page_title="SHL Server",
    page_icon="🖥️",
    layout="wide",
)

st.title("SHL Server")

if "boot_done" not in st.session_state:
    st.session_state.boot_done = False

if not st.session_state.boot_done:
    try:
        with st.spinner("Starting SHL runtime..."):
            bootstrap()

        st.session_state.boot_done = True
        st.success("Ubuntu + PRoot آماده است.")

    except Exception as e:
        st.error(str(e))
        st.code(
            "\n".join([
                f"BASE_DIR={BASE_DIR}",
                f"ROOTFS_DIR={ROOTFS_DIR}",
                f"PROOT_PATH={PROOT_PATH}",
            ])
        )
        st.stop()


try:
    link = start_sshx()

    st.subheader("SSHX")

    st.code(link)

    st.markdown(
        f"### [Open SSHX]({link})"
    )

except Exception as e:
    st.error(f"SSHX error: {e}")


st.subheader("Ubuntu")

col1, col2 = st.columns(2)

with col1:
    if st.button("Test Ubuntu"):
        result = ubuntu_command(
            """
echo "USER=$(id -un)"
echo "UID=$(id -u)"
echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
echo "ARCH=$(uname -m)"
echo "ROOTFS=$(df -h / | tail -1)"
""",
            timeout=60,
        )

        st.code(result.stdout)

with col2:
    if st.button("Restart SSHX"):
        if _sshx_process is not None:
            try:
                _sshx_process.terminate()
            except Exception:
                pass

        _sshx_process = None
        _sshx_link = None

        st.rerun()


st.caption(
    f"Ubuntu: {ROOTFS_DIR} | "
    f"PRoot: {PROOT_PATH}"
)
PY

cat > requirements.txt <<'EOF'
streamlit==1.64.0
EOF

python3.11 -m py_compile main.py

git add main.py requirements.txt
git commit -m "fix self rebuilding Ubuntu PRoot bootstrap"
git push origin main
