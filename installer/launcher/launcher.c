/*Sage Launcher  —  installer/launcher/launcher.c*/

#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <wchar.h>

static void FatalError(LPCWSTR context, DWORD code)
{
    WCHAR   msg[512];
    LPWSTR  sysmsg = NULL;

    FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
        FORMAT_MESSAGE_IGNORE_INSERTS,
        NULL, code,
        MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        (LPWSTR)&sysmsg, 0, NULL
    );

    _snwprintf_s(msg, 512, _TRUNCATE,
        L"%s\n\nError %lu: %s\n\nPlease reinstall Sage.",
        context,
        code,
        sysmsg ? sysmsg : L"(unknown)"
    );

    if (sysmsg) LocalFree(sysmsg);
    MessageBoxW(NULL, msg, L"Sage \u2014 Launch Error", MB_OK | MB_ICONERROR);
}

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev, LPSTR lpCmd, int nShow)
{
    (void)hInst; (void)hPrev; (void)lpCmd; (void)nShow;
    SetCurrentProcessExplicitAppUserModelID(L"com.tub.sage");
    WCHAR installDir[MAX_PATH];
    if (!GetModuleFileNameW(NULL, installDir, MAX_PATH)) {
        FatalError(L"Could not resolve launcher path.", GetLastError());
        return 1;
    }

    WCHAR *lastSlash = wcsrchr(installDir, L'\\');
    if (!lastSlash) {
        MessageBoxW(NULL,
            L"Unexpected launcher path format.\n\nPlease reinstall Sage.",
            L"Sage \u2014 Launch Error", MB_OK | MB_ICONERROR);
        return 1;
    }
    *lastSlash = L'\0';

    WCHAR pythonw[MAX_PATH];
    _snwprintf_s(pythonw, MAX_PATH, _TRUNCATE,
                 L"%s\\python\\pythonw.exe", installDir);

    if (GetFileAttributesW(pythonw) == INVALID_FILE_ATTRIBUTES) {
        WCHAR err[MAX_PATH + 128];
        _snwprintf_s(err, MAX_PATH + 128, _TRUNCATE,
            L"Python runtime not found:\n\n  %s\n\nPlease reinstall Sage.",
            pythonw);
        MessageBoxW(NULL, err, L"Sage \u2014 Launch Error", MB_OK | MB_ICONERROR);
        return 1;
    }
    WCHAR cmdLine[MAX_PATH + 32];
    _snwprintf_s(cmdLine, MAX_PATH + 32, _TRUNCATE,
                 L"\"%s\" -m sage", pythonw);

    STARTUPINFOW si;
    ZeroMemory(&si, sizeof(si));
    si.cb          = sizeof(si);
    si.dwFlags     = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;

    PROCESS_INFORMATION pi;
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(
            pythonw,          /* lpApplicationName  — explicit path   */
            cmdLine,          /* lpCommandLine      — mutable buffer   */
            NULL,             /* lpProcessAttributes                   */
            NULL,             /* lpThreadAttributes                    */
            FALSE,            /* bInheritHandles                       */
            CREATE_NO_WINDOW, /* dwCreationFlags                       */
            NULL,             /* lpEnvironment  — inherit from parent  */
            installDir,       /* lpCurrentDirectory                    */
            &si,
            &pi))
    {
        FatalError(L"Failed to start Sage.", GetLastError());
        return 1;
    }
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return 0;
}