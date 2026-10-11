/*
 * xkbcomp-shim — 让随包 Xvfb 找到随包 xkbcomp（fnOS 宿主 /usr/bin 无 xkbcomp）。
 *
 * 背景：xorg-server 用编译期固定路径 /usr/bin/xkbcomp 启动键盘映射编译器，
 * 且不提供运行时覆盖。fnOS 宿主（非 root 应用用户）无法向 /usr/bin 写文件。
 *
 * 方案：LD_PRELOAD 本库只对 Xvfb 生效，拦截 execve 调用，把
 * "/usr/bin/xkbcomp" 重定向到环境变量 DOUYIN_XKBCOMP 指定的随包二进制。
 *
 * 构建：gcc -shared -fPIC -O2 -o xkbcomp-shim.so xkbcomp-shim.c -ldl
 */
#define _GNU_SOURCE
#include <unistd.h>
#include <string.h>
#include <stdlib.h>
#include <dlfcn.h>

typedef int (*execve_fn)(const char *, char *const[], char *const[]);

static execve_fn real_execve(void)
{
    static execve_fn fn = NULL;
    if (!fn) {
        fn = (execve_fn)dlsym(RTLD_NEXT, "execve");
    }
    return fn;
}

int execve(const char *path, char *const argv[], char *const envp[])
{
    if (path && strcmp(path, "/usr/bin/xkbcomp") == 0) {
        const char *alt = getenv("DOUYIN_XKBCOMP");
        if (alt && *alt) {
            path = alt;
        }
    }
    return real_execve()(path, argv, envp);
}
