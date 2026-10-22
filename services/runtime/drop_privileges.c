#define _GNU_SOURCE

#include <errno.h>
#include <grp.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static unsigned long parse_id(const char *value, const char *label) {
    char *end = NULL;
    unsigned long parsed;

    errno = 0;
    parsed = strtoul(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed > UINT_MAX) {
        fprintf(stderr, "invalid %s: %s\n", label, value);
        exit(64);
    }
    return parsed;
}

int main(int argc, char **argv) {
    uid_t uid;
    gid_t gid;

    if (argc < 4) {
        fprintf(stderr, "usage: drop-privileges UID GID PROGRAM [ARG ...]\n");
        return 64;
    }
    if (geteuid() != 0) {
        fprintf(stderr, "drop-privileges must start as root\n");
        return 77;
    }

    uid = (uid_t)parse_id(argv[1], "uid");
    gid = (gid_t)parse_id(argv[2], "gid");
    if (setgroups(0, NULL) != 0 || setgid(gid) != 0 || setuid(uid) != 0) {
        perror("unable to drop privileges");
        return 77;
    }
    if (geteuid() != uid || getegid() != gid) {
        fprintf(stderr, "privilege drop verification failed\n");
        return 77;
    }

    execvp(argv[3], &argv[3]);
    perror("unable to execute service");
    return 126;
}
