/*
 * aipm-systemd-restart.c
 *
 * Canonical root-owned privileged broker for bounded systemd update execution.
 *
 * Architecture & Security Model:
 * - Unprivileged executor (uid 995 / aipm-executor) invokes this helper via sudo
 *   with an exact-argument specification.
 * - This binary performs independent, compile-time bounded authorization:
 *     1. Requires effective UID 0 (root).
 *     2. Strictly verifies argc == 3 (only --unit=<unit> and --verb=<verb>).
 *     3. Strictly verifies unit name matches the compiled allowlist (aipm-dashboard.service).
 *     4. Strictly verifies verb matches the approved operation (try-restart).
 *     5. Sanitizes environment (PATH=/usr/bin:/bin only).
 *     6. Invokes /bin/systemctl directly via execve() (no shell interpretation).
 *
 * Fail-Closed Policy:
 * - Any violation of argument count, argument structure, unit allowlist, verb allowlist,
 *   or process credentials immediately logs to stderr and terminates with non-zero exit code.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef SYSTEMCTL_BIN
#define SYSTEMCTL_BIN "/bin/systemctl"
#endif
#define UNIT_PREFIX "--unit="
#define VERB_PREFIX "--verb="

#ifndef AIPM_ALLOWED_UNIT
#define AIPM_ALLOWED_UNIT "aipm-dashboard.service"
#endif

#ifndef AIPM_ALLOWED_VERB
#define AIPM_ALLOWED_VERB "try-restart"
#endif

static const char *const ALLOWLISTED_UNITS[] = {
    AIPM_ALLOWED_UNIT,
    NULL
};

static const char *const ALLOWLISTED_VERBS[] = {
    AIPM_ALLOWED_VERB,
    NULL
};

static int is_allowlisted(const char *value, const char *const allowlist[]) {
    if (!value || !*value) {
        return 0;
    }
    for (size_t i = 0; allowlist[i] != NULL; i++) {
        if (strcmp(value, allowlist[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

static int contains_prohibited_chars(const char *str) {
    if (!str) {
        return 1;
    }
    const char *prohibited = "@;\\/|&$`\n\r\t \t'\"<>*?[]{}()";
    while (*str) {
        if (strchr(prohibited, *str) != NULL) {
            return 1;
        }
        str++;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    /* 1. Root EUID enforcement (bypassed only in unit test builds compiled with -DTEST_ALLOW_NON_ROOT) */
#ifndef TEST_ALLOW_NON_ROOT
    if (geteuid() != 0) {
        fprintf(stderr, "Error: aipm-systemd-restart requires root privileges (geteuid() != 0)\n");
        return 1;
    }
#endif

    /* 2. Strict argument count validation */
    if (argc != 3) {
        fprintf(stderr, "Error: invalid argument count (expected 2 options, got %d)\n", argc - 1);
        return 1;
    }

    const char *unit_arg = NULL;
    const char *verb_arg = NULL;

    /* 3. Strict option structure validation (accept --unit=... and --verb=...) */
    size_t unit_prefix_len = strlen(UNIT_PREFIX);
    size_t verb_prefix_len = strlen(VERB_PREFIX);

    for (int i = 1; i < 3; i++) {
        if (strncmp(argv[i], UNIT_PREFIX, unit_prefix_len) == 0 && unit_arg == NULL) {
            unit_arg = argv[i] + unit_prefix_len;
        } else if (strncmp(argv[i], VERB_PREFIX, verb_prefix_len) == 0 && verb_arg == NULL) {
            verb_arg = argv[i] + verb_prefix_len;
        } else {
            fprintf(stderr, "Error: unrecognized or duplicate argument: '%s'\n", argv[i]);
            return 1;
        }
    }

    if (!unit_arg || !*unit_arg) {
        fprintf(stderr, "Error: missing required --unit=<unit> option\n");
        return 1;
    }

    if (!verb_arg || !*verb_arg) {
        fprintf(stderr, "Error: missing required --verb=<verb> option\n");
        return 1;
    }

    /* 4. Strict prohibited characters check */
    if (contains_prohibited_chars(unit_arg)) {
        fprintf(stderr, "Error: unit name contains prohibited characters: '%s'\n", unit_arg);
        return 1;
    }

    if (contains_prohibited_chars(verb_arg)) {
        fprintf(stderr, "Error: verb contains prohibited characters: '%s'\n", verb_arg);
        return 1;
    }

    /* 5. Strict unit allowlist check */
    if (!is_allowlisted(unit_arg, ALLOWLISTED_UNITS)) {
        fprintf(stderr, "Error: unauthorized systemd unit: '%s'\n", unit_arg);
        return 1;
    }

    /* 6. Strict verb allowlist check */
    if (!is_allowlisted(verb_arg, ALLOWLISTED_VERBS)) {
        fprintf(stderr, "Error: unauthorized systemd verb: '%s'\n", verb_arg);
        return 1;
    }

    /* 7. Direct argv execution of /bin/systemctl without shell */
    char *const safe_argv[] = {
        (char *)SYSTEMCTL_BIN,
        (char *)verb_arg,
        (char *)unit_arg,
        NULL
    };

    char *const safe_envp[] = {
        "PATH=/usr/bin:/bin",
        NULL
    };

    execve(SYSTEMCTL_BIN, safe_argv, safe_envp);

    /* If execve returns, an error occurred */
    perror("execve systemctl");
    return 1;
}
