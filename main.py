import subprocess
import streamlit as st

st.set_page_config(
    page_title="Host Diagnostics",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 Streamlit Host Diagnostics")
st.caption("این اطلاعات مربوط به Host است، نه Ubuntu/PRoot/SSHX.")

commands = [
    ("1️⃣ Identity", "id"),
    ("2️⃣ Disk / Filesystems", "df -hT / /mount/admin /home/appuser /tmp"),
    ("3️⃣ Mounts", "mount | head -50"),
    ("4️⃣ /home/appuser mount", "findmnt -T /home/appuser"),
    ("5️⃣ /mount/admin mount", "findmnt -T /mount/admin"),
    ("6️⃣ Filesystem of /", "findmnt -T /"),
    ("7️⃣ Important directories", "ls -ld / /home /home/appuser /mount /mount/admin /tmp 2>&1"),
]

for title, command in commands:
    st.subheader(title)

    st.code(f"$ {command}", language="bash")

    try:
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=20,
        )

        output = result.stdout

        if result.stderr:
            output += "\n" + result.stderr

        if not output.strip():
            output = "(no output)"

        st.code(output)

    except Exception as e:
        st.error(f"Error: {e}")

st.divider()

st.info(
    "📌 خروجی کامل این صفحه را برای من بفرست. "
    "با آن مشخص می‌کنیم کدام مسیر در Streamlit واقعاً قابل استفاده برای persistence است."
)
