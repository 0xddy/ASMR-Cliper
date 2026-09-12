#include "ProcessRunner.h"
#include <fstream>
#include <algorithm>
#include <memory>
#include <stdexcept>

std::wstring QuoteWindowsArgument(const std::wstring& value) {
    // CommandLineToArgvW / CRT-compatible quoting; no shell is involved.
    std::wstring out = L"\"";
    size_t slashes = 0;
    for (wchar_t c : value) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'\"') {
            out.append(slashes * 2 + 1, L'\\');
            out.push_back(c);
        } else {
            out.append(slashes, L'\\');
            out.push_back(c);
        }
        slashes = 0;
    }
    out.append(slashes * 2, L'\\');
    out.push_back(L'\"');
    return out;
}

ProcessRunner::~ProcessRunner() { cancel(); finish(); }

void ProcessRunner::cancel() {
    if (job_) TerminateJobObject(job_, ERROR_CANCELLED);
}

void ProcessRunner::send(const std::string& line) {
    // Only short, newline-delimited control messages are sent from the UI thread.
    if(!running_||!input_||line.size()>4096)throw std::runtime_error("任务已结束，无法提交语言选择。");
    const auto data=line+"\n";DWORD written=0;
    if(!WriteFile(input_,data.data(),static_cast<DWORD>(data.size()),&written,nullptr)||written!=data.size())
        throw std::runtime_error("无法提交语言选择，请查看任务日志。");
}

void ProcessRunner::finish() {
    if (reader_.joinable()) reader_.join();
    if (input_) { CloseHandle(input_); input_ = nullptr; }
    if (process_) { CloseHandle(process_); process_ = nullptr; }
    if (job_) { CloseHandle(job_); job_ = nullptr; }
    running_ = false;
}

void ProcessRunner::start(HWND window, const std::filesystem::path& executable,
                          const std::vector<std::wstring>& arguments,
                          const std::filesystem::path& cwd, const std::filesystem::path& log) {
    if (running_) throw std::runtime_error("A process is already running.");
    finish();
    std::wstring command = QuoteWindowsArgument(executable.wstring());
    for (const auto& arg : arguments) command += L" " + QuoteWindowsArgument(arg);
    SECURITY_ATTRIBUTES sa{ sizeof(sa), nullptr, TRUE };
    HANDLE readPipe = nullptr, writePipe = nullptr, readInput = nullptr;
    PROCESS_INFORMATION pi{};
    auto cleanup = [&] {
        if (readPipe) CloseHandle(readPipe);
        if (writePipe) CloseHandle(writePipe);
        if (readInput) CloseHandle(readInput);
        if (input_) {CloseHandle(input_);input_=nullptr;}
    };
    if (!CreatePipe(&readPipe, &writePipe, &sa, 0)) throw std::runtime_error("Cannot create output pipe.");
    if (!SetHandleInformation(readPipe, HANDLE_FLAG_INHERIT, 0)) { cleanup(); throw std::runtime_error("Cannot protect pipe handle."); }
    if(!CreatePipe(&readInput,&input_,&sa,0)||!SetHandleInformation(input_,HANDLE_FLAG_INHERIT,0)) {
        cleanup();throw std::runtime_error("Cannot create task input pipe.");
    }
    job_ = CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job_ || !SetInformationJobObject(job_, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        cleanup(); if (job_) { CloseHandle(job_); job_ = nullptr; }
        throw std::runtime_error("Cannot create process cancellation group.");
    }
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = si.hStdError = writePipe;
    si.hStdInput = readInput;
    if (!CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, TRUE,
                        CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr, cwd.c_str(), &si, &pi)) {
        const auto error = GetLastError();
        cleanup(); CloseHandle(job_); job_ = nullptr;
        throw std::runtime_error("Cannot start Python. Windows error " + std::to_string(error));
    }
    if (!AssignProcessToJobObject(job_, pi.hProcess)) {
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
        cleanup(); CloseHandle(job_); job_ = nullptr;
        throw std::runtime_error("Cannot attach process to cancellation group.");
    }
    process_ = pi.hProcess;
    running_ = true;
    CloseHandle(writePipe); writePipe = nullptr;
    CloseHandle(readInput); readInput = nullptr;
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    const HANDLE process = process_;
    reader_ = std::thread([this, window, readPipe, process, log] {
        std::ofstream file(log, std::ios::binary);
        std::string pending;
        char buffer[8192];
        DWORD read = 0;
        auto send = [window](std::string line) {
            line.erase(std::remove(line.begin(),line.end(),'\0'),line.end());
            auto data = std::make_unique<std::string>(std::move(line));
            if (PostMessageW(window, WM_ENGINE_LINE, 0, reinterpret_cast<LPARAM>(data.get()))) data.release();
        };
        while (ReadFile(readPipe, buffer, sizeof(buffer), &read, nullptr) && read) {
            file.write(buffer, read); file.flush();
            pending.append(buffer, read);
            size_t end;
            while ((end = pending.find('\n')) != std::string::npos) {
                send(pending.substr(0, end)); pending.erase(0, end + 1);
            }
            if (pending.size() > 16 * 1024 * 1024) { send("Log line exceeds display limit."); pending.clear(); }
        }
        if (!pending.empty()) send(std::move(pending));
        CloseHandle(readPipe);
        WaitForSingleObject(process, INFINITE);
        DWORD exitCode = 1;
        GetExitCodeProcess(process, &exitCode);
        running_ = false;
        PostMessageW(window, WM_ENGINE_DONE, exitCode, 0);
    });
}
