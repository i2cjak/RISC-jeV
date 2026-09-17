"""Build a secret-free chroot from the distro's RISC-V compiler and its libraries."""
import re
import shutil
import subprocess
from pathlib import Path

target = Path("/opt/compiler-root")
target.mkdir(parents=True, exist_ok=True)
sources = list(Path("/usr/bin").glob("riscv64-unknown-elf-*"))
for name in ("/usr/lib/gcc/riscv64-unknown-elf", "/usr/lib/riscv64-unknown-elf", "/usr/riscv64-unknown-elf"):
    path = Path(name)
    if path.exists():
        destination = target / path.relative_to("/")
        shutil.copytree(path, destination, symlinks=True, dirs_exist_ok=True)
        sources.extend(p for p in path.rglob("*") if p.is_file())
for source in sources:
    destination = target / source.relative_to("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
    with source.open("rb") as file:
        header = file.read(20)
    if header[:4] != b"\x7fELF" or header[18:20] != b"\x3e\x00":
        continue
    result = subprocess.run(["ldd", str(source)], capture_output=True, text=True, check=False)
    for library in re.findall(r"(?:=> )?(/[^\s]+)", result.stdout):
        dependency = Path(library)
        destination = target / dependency.relative_to("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(dependency, destination)
(target / "work").mkdir(exist_ok=True)
