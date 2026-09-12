#include "MainWindow.h"
#include <shellapi.h>
#include <objidl.h>
#include <gdiplus.h>
#include <fstream>

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    const HRESULT com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    Gdiplus::GdiplusStartupInput startup;
    ULONG_PTR gdiplus = 0;
    Gdiplus::GdiplusStartup(&gdiplus, &startup, nullptr);
    int result = 1;
    try {
        wchar_t path[32768]{};
        GetModuleFileNameW(nullptr, path, 32768);
        auto root = std::filesystem::path(path).parent_path();
        for (int i = 0; i < 5 && !std::filesystem::exists(root / L"engine/main.py"); ++i) root = root.parent_path();
        if (!std::filesystem::exists(root / L"engine/main.py")) throw std::runtime_error("Cannot locate project engine/main.py.");
        nlohmann::json options = nlohmann::json::object();
        int count = 0;
        auto args = CommandLineToArgvW(GetCommandLineW(), &count);
        for (int i = 1; i + 1 < count; ++i) {
            if (std::wstring(args[i]).starts_with(L"--")) {
                options[Utf8(std::wstring(args[i]).substr(2))] = Utf8(args[i + 1]); ++i;
            }
        }
        LocalFree(args);
        MainWindow app(root, options);
        result = app.run(instance, show);
    } catch (const std::exception& e) {
        MessageBoxW(nullptr, Wide(e.what()).c_str(), L"ASMR-Cliper", MB_ICONERROR);
    }
    Gdiplus::GdiplusShutdown(gdiplus);
    if (SUCCEEDED(com)) CoUninitialize();
    return result;
}
