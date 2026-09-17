// Compiler confinement. No setuid bit; chroot mode is for the Railway container.
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define DENY(n) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, __NR_##n, 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|EPERM)
static void die(const char *message) { perror(message); exit(126); }
static void limit(int resource, rlim_t value) {
    struct rlimit limits = {value, value};
    if (setrlimit(resource, &limits)) die("resource limit");
}
int main(int argc, char **argv) {
    if (argc < 4) return 126;
    int command = 3;
    if (!strcmp(argv[1], "--chroot")) {
        if (argc < 5 || geteuid() != 0) return 126;
        if (chroot(argv[2]) || chdir(argv[3])) die("compiler root");
        struct stat info;
        if (stat(".", &info) || info.st_uid == 0) die("compiler owner");
        if (setgroups(0, NULL) || setgid(info.st_gid) || setuid(info.st_uid)) die("compiler identity");
        command = 4;
    } else if (!strcmp(argv[1], "--isolated")) {
        if (chdir(argv[2])) die("compiler directory");
    } else return 126;
    limit(RLIMIT_CPU, 8);
    limit(RLIMIT_AS, 384 * 1024 * 1024);
    limit(RLIMIT_FSIZE, 4 * 1024 * 1024);
    limit(RLIMIT_NOFILE, 64);
    limit(RLIMIT_CORE, 0);
    // Local bwrap shares the caller's real UID, whose existing tasks count too.
    if (command == 4) limit(RLIMIT_NPROC, 24);
    if (clearenv() || setenv("PATH", "/usr/bin:/bin", 1) || setenv("TMPDIR", ".", 1) || setenv("LC_ALL", "C", 1)) die("compiler environment");
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) die("no new privileges");
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, nr)),
        // Reject the x32 ABI as well as alternate architecture syscalls.
        BPF_JUMP(BPF_JMP|BPF_JGE|BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_KILL_PROCESS),
        DENY(socket), DENY(socketpair), DENY(connect), DENY(bind), DENY(listen),
        DENY(ptrace), DENY(process_vm_readv), DENY(process_vm_writev),
        DENY(mount), DENY(umount2), DENY(unshare), DENY(setns),
        DENY(setsid), DENY(setpgid), DENY(bpf), DENY(perf_event_open),
        DENY(keyctl), DENY(add_key), DENY(request_key), DENY(userfaultfd),
        DENY(io_uring_setup), DENY(open_by_handle_at),
        BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {sizeof(filter) / sizeof(filter[0]), filter};
    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program)) die("compiler seccomp");
    execv(argv[command], &argv[command]);
    die("compiler exec");
}
