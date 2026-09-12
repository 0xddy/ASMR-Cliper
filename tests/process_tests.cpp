#include "ProcessRunner.h"
#include <shellapi.h>
#include <iostream>

LRESULT CALLBACK TestProc(HWND window,UINT msg,WPARAM wp,LPARAM lp) {
    if(msg==WM_ENGINE_LINE) { delete reinterpret_cast<std::string*>(lp);return 0; }
    return DefWindowProcW(window,msg,wp,lp);
}

int wmain(int argc,wchar_t** argv) {
    const std::vector<std::wstring> cases={L"",L"simple",L"G:\\ASMR 音频\\test.m4a",L"trailing slash\\",L"quotes\"inside",L"& | $(touch) `literal`",L"\\\\\"quoted\\\\",L"line\nbreak"};
    for(const auto& item:cases) {
        auto command=L"test.exe "+QuoteWindowsArgument(item);
        int count=0;auto parsed=CommandLineToArgvW(command.c_str(),&count);
        bool good=parsed&&count==2&&std::wstring(parsed[1])==item;
        if(parsed) LocalFree(parsed);
        if(!good) { std::cerr<<"Argument round-trip failed\n";return 1; }
    }
    std::cout<<"All Unicode / space / quote / shell-metacharacter arguments passed.\n";
    if(argc>1) {
        WNDCLASSW wc{};wc.lpfnWndProc=TestProc;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=L"ASMRCLIP.ProcessTest";RegisterClassW(&wc);
        HWND window=CreateWindowW(wc.lpszClassName,L"",0,0,0,0,0,HWND_MESSAGE,nullptr,wc.hInstance,nullptr);
        ProcessRunner runner;
        auto log=std::filesystem::temp_directory_path()/(L"asmrclip-process-test-"+std::to_wstring(GetCurrentProcessId())+L".log");
        runner.start(window,argv[1],{L"-u",L"-c",L"import json,sys; reply=json.loads(sys.stdin.readline()); print(reply['language'],flush=True)"},std::filesystem::current_path(),log);
        runner.send("{\"type\":\"language_confirmed\",\"language\":\"ja\"}");
        bool received=false;auto inputBegin=GetTickCount64();
        while(!received&&GetTickCount64()-inputBegin<10000) {
            MSG msg{};
            while(PeekMessageW(&msg,nullptr,0,0,PM_REMOVE)) {
                if(msg.message==WM_ENGINE_LINE) {
                    auto line=reinterpret_cast<std::string*>(msg.lParam);received=*line=="ja"||*line=="ja\r";delete line;
                } else DispatchMessageW(&msg);
            }
            Sleep(10);
        }
        if(!received)runner.cancel();runner.finish();
        if(!received){DestroyWindow(window);std::filesystem::remove(log);std::cerr<<"Language reply did not reach the waiting process.\n";return 1;}
        std::cout<<"Language confirmation resumed the waiting process.\n";
        runner.start(window,argv[1],{L"-u",L"-c",L"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"},std::filesystem::current_path(),log);
        DWORD childId=0;
        const auto begin=GetTickCount64();
        while(!childId&&GetTickCount64()-begin<10000) {
            MSG msg{};
            while(PeekMessageW(&msg,nullptr,0,0,PM_REMOVE)) {
                if(msg.message==WM_ENGINE_LINE) {
                    auto line=reinterpret_cast<std::string*>(msg.lParam);
                    try { childId=std::stoul(*line); } catch(...) {}
                    delete line;
                } else DispatchMessageW(&msg);
            }
            Sleep(10);
        }
        HANDLE child=OpenProcess(SYNCHRONIZE,FALSE,childId);
        runner.cancel();runner.finish();
        bool terminated=child&&WaitForSingleObject(child,3000)==WAIT_OBJECT_0;
        if(child) CloseHandle(child);
        DestroyWindow(window);
        std::filesystem::remove(log);
        if(!terminated) { std::cerr<<"Cancellation failed to terminate a descendant.\n";return 1; }
        std::cout<<"Cancellation terminated Python and its descendant process.\n";
    }
    return 0;
}
