/* Ordinary Win32 launcher: no embedded interpreter, archive or self-extraction.
 * Only the application-owned isolated Python and fixed entry script are run.
 * Diagnostics inherit an explicit standard-handle allowlist, never arbitrary
 * handles owned by the launching process. Normal GUI startup inherits none.
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif
#include <windows.h>
#include <shellapi.h>
#include <wchar.h>
#include <stdlib.h>
#include <string.h>

#define PATH_LIMIT 32768
#define COMMAND_LIMIT 32767

static void report_error(const wchar_t *message, int diagnostic) {
    if (diagnostic) {
        char encoded[2048];
        DWORD written;
        int count = WideCharToMultiByte(CP_UTF8, 0, message, -1,
                                       encoded, sizeof(encoded), NULL, NULL);
        HANDLE error = GetStdHandle(STD_ERROR_HANDLE);
        if (count > 1 && error && error != INVALID_HANDLE_VALUE) {
            WriteFile(error, encoded, (DWORD)(count - 1), &written, NULL);
            WriteFile(error, "\r\n", 2, &written, NULL);
        }
    } else {
        MessageBoxW(NULL, message, L"文字起こし統合スタジオ", MB_OK | MB_ICONERROR);
    }
}

static int ordinary_file(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES &&
           !(attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT));
}

static int ordinary_directory(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES &&
           (attributes & FILE_ATTRIBUTE_DIRECTORY) &&
           !(attributes & FILE_ATTRIBUTE_REPARSE_POINT);
}

static int path_join(wchar_t *output, const wchar_t *root, const wchar_t *suffix) {
    size_t a = wcslen(root), b = wcslen(suffix);
    if (a + b + 1 >= PATH_LIMIT) return 0;
    memcpy(output, root, a * sizeof(wchar_t));
    memcpy(output + a, suffix, (b + 1) * sizeof(wchar_t));
    return 1;
}

static int append_char(wchar_t *command, size_t *used, wchar_t value) {
    if (*used + 1 >= COMMAND_LIMIT) return 0;
    command[(*used)++] = value;
    command[*used] = L'\0';
    return 1;
}

/* Microsoft C argv rules: every argument is quoted. Backslashes immediately
 * before a quote, or the closing quote, are doubled. No command shell runs.
 */
static int append_argument(wchar_t *command, size_t *used, const wchar_t *value) {
    size_t slashes = 0;
    if (*used && !append_char(command, used, L' ')) return 0;
    if (!append_char(command, used, L'"')) return 0;
    for (;; ++value) {
        if (*value == L'\\') {
            ++slashes;
            continue;
        }
        if (*value == L'"' || *value == L'\0') {
            size_t count = slashes * 2 + (*value == L'"' ? 1 : 0);
            while (count--) if (!append_char(command, used, L'\\')) return 0;
        } else {
            while (slashes > 0) {
                --slashes;
                if (!append_char(command, used, L'\\')) return 0;
            }
        }
        slashes = 0;
        if (*value == L'\0') break;
        if (!append_char(command, used, *value)) return 0;
    }
    return append_char(command, used, L'"');
}

