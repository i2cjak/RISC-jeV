"""Compile visitor C in a filesystem/network sandbox, never in the app's context."""

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class CompileError(RuntimeError):
    pass


def compile_source(source, identifier):
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > 12000:
        raise CompileError("Enter a C program up to 12 KB.")
    prison = os.environ.get("COMPILER_ROOT")
    parent = Path(prison) / "work" if prison else ROOT / "build/compiles"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{identifier}-", dir=parent))
    try:
        (directory / "program.c").write_text(source)
        for name in ("io.h", "start.S", "link.ld"):
            shutil.copyfile(ROOT / "firmware" / name, directory / name)
        sandbox = str(ROOT / "build/compiler_sandbox")
        if prison:
            if os.geteuid() != 0:
                raise CompileError("Compiler sandbox is unavailable.")
            uid = 10000 + int(identifier[:6], 16) % 50000
            os.chown(directory, uid, uid)
            prefix = [sandbox, "--chroot", prison, f"/work/{directory.name}"]
        else:
            if not shutil.which("bwrap"):
                raise CompileError("Compiler sandbox is unavailable.")
            prefix = [
                "bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--bind", str(directory), "/work", "--ro-bind", sandbox, "/sandbox",
                "--clearenv", "--", "/sandbox", "--isolated", "/work",
            ]

        def execute(arguments, output_limit=8000):
            with tempfile.TemporaryFile() as diagnostics:
                process = subprocess.Popen(prefix + arguments, stdout=diagnostics, stderr=subprocess.STDOUT, env={}, start_new_session=True)
                try:
                    code = process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise CompileError("Compilation exceeded 12 seconds.") from None
                diagnostics.seek(0)
                output = diagnostics.read(output_limit).decode(errors="replace")
                if code:
                    raise CompileError(output or "Compilation failed.")
                return output

        execute([
            "/usr/bin/riscv64-unknown-elf-gcc", "-march=rv32i", "-mabi=ilp32", "-O1",
            "-ffreestanding", "-fno-builtin", "-fno-pic", "-msmall-data-limit=0", "-nostdlib", "-nostartfiles",
            "-fdiagnostics-color=never", "-Wl,--no-relax", "-T", "link.ld", "start.S", "program.c", "-o", "program.elf",
        ])
        execute(["/usr/bin/riscv64-unknown-elf-objcopy", "-O", "binary", "program.elf", "program.bin"])
        disassembly = execute(["/usr/bin/riscv64-unknown-elf-objdump", "-d", "program.elf"], 1024 * 1024)
        binary = (directory / "program.bin").read_bytes()
        if not binary or len(binary) > 65536:
            raise CompileError("Program must fit in 64 KiB.")
        destination = ROOT / "build" / f"custom-{identifier}.bin"
        destination.write_bytes(binary)
        return destination, disassembly
    finally:
        shutil.rmtree(directory)
