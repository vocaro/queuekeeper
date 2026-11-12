#include <errno.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>

extern char **environ;

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: QueueLauncher EXECUTABLE [ARGUMENTS...]\n");
        return 2;
    }

    // Keep the signed app alive while macOS attributes the worker's network access to it.
    pid_t child;
    int result = posix_spawn(&child, argv[1], NULL, NULL, argv + 1, environ);
    if (result != 0) {
        fprintf(stderr, "posix_spawn: %s\n", strerror(result));
        return 1;
    }
    int status;
    while (waitpid(child, &status, 0) == -1) {
        if (errno != EINTR) {
            perror("waitpid");
            return 1;
        }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