static HANDLE duplicate_output(DWORD standard) {
    HANDLE source = GetStdHandle(standard), result = NULL;
    SECURITY_ATTRIBUTES security = {sizeof(SECURITY_ATTRIBUTES), NULL, TRUE};
    if (source && source != INVALID_HANDLE_VALUE &&
        DuplicateHandle(GetCurrentProcess(), source, GetCurrentProcess(),
                        &result, 0, TRUE, DUPLICATE_SAME_ACCESS)) return result;
    return CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       &security,
                       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous,
                    PWSTR ignored_command, int show) {
    wchar_t *root = NULL, *python = NULL, *entry = NULL, *directory = NULL;
    wchar_t *command = NULL, **arguments = NULL, *slash;
    STARTUPINFOEXW startup;
    PROCESS_INFORMATION process;
    HANDLE handles[3] = {NULL, NULL, NULL};
    SIZE_T attribute_bytes = 0;
    size_t used = 0;
    DWORD length, exit_code = 1, flags = CREATE_NO_WINDOW | CREATE_DEFAULT_ERROR_MODE;
    int count = 0, diagnostic = 0, ok = 0, attributes_initialized = 0;
    BOOL inherit = FALSE;
    SECURITY_ATTRIBUTES security = {sizeof(SECURITY_ATTRIBUTES), NULL, TRUE};
    (void)instance; (void)previous; (void)ignored_command; (void)show;
    ZeroMemory(&startup, sizeof(startup));
    ZeroMemory(&process, sizeof(process));
    startup.StartupInfo.cb = sizeof(STARTUPINFOW);
    arguments = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!arguments || count < 1) goto failure;
    diagnostic = count > 1 && !(count == 2 && wcscmp(arguments[1], L"--background") == 0);
    root = calloc(PATH_LIMIT, sizeof(wchar_t));
    python = calloc(PATH_LIMIT, sizeof(wchar_t));
    entry = calloc(PATH_LIMIT, sizeof(wchar_t));
    directory = calloc(PATH_LIMIT, sizeof(wchar_t));
    command = calloc(COMMAND_LIMIT, sizeof(wchar_t));
    if (!root || !python || !entry || !directory || !command) goto failure;
    length = GetModuleFileNameW(NULL, root, PATH_LIMIT);
    if (!length || length >= PATH_LIMIT) goto failure;
    slash = wcsrchr(root, L'\\');
    if (!slash || slash == root) goto failure;
    *slash = L'\0';
    if (!path_join(directory, root, L"\\Runtime") || !ordinary_directory(directory) ||
        !path_join(python, root, diagnostic ? L"\\Runtime\\python.exe" : L"\\Runtime\\pythonw.exe") ||
        !path_join(entry, root, L"\\_internal\\native_app_entry.py") ||
        !ordinary_file(python) || !ordinary_file(entry)) {
        report_error(L"実行ファイルが不足しています。セットアップexeの「修復」を実行してください。", diagnostic);
        goto cleanup;
    }
    if (!append_argument(command, &used, python) ||
        !append_argument(command, &used, L"-I") ||
        !append_argument(command, &used, L"-X") ||
        !append_argument(command, &used, L"utf8") ||
        !append_argument(command, &used, entry)) goto failure;
    for (int index = 1; index < count; ++index) {
        if (!append_argument(command, &used, arguments[index])) goto failure;
    }
    /* Remove the caller's current directory from DLL lookup. The child starts
     * in this application's folder and names its interpreter explicitly.
     */
    if (!SetDllDirectoryW(L"")) goto failure;
    if (diagnostic) {
        handles[0] = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                &security, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        handles[1] = duplicate_output(STD_OUTPUT_HANDLE);
        handles[2] = duplicate_output(STD_ERROR_HANDLE);
        for (int index = 0; index < 3; ++index) {
            if (!handles[index] || handles[index] == INVALID_HANDLE_VALUE) goto failure;
        }
        InitializeProcThreadAttributeList(NULL, 1, 0, &attribute_bytes);
        if (!attribute_bytes) goto failure;
        startup.lpAttributeList = HeapAlloc(GetProcessHeap(), 0, attribute_bytes);
        if (!startup.lpAttributeList ||
            !InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &attribute_bytes)) goto failure;
        attributes_initialized = 1;
        if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, sizeof(handles), NULL, NULL)) goto failure;
        startup.StartupInfo.cb = sizeof(STARTUPINFOEXW);
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = handles[0];
        startup.StartupInfo.hStdOutput = handles[1];
        startup.StartupInfo.hStdError = handles[2];
        flags |= EXTENDED_STARTUPINFO_PRESENT;
        inherit = TRUE;
    }
    if (!CreateProcessW(python, command, NULL, NULL, inherit, flags,
                        NULL, root, &startup.StartupInfo, &process)) goto failure;
    if (WaitForSingleObject(process.hProcess, INFINITE) != WAIT_OBJECT_0 ||
        !GetExitCodeProcess(process.hProcess, &exit_code)) goto failure;
    ok = 1;
    goto cleanup;

failure:
    report_error(L"アプリを起動できませんでした。セットアップexeで修復してください。", diagnostic);
cleanup:
    if (process.hThread) CloseHandle(process.hThread);
    if (process.hProcess) CloseHandle(process.hProcess);
    if (startup.lpAttributeList) {
        if (attributes_initialized) DeleteProcThreadAttributeList(startup.lpAttributeList);
        HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
    }
    for (int index = 0; index < 3; ++index) {
        if (handles[index] && handles[index] != INVALID_HANDLE_VALUE) CloseHandle(handles[index]);
    }
    if (arguments) LocalFree(arguments);
    free(root); free(python); free(entry); free(directory); free(command);
    return ok ? (int)exit_code : 1;
}
