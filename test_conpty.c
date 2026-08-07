#include <windows.h>
#include <stdio.h>

int main() {
    HANDLE hPipePTYIn, hPipeOut;
    HANDLE hPipeIn, hPipePTYOut;
    
    // Create pipes
    if (!CreatePipe(&hPipePTYIn, &hPipeOut, NULL, 0)) {
        printf("CreatePipe(input) failed: %lu\n", GetLastError());
        return 1;
    }
    if (!CreatePipe(&hPipeIn, &hPipePTYOut, NULL, 0)) {
        printf("CreatePipe(output) failed: %lu\n", GetLastError());
        return 1;
    }
    
    // Create ConPTY
    COORD size = {80, 24};
    HPCON hPC;
    HRESULT hr = CreatePseudoConsole(size, hPipePTYIn, hPipePTYOut, 0, &hPC);
    CloseHandle(hPipePTYIn);
    CloseHandle(hPipePTYOut);
    
    if (FAILED(hr)) {
        printf("CreatePseudoConsole failed: 0x%lx\n", hr);
        return 1;
    }
    printf("ConPTY created: %p\n", hPC);
    
    // Initialize STARTUPINFOEXW
    STARTUPINFOEXW si = {0};
    si.StartupInfo.cb = sizeof(STARTUPINFOEXW);
    
    size_t attrSize = 0;
    InitializeProcThreadAttributeList(NULL, 1, 0, &attrSize);
    si.lpAttributeList = (PPROC_THREAD_ATTRIBUTE_LIST)malloc(attrSize);
    InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &attrSize);
    
    if (!UpdateProcThreadAttribute(si.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hPC, sizeof(HPCON), NULL, NULL)) {
        printf("UpdateProcThreadAttribute failed: %lu\n", GetLastError());
        return 1;
    }
    
    // Spawn process
    wchar_t cmd[] = L"cmd.exe /c \"echo Hello from ConPTY && echo Second line\"";
    PROCESS_INFORMATION pi = {0};
    
    printf("Spawning process...\n");
    
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, EXTENDED_STARTUPINFO_PRESENT, NULL, NULL, &si.StartupInfo, &pi)) {
        printf("CreateProcessW failed: %lu\n", GetLastError());
        return 1;
    }
    printf("Process spawned: PID=%lu\n", pi.dwProcessId);
    
    // Wait for process
    WaitForSingleObject(pi.hThread, 10000);
    Sleep(500);
    
    // Read from output pipe
    char buf[512];
    DWORD bytesRead;
    printf("Reading from ConPTY output...\n");
    
    while (ReadFile(hPipeIn, buf, sizeof(buf), &bytesRead, NULL) && bytesRead > 0) {
        printf("[CAPTURED %lu bytes]: %.*s", bytesRead, (int)bytesRead, buf);
    }
    
    DWORD err = GetLastError();
    printf("ReadFile ended with error: %lu\n", err);
    
    // Cleanup
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    DeleteProcThreadAttributeList(si.lpAttributeList);
    free(si.lpAttributeList);
    ClosePseudoConsole(hPC);
    CloseHandle(hPipeIn);
    CloseHandle(hPipeOut);
    
    printf("Done.\n");
    return 0;
}