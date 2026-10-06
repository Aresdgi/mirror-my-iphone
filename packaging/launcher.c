/*
 * Mirror my iPhone launcher, the .app's main executable.
 *
 * Runs the app's Python code inside this process by loading libpython from the Python that the
 * app's environment (a venv, see find_venv) was created with. That way
 * macOS shows "Mirror my iPhone" with its own icon in the Dock and menu bar, not "Python", and
 * permission prompts (camera) name Mirror my iPhone. Passing the venv's interpreter as argv[0]
 * makes Python use the venv, so sys.executable works for subprocesses too.
 *
 * If no environment fits this version's requirements, it opens setup.command in Terminal
 * instead, which builds one and relaunches the app. If libpython can't be loaded, it
 * runs the venv's Python directly (works, but the Dock then shows Python).
 */

#include <dlfcn.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef APP_VERSION
#define APP_VERSION "dev" /* set by build_app.sh */
#endif

typedef int (*py_bytes_main_t)(int, char **);

/* Read a whole file into a NUL-terminated buffer. Returns NULL if it can't be read. */
static char *read_file(const char *path, size_t *length) {
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    char *data = NULL;
    size_t size = 0, capacity = 0, n;
    char chunk[4096];
    while ((n = fread(chunk, 1, sizeof chunk, file)) > 0) {
        if (size + n + 1 > capacity) {
            capacity = (size + n + 1) * 2;
            char *grown = realloc(data, capacity);
            if (!grown) { free(data); fclose(file); return NULL; }
            data = grown;
        }
        memcpy(data + size, chunk, n);
        size += n;
    }
    fclose(file);
    if (!data && !(data = calloc(1, 1))) return NULL;
    data[size] = '\0';
    if (length) *length = size;
    return data;
}

static int same_contents(const char *a, const char *b) {
    size_t length_a, length_b;
    char *data_a = read_file(a, &length_a), *data_b = read_file(b, &length_b);
    int same = data_a && data_b && length_a == length_b && memcmp(data_a, data_b, length_a) == 0;
    free(data_a);
    free(data_b);
    return same;
}

/* A venv is usable if its interpreter exists and it was built for this app's requirements. */
static int venv_ready(const char *venv, const char *requirements) {
    char python[PATH_MAX], stamp[PATH_MAX];
    snprintf(python, sizeof python, "%s/bin/python3", venv);
    snprintf(stamp, sizeof stamp, "%s/.requirements.lock", venv);
    return access(python, X_OK) == 0 && same_contents(requirements, stamp);
}

/* The first usable environment: $MIRROR_MY_IPHONE_VENV, the one the Homebrew cask builds in its
   Caskroom, then ~/Library/Application Support/Mirror my iPhone/venv (made by setup.command). */
static int find_venv(const char *requirements, char *venv, size_t size) {
    const char *home = getenv("HOME");
    const char *override = getenv("MIRROR_MY_IPHONE_VENV");
    char candidates[4][PATH_MAX];
    int count = 0;
    if (override && *override) snprintf(candidates[count++], PATH_MAX, "%s", override);
    snprintf(candidates[count++], PATH_MAX, "/opt/homebrew/Caskroom/mirror-my-iphone/%s/venv", APP_VERSION);
    snprintf(candidates[count++], PATH_MAX, "/usr/local/Caskroom/mirror-my-iphone/%s/venv", APP_VERSION);
    snprintf(candidates[count++], PATH_MAX, "%s/Library/Application Support/Mirror my iPhone/venv", home ? home : "");
    for (int i = 0; i < count; i++) {
        if (venv_ready(candidates[i], requirements)) {
            snprintf(venv, size, "%s", candidates[i]);
            return 1;
        }
    }
    return 0;
}

/* Cut `path` after its last '/' `levels` times, in place. */
static void parent_dir(char *path, int levels) {
    while (levels-- > 0) {
        char *slash = strrchr(path, '/');
        if (slash) *slash = '\0';
    }
}

int main(int argc, char **argv) {
    char executable[PATH_MAX], contents[PATH_MAX];
    uint32_t size = sizeof executable;
    if (_NSGetExecutablePath(executable, &size) != 0 || !realpath(executable, contents)) {
        fprintf(stderr, "Mirror my iPhone: can't locate the app bundle\n");
        return 1;
    }
    parent_dir(contents, 2); /* .../Mirror my iPhone.app/Contents/MacOS/Mirror my iPhone → .../Contents */

    char main_py[PATH_MAX], requirements[PATH_MAX], setup[PATH_MAX];
    snprintf(main_py, sizeof main_py, "%s/Resources/app/main.py", contents);
    snprintf(requirements, sizeof requirements, "%s/Resources/app/requirements.lock", contents);
    snprintf(setup, sizeof setup, "%s/Resources/setup.command", contents);

    char venv[PATH_MAX], python[PATH_MAX], libpython_file[PATH_MAX];
    if (!find_venv(requirements, venv, sizeof venv)) {
        execl("/usr/bin/open", "open", "-a", "Terminal", setup, (char *)NULL);
        perror("Mirror my iPhone: open setup.command");
        return 1;
    }
    snprintf(python, sizeof python, "%s/bin/python3", venv);
    snprintf(libpython_file, sizeof libpython_file, "%s/.libpython", venv);

    /* The app bundle stays untouched: no __pycache__ inside it, which would break its signature */
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    /* Python's argv: the venv interpreter (selects the venv), main.py, then our arguments minus
       the -psn_ process serial number older macOS versions pass */
    char **python_argv = calloc((size_t)argc + 2, sizeof(char *));
    if (!python_argv) return 1;
    int python_argc = 0;
    python_argv[python_argc++] = python;
    python_argv[python_argc++] = main_py;
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "-psn_", 5) != 0) python_argv[python_argc++] = argv[i];
    python_argv[python_argc] = NULL;

    char *libpython = read_file(libpython_file, NULL);
    if (libpython) libpython[strcspn(libpython, "\r\n")] = '\0';
    if (libpython && *libpython) {
        void *library = dlopen(libpython, RTLD_NOW | RTLD_GLOBAL);
        py_bytes_main_t py_bytes_main = library ? (py_bytes_main_t)dlsym(library, "Py_BytesMain") : NULL;
        if (py_bytes_main) return py_bytes_main(python_argc, python_argv);
        fprintf(stderr, "Mirror my iPhone: can't load %s (%s), running Python directly\n", libpython, dlerror());
    }
    execv(python, python_argv);
    perror("Mirror my iPhone: exec python");
    return 1;
}
