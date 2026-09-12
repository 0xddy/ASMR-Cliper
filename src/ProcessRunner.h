#pragma once
#include <windows.h>
#include <atomic>
#include <filesystem>
#include <string>
#include <thread>
#include <vector>

inline constexpr UINT WM_ENGINE_LINE = WM_APP + 40;
inline constexpr UINT WM_ENGINE_DONE = WM_APP + 41;
std::wstring QuoteWindowsArgument(const std::wstring& argument);

class ProcessRunner {
public:
    ~ProcessRunner();
    void start(HWND window, const std::filesystem::path& executable,
               const std::vector<std::wstring>& arguments, const std::filesystem::path& cwd,
               const std::filesystem::path& log);
    void cancel();
    void send(const std::string& line);
    void finish();
    bool running() const { return running_; }
private:
    HANDLE process_ = nullptr;
    HANDLE job_ = nullptr;
    HANDLE input_ = nullptr;
    std::thread reader_;
    std::atomic_bool running_ = false;
};
