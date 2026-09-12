#include "MainWindow.h"
#include "UiControls.h"
#include "UiTheme.h"
#include <commctrl.h>
#include <shobjidl.h>
#include <shellapi.h>
#include <uxtheme.h>
#include <wrl/client.h>
#include <gdiplus.h>
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <memory>
#include <sstream>

using json = nlohmann::json;
namespace fs = std::filesystem;
using Microsoft::WRL::ComPtr;
namespace {
using namespace UiTheme;
constexpr UINT_PTR SettingsTimer=21;
struct NumericSetting {int id;const char* key;double min,max;const wchar_t* label;};
constexpr NumericSetting NumericSettings[]={
    {Before,"strict_pre",0,60,L"说话前余量"},{After,"strict_post",0,60,L"说话后余量"},
    {Minimum,"strict_min_section",1,600,L"最短片段"},{DenseGap,"strict_dense_gap",0,120,L"聊天合并间隔"},
    {Silence,"max_pause_seconds",.3,10,L"最长空窗期"},{SilenceDb,"silence_db",-90,-20,L"静音电平"},
    {FadeSeconds,"join_fade_seconds",.05,2,L"接缝淡化时长"},{EdgeFadeSeconds,"edge_fade_seconds",.05,3,L"首尾淡化时长"}};
const NumericSetting* NumericParameter(int id) {
    for(const auto& parameter:NumericSettings)if(parameter.id==id)return &parameter;
    return nullptr;
}
double NumericValue(const std::wstring& input,const NumericSetting& parameter) {
    try {
        size_t used=0;double value=std::stod(input,&used);
        if(used==input.size()&&std::isfinite(value)&&value>=parameter.min&&value<=parameter.max)return value;
    }catch(const std::exception&){}
    std::ostringstream range;range<<parameter.min<<" – "<<parameter.max;
    throw std::runtime_error(Utf8(parameter.label)+"须为 "+range.str()+"，未保存此项。");
}
bool IsSettingsInput(int id) {return NumericParameter(id)||id==ProxyUrl||id==Input||id==Output;}
bool IsSettingsChoice(int id) {return id==Language||id==Device||id==SpeechChoice||id==ReviewChoice||id==AudioEncoding||id==OutputKind;}
std::wstring Duration(double sec) {
    auto s=static_cast<int>(std::round(sec));
    std::wostringstream out;
    out<<s/3600<<L" 小时 "<<(s%3600)/60<<L" 分 "<<s%60<<L" 秒";
    return out.str();
}
std::wstring Number(double v) {
    std::wostringstream out; out<<v; return out.str();
}
void Rounded(HDC dc, RECT r, COLORREF fill, COLORREF border, int radius=16) {
    auto brush=CreateSolidBrush(fill); auto pen=CreatePen(PS_SOLID,1,border);
    auto oldB=SelectObject(dc,brush); auto oldP=SelectObject(dc,pen);
    RoundRect(dc,r.left,r.top,r.right,r.bottom,radius,radius);
    SelectObject(dc,oldB); SelectObject(dc,oldP); DeleteObject(brush); DeleteObject(pen);
}
void Clipboard(HWND owner, const std::wstring& content) {
    if (!OpenClipboard(owner)) return;
    auto memory=GlobalAlloc(GMEM_MOVEABLE,(content.size()+1)*sizeof(wchar_t));
    if (memory) {
        void* ptr=GlobalLock(memory);
        if (ptr) {
            memcpy(ptr,content.c_str(),(content.size()+1)*sizeof(wchar_t)); GlobalUnlock(memory);
            EmptyClipboard(); if (!SetClipboardData(CF_UNICODETEXT,memory)) GlobalFree(memory);
        } else GlobalFree(memory);
    }
    CloseClipboard();
}
struct PromptState { std::wstring content; HFONT font; HWND edit=nullptr; };
LRESULT CALLBACK PromptProc(HWND hwnd,UINT msg,WPARAM wp,LPARAM lp) {
    auto state=reinterpret_cast<PromptState*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
    if (msg==WM_NCCREATE) {
        state=static_cast<PromptState*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);
        SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(state));
    }
    if (!state) return DefWindowProcW(hwnd,msg,wp,lp);
    switch(msg) {
    case WM_CREATE: {
        state->edit=CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",state->content.c_str(),WS_CHILD|WS_VISIBLE|WS_VSCROLL|ES_MULTILINE|ES_READONLY|ES_AUTOVSCROLL,0,0,0,0,hwnd,reinterpret_cast<HMENU>(1),nullptr,nullptr);
        SendMessageW(state->edit,WM_SETFONT,reinterpret_cast<WPARAM>(state->font),TRUE);
        auto copy=CreateWindowW(L"BUTTON",L"复制提示词",WS_CHILD|WS_VISIBLE|WS_TABSTOP,0,0,0,0,hwnd,reinterpret_cast<HMENU>(2),nullptr,nullptr);
        auto close=CreateWindowW(L"BUTTON",L"关闭",WS_CHILD|WS_VISIBLE|WS_TABSTOP,0,0,0,0,hwnd,reinterpret_cast<HMENU>(3),nullptr,nullptr);
        SendMessageW(copy,WM_SETFONT,reinterpret_cast<WPARAM>(state->font),TRUE);
        SendMessageW(close,WM_SETFONT,reinterpret_cast<WPARAM>(state->font),TRUE);
        return 0;
    }
    case WM_SIZE: {
        int w=LOWORD(lp),h=HIWORD(lp),pad=MulDiv(16,GetDpiForWindow(hwnd),96),height=MulDiv(38,GetDpiForWindow(hwnd),96);
        MoveWindow(state->edit,pad,pad,w-2*pad,h-height-3*pad,TRUE);
        MoveWindow(GetDlgItem(hwnd,2),pad,h-height-pad,8*pad,height,TRUE);
        MoveWindow(GetDlgItem(hwnd,3),w-6*pad,h-height-pad,5*pad,height,TRUE);
        return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wp)==2) Clipboard(hwnd,state->content);
        if (LOWORD(wp)==3) DestroyWindow(hwnd);
        return 0;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    }
    return DefWindowProcW(hwnd,msg,wp,lp);
}
}

std::string Utf8(const std::wstring& value) {
    if (value.empty()) return {};
    int size=WideCharToMultiByte(CP_UTF8,0,value.data(),static_cast<int>(value.size()),nullptr,0,nullptr,nullptr);
    std::string result(size,0);
    WideCharToMultiByte(CP_UTF8,0,value.data(),static_cast<int>(value.size()),result.data(),size,nullptr,nullptr);
    return result;
}
std::wstring Wide(const std::string& value) {
    if (value.empty()) return {};
    int size=MultiByteToWideChar(CP_UTF8,0,value.data(),static_cast<int>(value.size()),nullptr,0);
    std::wstring result(size,0);
    MultiByteToWideChar(CP_UTF8,0,value.data(),static_cast<int>(value.size()),result.data(),size);
    return result;
}
json ReadJson(const fs::path& path) {
    std::ifstream stream(path,std::ios::binary);
    if (!stream) throw std::runtime_error("Cannot read configuration file.");
    return json::parse(stream);
}
void WriteJson(const fs::path& path,const json& value) {
    fs::create_directories(path.parent_path());
    auto temp=path; temp+=L".tmp";
    { std::ofstream file(temp,std::ios::binary); file<<value.dump(2); if (!file) throw std::runtime_error("Cannot save configuration."); }
    if (!MoveFileExW(temp.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)) throw std::runtime_error("Cannot commit configuration file.");
}

MainWindow::MainWindow(fs::path root,json options):root_(std::move(root)),options_(std::move(options)) {
    cfg_=ReadJson(root_/L"config/defaults.json");
    if (fs::exists(root_/L"config/user.json")) {
        try { cfg_.update(ReadJson(root_/L"config/user.json")); } catch (...) { notice_=true;status_=L"上次设置无法读取，已恢复默认设置。"; }
    }
    if (options_.contains("test-job")) cfg_.update(ReadJson(fs::path(Wide(options_["test-job"]))));
    if (options_.contains("test-config")) cfg_.update(ReadJson(fs::path(Wide(options_["test-config"]))));
    cfg_.erase("silence_seconds"); // Old detection threshold is not an output pause limit.
    cfg_["input"]=cfg_.value("input","");
    cfg_["output_dir"]=cfg_.value("output_dir",Utf8((root_/L"output").wstring()));
    white_=CreateSolidBrush(White); background_=CreateSolidBrush(Bg);
    try { environment_=ReadJson(root_/L"runtime/environment-status.json");for(auto& c:environment_["components"]) components_[c["id"]]=c; } catch(...) {}
    try { if(!testing()) {history_=ReadJson(root_/L"config/history.json");if(!history_.is_array())history_=json::array();} } catch(...) {}
    const std::vector<std::string> pages{"task","history","environment","settings","logs"};
    auto page=std::find(pages.begin(),pages.end(),options_.value("page","task"));page_=page==pages.end()?0:static_cast<int>(page-pages.begin());
    const auto setting=options_.value("settings-tab","");
    settingsTab_=setting=="network"?2:(setting=="recognition"||setting=="menu")?1:0;
    strictExpanded_=cfg_.value("mode","")=="strict";
    environmentTab_=options_.value("environment-tab","")=="base"?0:1;
}
MainWindow::~MainWindow() {
    runner_.cancel(); runner_.finish();
    for (auto font:{font_,titleFont_,boldFont_,smallFont_,brandFont_}) if(font) DeleteObject(font);
    DeleteObject(white_); DeleteObject(background_);
}
HWND MainWindow::control(int id) const { auto it=controls_.find(id); return it==controls_.end()?nullptr:it->second; }
std::wstring MainWindow::value(int id) const {
    int length=GetWindowTextLengthW(control(id)); std::wstring result(static_cast<size_t>(length)+1,0);
    GetWindowTextW(control(id),result.data(),length+1); result.resize(length); return result;
}
void MainWindow::text(int id,const std::wstring& content) { if(value(id)!=content)SetWindowTextW(control(id),content.c_str()); }

void MainWindow::invalidateFooter() {
    RECT footer{};GetClientRect(window_,&footer);
    footer.left=d(200);footer.top=std::max(0L,footer.bottom-d(80));
    InvalidateRect(window_,&footer,FALSE);
}

void MainWindow::beginTiming() {
    taskStarted_=GetTickCount64();taskElapsed_=0;timing_=true;hasTiming_=true;
    mediaReady_=false;activeOutput_.clear();
    taskProgress_=json::object();SetTimer(window_,20,1000,nullptr);
}
void MainWindow::stopTiming() {
    if(timing_)taskElapsed_=GetTickCount64()-taskStarted_;
    timing_=false;KillTimer(window_,20);
}
double MainWindow::elapsedSeconds() const {
    return static_cast<double>(timing_?GetTickCount64()-taskStarted_:taskElapsed_)/1000.;
}

int MainWindow::run(HINSTANCE instance,int show) {
    instance_=instance;
    INITCOMMONCONTROLSEX cc{sizeof(cc),ICC_PROGRESS_CLASS|ICC_STANDARD_CLASSES}; InitCommonControlsEx(&cc);
    WNDCLASSEXW wc{sizeof(wc)};
    wc.lpfnWndProc=WindowProc; wc.hInstance=instance; wc.lpszClassName=L"ASMRCLIP.MainWindow";
    wc.hCursor=LoadCursorW(nullptr,IDC_ARROW); wc.hIcon=LoadIconW(instance,MAKEINTRESOURCEW(101));wc.hIconSm=static_cast<HICON>(LoadImageW(instance,MAKEINTRESOURCEW(101),IMAGE_ICON,GetSystemMetrics(SM_CXSMICON),GetSystemMetrics(SM_CYSMICON),LR_DEFAULTCOLOR));
    RegisterClassExW(&wc);
    dpi_=GetDpiForSystem();
    window_=CreateWindowExW(WS_EX_ACCEPTFILES,wc.lpszClassName,L"ASMR-Cliper",WS_OVERLAPPEDWINDOW|WS_CLIPCHILDREN,
        CW_USEDEFAULT,CW_USEDEFAULT,d(1140),d(820),nullptr,nullptr,instance,this);
    if(!window_) throw std::runtime_error("Cannot create application window.");
    bool test=testing();
    ShowWindow(window_,test?SW_HIDE:show); UpdateWindow(window_);
    SetTimer(window_,1,test?500:900,nullptr);
    MSG msg{};
    while(GetMessageW(&msg,nullptr,0,0)>0) {
        if(!IsDialogMessageW(window_,&msg)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    }
    return static_cast<int>(msg.wParam);
}
LRESULT CALLBACK MainWindow::WindowProc(HWND hwnd,UINT msg,WPARAM wp,LPARAM lp) {
    auto app=reinterpret_cast<MainWindow*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
    if(msg==WM_NCCREATE) {
        app=static_cast<MainWindow*>(reinterpret_cast<CREATESTRUCTW*>(lp)->lpCreateParams);
        app->window_=hwnd; SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(app));
    }
    return app?app->message(msg,wp,lp):DefWindowProcW(hwnd,msg,wp,lp);
}



void MainWindow::setFonts() {
    for(auto font:{font_,titleFont_,boldFont_,smallFont_,brandFont_}) if(font) DeleteObject(font);
    auto make=[&](int size,int weight) { return CreateFontW(-d(size),0,0,0,weight,FALSE,FALSE,FALSE,DEFAULT_CHARSET,OUT_DEFAULT_PRECIS,CLIP_DEFAULT_PRECIS,CLEARTYPE_QUALITY,DEFAULT_PITCH,L"Microsoft YaHei UI"); };
    font_=make(15,FW_NORMAL);titleFont_=make(27,FW_BOLD);boldFont_=make(17,FW_BOLD);smallFont_=make(13,FW_NORMAL);
    brandFont_=make(18,FW_SEMIBOLD);
    for(auto [id,handle]:controls_) { (void)id; SendMessageW(handle,WM_SETFONT,reinterpret_cast<WPARAM>(font_),FALSE); }
}










void MainWindow::readSoundSettings() {
    for(auto [id,key]:SoundOptions)cfg_[key]=SendMessageW(control(id),BM_GETCHECK,0,0)==BST_CHECKED;
}
void MainWindow::readFadeSettings() {
    struct Fade {int toggle,field;const char* enabled;const char* seconds;};
    for(const auto& fade:std::vector<Fade>{{FadeEnabled,FadeSeconds,"join_fade_enabled","join_fade_seconds"},
                                         {EdgeFadeEnabled,EdgeFadeSeconds,"edge_fade_enabled","edge_fade_seconds"}}) {
        bool enabled=SendMessageW(control(fade.toggle),BM_GETCHECK,0,0)==BST_CHECKED;
        if(enabled) {
            cfg_[fade.seconds]=NumericValue(value(fade.field),*NumericParameter(fade.field));
        }
        cfg_[fade.enabled]=enabled;
    }
}
void MainWindow::readModelSettings() {
    const std::vector<std::string> models{"whisper-large-v3","qwen3-asr","whisper-turbo"};
    int speech=static_cast<int>(SendMessageW(control(SpeechChoice),CB_GETCURSEL,0,0));
    int review=static_cast<int>(SendMessageW(control(ReviewChoice),CB_GETCURSEL,0,0));
    cfg_["speech_model"]=models.at(std::clamp(speech,0,2));
    cfg_["review_model_id"]=models.at(std::clamp(review,0,1));
    const std::vector<std::string> paths{"models/whisper-review","models/qwen-asr","models/whisper-turbo"};
    cfg_["whisper_model"]=paths.at(std::clamp(speech,0,2));cfg_["review_model"]=paths.at(std::clamp(review,0,1));
}
void MainWindow::readRecognitionSettings() {
    readModelSettings();
    const std::vector<std::string> languages{"auto","ko","ja","zh","en"},devices{"auto","cuda","cpu"};
    auto lang=static_cast<int>(SendMessageW(control(Language),CB_GETCURSEL,0,0)),device=static_cast<int>(SendMessageW(control(Device),CB_GETCURSEL,0,0));
    cfg_["language"]=languages.at(std::clamp(lang,0,4)); cfg_["device"]=devices.at(std::clamp(device,0,2));
    cfg_["review_enabled"]=cfg_.value("mode","")=="extract"||SendMessageW(control(Audit),BM_GETCHECK,0,0)==BST_CHECKED;
    const std::vector<std::string> codecs{"source","flac","pcm","aac"};
    cfg_["audio_output_codec"]=codecs.at(std::clamp(static_cast<int>(SendMessageW(control(AudioEncoding),CB_GETCURSEL,0,0)),0,3));
    cfg_["generate_program_menu"]=SendMessageW(control(MenuEnabled),BM_GETCHECK,0,0)==BST_CHECKED;
}
void MainWindow::readEditingSettings() {
    for(int id:{Before,After,Minimum,DenseGap,Silence,SilenceDb}) {
        const auto& parameter=*NumericParameter(id);cfg_[parameter.key]=NumericValue(value(id),parameter);
    }
    readSoundSettings();
    readFadeSettings();
}
void MainWindow::readNetworkSettings() {
    cfg_["proxy_enabled"]=SendMessageW(control(ProxyEnabled),BM_GETCHECK,0,0)==BST_CHECKED;
    cfg_["proxy_url"]=Utf8(value(ProxyUrl));
}
void MainWindow::readOutputSettings() {
    int selected=std::clamp(static_cast<int>(SendMessageW(control(OutputKind),CB_GETCURSEL,0,0)),0,3);
    const std::vector<std::string> outputs{"auto","audio","video","video"};
    cfg_["output_kind"]=outputs.at(selected);cfg_["video_cut_mode"]=selected==3?"precise":"copy";
}
void MainWindow::readSettings() {
    const auto previous=cfg_;
    try {
        readOutputSettings();
        auto resolve=[&](std::wstring input) {
            if(input.size()>=2&&input.front()==L'\"'&&input.back()==L'\"')input=input.substr(1,input.size()-2);
            if(input.empty())return std::string{};
            fs::path path(input);if(path.is_relative())path=root_/path;
            return Utf8(path.lexically_normal().wstring());
        };
        cfg_["input"]=resolve(value(Input));cfg_["output_dir"]=resolve(value(Output));
        readEditingSettings();readRecognitionSettings();readNetworkSettings();
    } catch(...) {cfg_=previous;throw;}
}
void MainWindow::saveSettings() {
    if(testing()) {
        if(options_.contains("test-autosave")) {auto path=fs::path(Wide(options_["test-autosave"]));path.replace_extension(L".settings.json");WriteJson(path,cfg_);}
        return;
    }
    WriteJson(root_/L"config/user.json",cfg_);
}
void MainWindow::autoSaveSetting(int id,bool reportInvalid) {
    if(!settingsReady_||populatingSettings_||busy_)return;
    pendingSettings_.erase(id);
    const auto previous=cfg_;bool validated=false;
    try {
        auto checked=[&](int controlId){return SendMessageW(control(controlId),BM_GETCHECK,0,0)==BST_CHECKED;};
        auto choice=[&](const char* key,const std::vector<std::string>& choices) {
            int selected=static_cast<int>(SendMessageW(control(id),CB_GETCURSEL,0,0));
            cfg_[key]=choices.at(std::clamp(selected,0,static_cast<int>(choices.size())-1));
        };
        if(const auto parameter=NumericParameter(id))cfg_[parameter->key]=NumericValue(value(id),*parameter);
        else if(IsSoundOption(id)){for(auto [controlId,key]:SoundOptions)if(controlId==id)cfg_[key]=checked(id);}
        else if(id==FadeEnabled)cfg_["join_fade_enabled"]=checked(id);
        else if(id==EdgeFadeEnabled)cfg_["edge_fade_enabled"]=checked(id);
        else if(id==MenuEnabled)cfg_["generate_program_menu"]=checked(id);
        else if(id==Audit)cfg_["review_enabled"]=cfg_.value("mode","")=="extract"||checked(id);
        else if(id==ProxyEnabled)cfg_["proxy_enabled"]=checked(id);
        else if(id==ProxyUrl)cfg_["proxy_url"]=Utf8(value(id));
        else if(id==Input||id==Output)cfg_[id==Input?"input":"output_dir"]=Utf8(value(id));
        else if(id==Language)choice("language",{"auto","ko","ja","zh","en"});
        else if(id==Device)choice("device",{"auto","cuda","cpu"});
        else if(id==AudioEncoding)choice("audio_output_codec",{"source","flac","pcm","aac"});
        else if(id==OutputKind)readOutputSettings();
        else if(id==SpeechChoice||id==ReviewChoice)readModelSettings();
        else if(id==Strict||id==Relaxed||id==Extract) {
            cfg_["mode"]=id==Strict?"strict":id==Extract?"extract":"relaxed";
            if(id==Extract)cfg_["review_enabled"]=true;
        } else return;
        validated=true;
        if(cfg_!=previous)saveSettings();
        if(saveErrorControl_==id) {
            if(status_==saveError_) {notice_=false;status_.clear();}
            saveErrorControl_=0;saveError_.clear();InvalidateRect(window_,nullptr,FALSE);
        }
    }catch(const std::exception& error) {
        cfg_=previous;
        if(validated)pendingSettings_.insert(id); // Retry an I/O failure on the next flush.
        if(reportInvalid||validated) {
            saveErrorControl_=id;saveError_=L"设置未保存："+Wide(error.what());notice_=true;status_=saveError_;
            appendLog(saveError_);InvalidateRect(window_,nullptr,FALSE);
        }
    }
}
void MainWindow::flushPendingSettings(bool reportInvalid) {
    KillTimer(window_,SettingsTimer);
    auto pending=std::move(pendingSettings_);pendingSettings_.clear();
    for(int id:pending)autoSaveSetting(id,reportInvalid);
}
void MainWindow::appendLog(const std::wstring& content) {
    if(GetWindowTextLengthW(control(Log))>120000) text(Log,L"较早日志已收起，完整日志保存在 runtime/logs。\r\n");
    SendMessageW(control(Log),EM_SETSEL,static_cast<WPARAM>(-1),static_cast<LPARAM>(-1));
    auto line=content+L"\r\n";
    SendMessageW(control(Log),EM_REPLACESEL,FALSE,reinterpret_cast<LPARAM>(line.c_str()));
}
void MainWindow::chooseFile(bool folder) {
    ComPtr<IFileOpenDialog> dialog;
    if(FAILED(CoCreateInstance(CLSID_FileOpenDialog,nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&dialog)))) return;
    DWORD flags=0;dialog->GetOptions(&flags);
    dialog->SetOptions(flags|FOS_FORCEFILESYSTEM|(folder?FOS_PICKFOLDERS:FOS_FILEMUSTEXIST));
    dialog->SetTitle(folder?L"选择剪辑结果保存目录":L"选择音频或视频");
    if(!folder) { COMDLG_FILTERSPEC filter[]={
        {L"音频与视频",L"*.m4a;*.mp4;*.mov;*.mkv;*.webm;*.avi;*.flv;*.ts;*.m2ts;*.mp3;*.flac;*.wav;*.ogg;*.opus;*.mka"},
        {L"视频文件",L"*.mp4;*.mov;*.mkv;*.webm;*.avi;*.flv;*.ts;*.m2ts"},
        {L"音频文件",L"*.m4a;*.mp3;*.flac;*.wav;*.ogg;*.opus;*.mka"},{L"所有文件",L"*.*"}};dialog->SetFileTypes(4,filter); }
    if(SUCCEEDED(dialog->Show(window_))) {
        ComPtr<IShellItem> item;
        PWSTR path=nullptr;
        if(SUCCEEDED(dialog->GetResult(&item))&&SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH,&path))) { text(folder?Output:Input,path);CoTaskMemFree(path); }
    }
}
void MainWindow::start(bool doctor) {
    if(doctor) {environmentTask("inspect");return;}
    if(busy_) return;
    try {
        readSettings();
        if(!fs::is_regular_file(fs::path(Wide(cfg_["input"])))||value(Output).empty()) throw std::runtime_error("请选择存在的音频或视频文件和输出目录。");
        auto python=environment_.contains("python")?fs::path(Wide(environment_["python"])):root_/L"runtime/venv/Scripts/python.exe";
        if(!fs::exists(python)) python=root_/L"runtime/python/python.exe";
        if(!fs::exists(python)||!environmentReady()) {selectPage(2);throw std::runtime_error("请在运行环境页面点击「补齐环境」。");}
        saveSettings();
        fs::create_directories(root_/L"runtime/jobs");fs::create_directories(root_/L"runtime/logs");
        std::wstring token=std::to_wstring(GetCurrentProcessId())+L"_"+std::to_wstring(GetTickCount64());
        auto job=root_/L"runtime/jobs"/(token+L".json");
        auto jobSettings=cfg_;jobSettings["confirm_detected_language"]=!testing();WriteJson(job,jobSettings);
        cancelled_=false;completed_=false;checking_=false;eventCount_=0;activeAction_="run";downloadStatus_.clear();notice_=true;
        status_=L"正在准备本地音频分析…";SendMessageW(control(Progress),PBM_SETPOS,0,0);
        appendLog(L"开始剪辑 · "+Wide(cfg_.value("mode","relaxed")=="strict"?"严格模式 V2":cfg_.value("mode","")=="extract"?"提取模式 V4":"宽松模式 V3"));
        enableControls(true);beginTiming();
        runner_.start(window_,python,{L"-X",L"utf8",L"-u",(root_/L"engine/main.py").wstring(),L"run",L"--config",job.wstring()},root_,root_/L"runtime/logs"/(token+L".log"));
    } catch(const std::exception& e) {
        stopTiming();enableControls(false);notice_=true;status_=Wide(e.what());appendLog(L"无法开始："+status_);InvalidateRect(window_,nullptr,FALSE);
        if(testing()) finishTest(1);
    }
}

void MainWindow::environmentTask(const std::string& action,const std::string& component) {
    if(busy_) return;
    try {
        // Environment operations do not depend on valid audio form fields.
        cfg_["proxy_enabled"]=SendMessageW(control(ProxyEnabled),BM_GETCHECK,0,0)==BST_CHECKED;
        cfg_["proxy_url"]=Utf8(value(ProxyUrl));saveSettings();
        fs::create_directories(root_/L"runtime/jobs");fs::create_directories(root_/L"runtime/logs");
        auto token=L"environment_"+std::to_wstring(GetCurrentProcessId())+L"_"+std::to_wstring(GetTickCount64());
        auto job=root_/L"runtime/jobs"/(token+L".json");WriteJson(job,cfg_);
        wchar_t system[MAX_PATH]{};GetSystemDirectoryW(system,MAX_PATH);
        auto powershell=fs::path(system)/L"WindowsPowerShell/v1.0/powershell.exe";
        activeAction_=action;cancelled_=false;completed_=false;checking_=true;eventCount_=0;downloadStatus_.clear();notice_=true;
        status_=action=="inspect"?L"正在检测运行环境…":action=="install"?L"正在补齐运行环境…":L"正在测试下载连接…";
        if(action=="testproxy"){proxyTested_=true;proxyResults_=json::array();proxyStatus_=L"正在测试连接…";}
        SendMessageW(control(Progress),PBM_SETPOS,0,0);appendLog(status_);enableControls(true);beginTiming();
        runner_.start(window_,powershell,{L"-NoProfile",L"-NonInteractive",L"-ExecutionPolicy",L"Bypass",L"-File",(root_/L"scripts/environment.ps1").wstring(),L"-Action",Wide(action),L"-Config",job.wstring(),L"-Component",Wide(component)},root_,root_/L"runtime/logs"/(token+L".log"));
    } catch(const std::exception& e) {
        stopTiming();enableControls(false);notice_=true;status_=Wide(e.what());appendLog(status_);InvalidateRect(window_,nullptr,FALSE);if(testing())finishTest(1);
    }
}

void MainWindow::storeResult() {
    const auto output=Wide(lastResult_.value("output",""));
    auto found=std::find_if(history_.begin(),history_.end(),[&](const auto& row){return _wcsicmp(Wide(row.value("output","")).c_str(),output.c_str())==0;});
    if(found!=history_.end())*found=lastResult_;else history_.insert(history_.begin(),lastResult_);
    if(history_.size()>100)history_.erase(history_.begin()+100,history_.end());
    if(!testing()) {try {WriteJson(root_/L"config/history.json",history_);}catch(const std::exception& e){appendLog(L"记录保存失败："+Wide(e.what()));}}
    updateHistory();
}

void MainWindow::programMenuTask() {
    if(busy_||!lastResult_.contains("output"))return;
    try {
        const auto output=lastResult_["output"].get<std::string>();
        if(!fs::is_regular_file(fs::path(Wide(output))))throw std::runtime_error("成片文件不存在。");
        auto python=environment_.contains("python")?fs::path(Wide(environment_["python"])):root_/L"runtime/venv/Scripts/python.exe";
        if(!fs::exists(python))python=root_/L"runtime/python/python.exe";
        if(!fs::exists(python)){selectPage(2);throw std::runtime_error("请先在运行环境中安装 Python。");}
        fs::create_directories(root_/L"runtime/jobs");fs::create_directories(root_/L"runtime/logs");
        const auto token=L"menu_"+std::to_wstring(GetCurrentProcessId())+L"_"+std::to_wstring(GetTickCount64());
        auto job=root_/L"runtime/jobs"/(token+L".json");auto data=cfg_;data["input"]=output;WriteJson(job,data);
        activeAction_="menu";cancelled_=completed_=checking_=false;downloadStatus_.clear();notice_=true;
        beginTiming();activeOutput_=output;status_=L"正在识别成片中的 ASMR 项目…";
        enableControls(true);appendLog(status_);
        runner_.start(window_,python,{L"-X",L"utf8",L"-u",(root_/L"engine/main.py").wstring(),L"menu",L"--config",job.wstring()},root_,root_/L"runtime/logs"/(token+L".log"));
    } catch(const std::exception& e) {
        stopTiming();enableControls(false);notice_=true;status_=L"节目单未生成："+Wide(e.what());appendLog(status_);InvalidateRect(window_,nullptr,FALSE);
    }
}

std::string MainWindow::chooseLanguage(const json& data) {
    const auto choices=data.value("choices",std::vector<std::string>{"ko","ja","zh","en"});
    const std::map<std::string,std::wstring> names{{"ko",L"韩语"},{"ja",L"日语"},{"zh",L"中文"},{"en",L"英语"}};
    const auto detected=data.value("detected","");
    auto name=[&](const std::string& code){auto found=names.find(code);return found!=names.end()?found->second:Wide(code);};
    std::vector<std::wstring> labels;std::vector<TASKDIALOG_BUTTON> radios;
    for(const auto& code:choices)labels.push_back(name(code)+(code==detected?L"（识别结果）":L""));
    int selected=0;
    for(size_t i=0;i<labels.size();++i) {
        radios.push_back({100+static_cast<int>(i),labels[i].c_str()});
        if(choices[i]==detected)selected=radios.back().nButtonID;
    }
    std::wstring content=selected?L"识别为 "+name(detected)+L"。确认或修改后继续当前任务。":L"未能确定语言，请选择后继续当前任务。";
    TASKDIALOG_BUTTON buttons[]={{IDOK,L"确认并继续"},{IDCANCEL,L"取消任务"}};
    struct State {MainWindow* app;int selected;const std::vector<std::string>* choices;} state{this,selected,&choices};
    TASKDIALOGCONFIG dialog{sizeof(dialog)};dialog.hwndParent=window_;dialog.hInstance=instance_;
    dialog.dwFlags=TDF_ALLOW_DIALOG_CANCELLATION|TDF_POSITION_RELATIVE_TO_WINDOW|TDF_SIZE_TO_CONTENT;
    dialog.pszWindowTitle=L"确认录音语言";dialog.pszMainInstruction=L"录音中的说话声是什么语言？";
    dialog.pszContent=content.c_str();dialog.pszFooter=L"任务已暂停，确认后从当前步骤继续。";
    dialog.cButtons=2;dialog.pButtons=buttons;dialog.nDefaultButton=IDOK;
    dialog.cRadioButtons=static_cast<UINT>(radios.size());dialog.pRadioButtons=radios.data();dialog.nDefaultRadioButton=selected;
    if(!selected)dialog.dwFlags|=TDF_NO_DEFAULT_RADIO_BUTTON;
    dialog.lpCallbackData=reinterpret_cast<LONG_PTR>(&state);
    dialog.pfCallback=[](HWND hwnd,UINT notification,WPARAM,LPARAM,LONG_PTR value)->HRESULT {
        auto& s=*reinterpret_cast<State*>(value);
        if(notification==TDN_CREATED) {
            s.app->languageDialog_=hwnd;
            SendMessageW(hwnd,TDM_ENABLE_BUTTON,IDOK,s.selected!=0);
            if(s.app->testing()&&s.app->options_.contains("test-language-choice")) {
                const auto choice=s.app->options_["test-language-choice"].get<std::string>();
                auto it=std::find(s.choices->begin(),s.choices->end(),choice);
                if(it!=s.choices->end())PostMessageW(hwnd,TDM_CLICK_RADIO_BUTTON,100+std::distance(s.choices->begin(),it),0);
                PostMessageW(hwnd,TDM_CLICK_BUTTON,it!=s.choices->end()?IDOK:IDCANCEL,0);
            }
        } else if(notification==TDN_RADIO_BUTTON_CLICKED)SendMessageW(hwnd,TDM_ENABLE_BUTTON,IDOK,TRUE);
        else if(notification==TDN_DESTROYED)s.app->languageDialog_=nullptr;
        return S_OK;
    };
    int button=IDCANCEL,radio=0;
    if(FAILED(TaskDialogIndirect(&dialog,&button,&radio,nullptr)))throw std::runtime_error("无法显示语言确认窗口。");
    return button==IDOK&&radio>=100&&radio<100+static_cast<int>(choices.size())?choices[radio-100]:"";
}

void MainWindow::confirmLanguage(const json& data) {
    if(cancelled_||completed_||!runner_.running())return;
    if(!taskProgress_.empty())taskProgress_["detail"]="等待确认语言，可在弹窗中修改后继续";
    InvalidateRect(window_,nullptr,FALSE);
    try {
        const auto chosen=chooseLanguage(data);
        if(!runner_.running())return;
        if(chosen.empty()) {
            cancelled_=true;status_=L"正在取消任务…";EnableWindow(control(Cancel),FALSE);runner_.cancel();
        } else {
            runner_.send(json{{"type","language_confirmed"},{"language",chosen}}.dump());
            appendLog(L"已确认本次任务语言："+Wide(chosen));
        }
    } catch(const std::exception& e) {
        status_=L"处理失败："+Wide(e.what());notice_=true;stopTiming();appendLog(status_);runner_.cancel();
    }
    InvalidateRect(window_,nullptr,FALSE);
}

void MainWindow::receive(const std::string& line) {
    auto data=json::parse(line,nullptr,false);
    if(data.is_discarded()||!data.is_object()) {if(!line.empty())appendLog(Wide(line));return;}
    const auto previousStatus=status_;
    ++eventCount_;auto type=data.value("type","");std::wstring msg=Wide(data.value("message",""));
    if(!msg.empty())appendLog(msg);
    if((activeAction_=="run"||activeAction_=="menu")&&data.contains("task_progress")&&timing_&&!completed_&&!cancelled_) {
        const auto previous=taskProgress_;
        taskProgress_=data["task_progress"];
        status_=L"阶段 "+std::to_wstring(taskProgress_.value("stage",1))+L"/"+std::to_wstring(taskProgress_.value("stages",6))+L" · "+Wide(taskProgress_.value("title","处理中"));
        const int round=taskProgress_.value("round",0);
        if(round>0)status_+=L" · 第 "+std::to_wstring(round)+L" 轮 / 最多 "+std::to_wstring(taskProgress_.value("round_limit",round))+L" 轮 · 通过即结束";
        if(previous.value("stage",0)!=taskProgress_.value("stage",0)||previous.value("round",0)!=round)appendLog(status_);
        double percent=taskProgress_.value("percent",json()).is_number()?taskProgress_["percent"].get<double>():0.;
        SendMessageW(control(Progress),PBM_SETPOS,static_cast<WPARAM>(std::clamp(percent,0.,100.)*10),0);
    } else if((activeAction_!="run"&&activeAction_!="menu")||taskProgress_.empty()) {
        if(data.contains("progress")&&data["progress"].is_number())SendMessageW(control(Progress),PBM_SETPOS,static_cast<WPARAM>(std::clamp(data["progress"].get<double>(),0.,100.)*10),0);
        if(!msg.empty()&&type!="log")status_=msg;
    }
    if(type=="language_confirmation") {confirmLanguage(data);return;}
    if(type=="component") {
        components_[data.value("id","")]=data;
    } else if(type=="environment") {
        environment_=data;
        if(data.contains("components")) for(auto& c:data["components"]) components_[c["id"]]=c;
        if(activeAction_=="inspect") completed_=true;
    } else if(type=="setup_complete") completed_=true;
    else if(type=="download") {
        double bytes=data.value("downloaded",data.value("bytes",0.)),total=data.value("total",0.),speed=data.value("bytes_per_second",data.value("speed",0.));
        std::wostringstream out;out<<std::fixed<<std::setprecision(1)<<bytes/1048576<<L" / "<<total/1048576<<L" MB";
        if(speed>0) out<<L"   ·   "<<speed/1048576<<L" MB/s";downloadStatus_=out.str();
    } else if(type=="proxy_result") {
        completed_=true;proxyStatus_=data.value("ok",false)?L"连接测试通过":L"部分连接失败";
        const auto& rows=data.contains("results")?data["results"]:data.value("checks",json::array());proxyResults_=rows;
        for(const auto& row:rows) {
            if(!row.value("ok",false)) appendLog(Wide(row.value("detail",row.value("error",""))));
        }
    } else if(type=="menu_complete") {
        stopTiming();completed_=true;EnableWindow(control(Cancel),FALSE);
        auto menu=data.value("program_menu",json::object());
        status_=menu.value("status","")=="ready"?L"节目单已生成 · "+std::to_wstring(menu.value("chapters",json::array()).size())+L" 项":L"成片已保留，节目单未生成："+Wide(menu.value("note","请查看日志"));
        for(const auto& row:history_)if(row.value("output","")==activeOutput_||_wcsicmp(Wide(row.value("output","")).c_str(),Wide(data.value("output","")).c_str())==0) {
            lastResult_=row;lastResult_["program_menu"]=menu;storeResult();break;
        }
    } else if(type=="complete"||type=="media_ready") {
        const bool final=type=="complete";
        if(final)stopTiming();
        lastResult_=data;completed_=final;mediaReady_=true;activeOutput_=data.value("output","");EnableWindow(control(Cancel),!final);
        lastResult_["elapsed_seconds"]=elapsedSeconds();
        status_=L"完成 · "+Duration(data.value("duration",0.))+L" · "+std::to_wstring(data.value("segments",0))+L" 段";
        auto review=data.value("speech_review",json::object());
        if(review.value("status","")=="needs_review")status_+=L" · "+std::to_wstring(review.value("findings",json::array()).size())+L" 处待复听";
        appendLog(L"结果："+Wide(data.value("output","")));
        if(data.value("join_review_count",0)>0) appendLog(L"有接缝建议复听，具体位置见校验报告。");
        SYSTEMTIME now{};GetLocalTime(&now);wchar_t stamp[32]{};swprintf_s(stamp,L"%04u-%02u-%02u  %02u:%02u",now.wYear,now.wMonth,now.wDay,now.wHour,now.wMinute);
        lastResult_["finished_at"]=Utf8(stamp);lastResult_["mode"]=cfg_.value("mode","relaxed");
        storeResult();if(!testing())selectPage(1);
        if(final&&data.value("program_menu",json::object()).value("status","")=="ready")status_+=L" · 节目单已生成";
        else if(final&&(data.value("program_menu",json::object()).value("status","")=="unavailable"||data.value("program_menu",json::object()).value("status","")=="failed"))status_+=L" · 节目单未生成";
    }
    if(type=="error"){stopTiming();notice_=true;status_=std::wstring(mediaReady_||activeAction_=="menu"?L"成片已保留，节目单未生成：":L"处理失败：")+msg;if(activeAction_=="testproxy")proxyStatus_=msg;}
    if(((type=="component"||type=="environment")&&page_==2)||(type=="proxy_result"&&page_==3&&settingsTab_==2))layout();
    if(status_!=previousStatus||data.contains("task_progress")||data.contains("progress")||type=="download"||type=="error")invalidateFooter();
}

void MainWindow::prompt() {
    const bool strict=cfg_.value("mode","relaxed")=="strict";
    const bool extract=cfg_.value("mode","")=="extract";
    auto path=root_/L"docs/prompts"/(strict?L"strict-v2.txt":extract?L"extract-v4.txt":L"relaxed-v3.txt");
    std::ifstream file(path,std::ios::binary);
    std::string raw((std::istreambuf_iterator<char>(file)),std::istreambuf_iterator<char>());
    std::wstring content=Wide(raw);
    if(strict) content+=L"\n\n当前界面参数：前余量 "+value(Before)+L" 秒；后余量 "+value(After)+L" 秒；最短连续片段 "+value(Minimum)+L" 秒；聊天合并间隔 "+value(DenseGap)+L" 秒。";
    content+=L"\n最长空窗期："+value(Silence)+L" 秒（按成片连续静音计，包括跨片段接缝）。";
    content+=L"\n\n当前保留声音（说话声始终删除）：";
    bool any=false;
    for(auto [id,key]:SoundOptions)if(SendMessageW(control(id),BM_GETCHECK,0,0)==BST_CHECKED){if(any)content+=L"、";content+=value(id);any=true;}
    if(!any)content+=L"无";
    content+=L"。当前选择优先于默认设置；与说话重叠或无法自然衔接的片段仍可能被一起剪掉。";
    content+=L"\n成片大模型复核："+std::wstring(extract||SendMessageW(control(Audit),BM_GETCHECK,0,0)==BST_CHECKED?L"开启":L"关闭")+L"。";
    content+=L"\n接缝淡化："+std::wstring(SendMessageW(control(FadeEnabled),BM_GETCHECK,0,0)==BST_CHECKED?L"开启，音轨重新编码；每侧最多 "+value(FadeSeconds)+L" 秒。":L"关闭，原音频包直接复制。");
    showText(strict?L"严格模式（V2）提示词":extract?L"提取模式（V4）提示词":L"宽松模式（V3）提示词",content);
}

std::wstring MainWindow::reviewFindingsText() const {
    auto findings=lastResult_.value("speech_review",json::object()).value("findings",json::array());
    std::wstring content=L"已完成 · "+std::to_wstring(findings.size())+L" 处待复听\n时间对应剪辑后的成片。\n\n";
    auto stamp=[](double seconds) {
        auto ms=static_cast<ULONGLONG>(std::max(0.,seconds)*1000+.5);wchar_t text[48]{};
        swprintf_s(text,L"%02llu:%02llu:%02llu.%03llu",ms/3600000,(ms/60000)%60,(ms/1000)%60,ms%1000);return std::wstring(text);
    };
    for(const auto& row:findings) {
        content+=stamp(row.value("start",0.))+L" — "+stamp(row.value("end",0.))+L"\n"+Wide(row.value("text",""))+L"\n\n";
    }
    return content;
}

std::wstring MainWindow::programMenuText() const {
    auto menu=lastResult_.value("program_menu",json::object());
    std::wstring content=L"成片节目单\n时间对应最终成片 · 本地 CLAP 声音语义识别\n\n";
    auto stamp=[](double seconds){int s=static_cast<int>(std::max(0.,seconds));wchar_t text[32]{};swprintf_s(text,L"%02d:%02d:%02d",s/3600,(s/60)%60,s%60);return std::wstring(text);};
    for(const auto& row:menu.value("chapters",json::array())) {
        content+=stamp(row.value("start",0.))+L" — "+stamp(row.value("end",0.))+L"   "+Wide(row.value("title","待确认"));
        if(!row.value("review_findings",json::array()).empty())content+=L" · 含待复听位置";
        content+=L"\n";
    }
    content+=L"\n相似声音可能混淆，项目切换时间约为 5 秒精度；待确认表示证据不足。";
    return content;
}

void MainWindow::showText(const std::wstring& title,const std::wstring& content) {
    std::wstring crlf;
    for(auto c:content) { if(c==L'\n') crlf.push_back(L'\r');crlf.push_back(c); }
    PromptState state{crlf,font_};
    WNDCLASSW wc{};wc.lpfnWndProc=PromptProc;wc.hInstance=instance_;wc.lpszClassName=L"ASMRCLIP.Prompt";wc.hCursor=LoadCursorW(nullptr,IDC_ARROW);wc.hbrBackground=reinterpret_cast<HBRUSH>(COLOR_WINDOW+1);RegisterClassW(&wc);
    auto popup=CreateWindowExW(WS_EX_DLGMODALFRAME,wc.lpszClassName,title.c_str(),WS_OVERLAPPEDWINDOW|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,d(790),d(710),window_,nullptr,instance_,&state);
    if(!popup){appendLog(L"无法打开详情窗口。");return;}
    SetWindowTextW(GetDlgItem(popup,2),L"复制内容");
    if(options_.contains("test-menu")) {
        RECT owner{};GetWindowRect(window_,&owner);
        SetWindowPos(popup,nullptr,owner.left+d(220),owner.top+d(24),0,0,SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE);
        screenshot(fs::path(Wide(options_["test-menu"])).parent_path()/L"program-menu-details.png",popup);
        PostMessageW(popup,WM_CLOSE,0,0);
    }
    EnableWindow(window_,FALSE);
    MSG message{};
    while(IsWindow(popup)&&GetMessageW(&message,nullptr,0,0)>0) {
        if(!IsDialogMessageW(popup,&message)) { TranslateMessage(&message);DispatchMessageW(&message); }
    }
    EnableWindow(window_,TRUE);SetForegroundWindow(window_);
}
void MainWindow::screenshot(const fs::path& path,HWND popup) {
    RECT rect{};GetClientRect(window_,&rect);
    HDC dc=GetDC(window_),memory=CreateCompatibleDC(dc);
    HBITMAP bitmap=CreateCompatibleBitmap(dc,rect.right,rect.bottom);
    auto old=SelectObject(memory,bitmap);
    // Render this application's own client and controls. This also works for
    // hidden smoke-test windows without relying on the desktop compositor.
    paint(memory);
    for(auto [id,child]:controls_) {
        if(!(GetWindowLongPtrW(child,GWL_STYLE)&WS_VISIBLE))continue;
        RECT r{};GetWindowRect(child,&r);
        MapWindowPoints(nullptr,window_,reinterpret_cast<POINT*>(&r),2);
        int saved=SaveDC(memory);
        SetViewportOrgEx(memory,r.left,r.top,nullptr);
        IntersectClipRect(memory,0,0,r.right-r.left,r.bottom-r.top);
        SendMessageW(child,WM_PRINT,reinterpret_cast<WPARAM>(memory),PRF_CLIENT|PRF_NONCLIENT|PRF_CHILDREN|PRF_ERASEBKGND);
        RestoreDC(memory,saved);
    }
    if(popup) {
        RECT r{};GetWindowRect(popup,&r);MapWindowPoints(nullptr,window_,reinterpret_cast<POINT*>(&r),2);
        int saved=SaveDC(memory);SetViewportOrgEx(memory,r.left,r.top,nullptr);
        PrintWindow(popup,memory,0);RestoreDC(memory,saved);
    }
    SelectObject(memory,old);
    UINT count=0,size=0;Gdiplus::GetImageEncodersSize(&count,&size);
    std::vector<BYTE> encoders(size);auto info=reinterpret_cast<Gdiplus::ImageCodecInfo*>(encoders.data());Gdiplus::GetImageEncoders(count,size,info);
    fs::create_directories(path.parent_path());
    for(UINT i=0;i<count;++i) if(wcscmp(info[i].MimeType,L"image/png")==0) { Gdiplus::Bitmap image(bitmap,nullptr);image.Save(path.c_str(),&info[i].Clsid,nullptr);break; }
    DeleteObject(bitmap);DeleteDC(memory);ReleaseDC(window_,dc);
}
void MainWindow::finishTest(DWORD code) {
    if(options_.contains("snapshot")) screenshot(fs::path(Wide(options_["snapshot"])));
    if(options_.contains("self-test")) screenshot(fs::path(Wide(options_["self-test"])));
    if(options_.contains("test-report")) WriteJson(fs::path(Wide(options_["test-report"])),{{"exit_code",code},{"completed",completed_},{"events",eventCount_},{"result",lastResult_},{"controls",controls_.size()},{"page",page_},{"environment",environment_},{"proxy_result",Utf8(proxyStatus_)},
        {"language_selection",SendMessageW(control(Language),CB_GETCURSEL,0,0)},{"device_selection",SendMessageW(control(Device),CB_GETCURSEL,0,0)}});
    testExit_=static_cast<int>(code);DestroyWindow(window_);
}
LRESULT MainWindow::message(UINT msg,WPARAM wp,LPARAM lp) {
    switch(msg) {
    case WM_CREATE: dpi_=GetDpiForWindow(window_);createControls();layout();return 0;
    case WM_SIZE: if(wp!=SIZE_MINIMIZED&&!controls_.empty()) layout();return 0;
    case WM_DPICHANGED: {
        dpi_=HIWORD(wp);setFonts();auto r=reinterpret_cast<RECT*>(lp);
        SetWindowPos(window_,nullptr,r->left,r->top,r->right-r->left,r->bottom-r->top,SWP_NOZORDER|SWP_NOACTIVATE);
        layout();RedrawWindow(window_,nullptr,nullptr,RDW_INVALIDATE|RDW_FRAME|RDW_ALLCHILDREN);return 0;
    }
    case WM_GETMINMAXINFO: {
        auto info=reinterpret_cast<MINMAXINFO*>(lp);info->ptMinTrackSize={d(1100),d(800)};return 0;
    }
    case WM_UPDATEUISTATE: wp=MAKEWPARAM(UIS_SET,UISF_HIDEFOCUS);break;
    case WM_ERASEBKGND: return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps{};HDC dc=BeginPaint(window_,&ps);
        if(!IsRectEmpty(&ps.rcPaint)) {BufferedSurface surface(dc,ps.rcPaint);paint(surface.dc());}
        EndPaint(window_,&ps);return 0;
    }
    case WM_PRINTCLIENT: paint(reinterpret_cast<HDC>(wp));return 0;
    case WM_SETCURSOR:
        if(LOWORD(lp)==HTCLIENT) {
            POINT point{};GetCursorPos(&point);ScreenToClient(window_,&point);
            if(inputAt(point)) {SetCursor(LoadCursorW(nullptr,IDC_IBEAM));return TRUE;}
        }
        break;
    case WM_LBUTTONDOWN: {
        POINT point{static_cast<short>(LOWORD(lp)),static_cast<short>(HIWORD(lp))};
        if(auto edit=inputAt(point)) {
            RECT client{};GetClientRect(edit,&client);MapWindowPoints(window_,edit,&point,1);
            point.x=std::clamp<LONG>(point.x,0,client.right-1);point.y=client.bottom/2;
            SendMessageW(edit,WM_LBUTTONDOWN,wp,MAKELPARAM(point.x,point.y));return 0;
        }
        break;
    }
    case WM_DRAWITEM: {auto item=reinterpret_cast<DRAWITEMSTRUCT*>(lp);if(!DrawChoiceMenuItem(item))drawButton(item);return TRUE;}
    case WM_MEASUREITEM: {auto item=reinterpret_cast<MEASUREITEMSTRUCT*>(lp);if(!MeasureChoiceMenuItem(item))item->itemHeight=d(item->CtlID==History?66:24);return TRUE;}
    case WM_ENTERIDLE:
        if(wp==MSGF_MENU&&dropdownTestId_)testDropdownIdle(reinterpret_cast<HWND>(lp));
        break;
    case WM_CTLCOLORSTATIC: case WM_CTLCOLOREDIT: {
        auto dc=reinterpret_cast<HDC>(wp);SetTextColor(dc,Ink);
        bool bg=reinterpret_cast<HWND>(lp)==control(ModeText)||!IsWindowEnabled(reinterpret_cast<HWND>(lp));SetBkColor(dc,bg?Bg:White);
        return reinterpret_cast<LRESULT>(bg?background_:white_);
    }
    case WM_DROPFILES: {
        auto drop=reinterpret_cast<HDROP>(wp);wchar_t file[32768]{};
        if(!busy_&&DragQueryFileW(drop,0,file,32768)) {text(Input,file);selectPage(0);}
        DragFinish(drop);return 0;
    }
    case WM_COMMAND: {
        int id=LOWORD(wp);
        if(settingsReady_&&!populatingSettings_&&!busy_&&IsSettingsInput(id)) {
            if(HIWORD(wp)==EN_CHANGE) {
                pendingSettings_.insert(id);if(!SetTimer(window_,SettingsTimer,400,nullptr))flushPendingSettings(false);
                if(id==ProxyUrl) {
                    proxyTested_=false;proxyResults_=json::array();proxyStatus_.clear();
                    if(activeAction_=="testproxy")notice_=false;
                    layout();
                }
                return 0;
            }
            if(HIWORD(wp)==EN_KILLFOCUS){autoSaveSetting(id);return 0;}
        }
        if(HIWORD(wp)==CBN_SELCHANGE&&IsSettingsChoice(id)){autoSaveSetting(id);InvalidateRect(window_,nullptr,FALSE);return 0;}
        if(id==History&&HIWORD(wp)==LBN_SELCHANGE) {selectHistory();return 0;}
        if(HIWORD(wp)!=BN_CLICKED) break;
        if(id>=NavTask&&id<=NavLogs) {selectPage(id-NavTask);return 0;}
        if(id==SettingsAudio||id==SettingsNetwork||id==SettingsRecognition) {selectPage(3,id==SettingsNetwork?2:id==SettingsRecognition?1:0);return 0;}
        if(id==StrictDetails){strictExpanded_=!strictExpanded_;layout();return 0;}
        if(id==MenuModels){environmentTab_=1;selectPage(2);return 0;}
        if(id==ModelSettings){selectPage(3,1);return 0;}
        if(id==EnvironmentBase||id==EnvironmentModels) {
            environmentTab_=id==EnvironmentModels?1:0;selectPage(2);return 0;
        }
        if(id==SpeechChoice||id==ReviewChoice) {
            showChoices(id);return 0;
        }
        if(id==EditSettings||id==NetworkSettings) {selectPage(3,id==NetworkSettings?2:0);return 0;}
        if(id==Install) {environmentTask("install");return 0;}
        if(id>=RepairPython&&id<=RepairFfmpeg) {const char* keys[]={"python","dependencies","whisper","ast","ffmpeg"};environmentTask("install",keys[id-RepairPython]);return 0;}
        if(id==RepairReview){environmentTask("install","review");return 0;}
        if(id>=RepairQwen&&id<=RepairNeural){const char* keys[]={"qwen","aligner","clap","neural"};environmentTask("install",keys[id-RepairQwen]);return 0;}
        if(id==TestProxy) {environmentTask("testproxy");return 0;}
        if(id==ProxyEnabled||id==Audit||id==MenuEnabled||id==FadeEnabled||id==EdgeFadeEnabled||IsSoundOption(id)){
            ToggleChecked(control(id));
            autoSaveSetting(id);
            if(id==ProxyEnabled){proxyTested_=false;proxyResults_=json::array();proxyStatus_.clear();if(activeAction_=="testproxy")notice_=false;}
            enableControls(busy_);return 0;
        }
        if(id==Language||id==Device||id==OutputKind||id==AudioEncoding){showChoices(id);InvalidateRect(window_,nullptr,FALSE);return 0;}
        if(id==NewTask){selectPage(0);return 0;}
        if(id==Reset) {
            if(busy_)return 0;
            const auto previous=cfg_;
            try {auto defaults=ReadJson(root_/L"config/defaults.json");
                const std::vector<std::string> keys=settingsTab_==0?
                    std::vector<std::string>{"max_pause_seconds","silence_db","strict_pre","strict_post","strict_min_section","strict_dense_gap","join_fade_enabled","join_fade_seconds","edge_fade_enabled","edge_fade_seconds"}:
                    settingsTab_==1?std::vector<std::string>{"language","device","speech_model","review_model_id","whisper_model","review_model","review_enabled","generate_program_menu","audio_output_codec"}:
                    std::vector<std::string>{"proxy_enabled","proxy_url"};
                for(const auto& key:keys)cfg_[key]=defaults[key];
                if(settingsTab_==0)for(auto option:SoundOptions)cfg_[option.second]=defaults[option.second];
                if(cfg_.value("mode","")=="extract")cfg_["review_enabled"]=true;
                saveSettings();populateSettings(settingsTab_);enableControls(busy_);notice_=true;status_=L"当前分类已恢复默认。";
            }catch(const std::exception& e){cfg_=previous;notice_=true;status_=L"无法恢复默认："+Wide(e.what());}InvalidateRect(window_,nullptr,FALSE);return 0;
        }
        if(id==ClearLog) {text(Log,L"");return 0;}
        if(id==OpenLogs) {auto path=root_/L"runtime/logs";fs::create_directories(path);ShellExecuteW(window_,L"open",path.c_str(),nullptr,nullptr,SW_SHOWNORMAL);return 0;}
        if(id==BrowseInput) chooseFile(false);
        else if(id==BrowseOutput) chooseFile(true);
        else if(id==Strict||id==Relaxed||id==Extract) { autoSaveSetting(id);if(id==Strict)strictExpanded_=true;refreshMode(); }
        else if(id==Prompt) prompt();
        else if(id==Start) start(false);
        else if(id==Doctor) start(true);
        else if(id==Cancel&&busy_) { cancelled_=true;status_=L"正在取消，已完成的文件和缓存将保留…";EnableWindow(control(Cancel),FALSE);runner_.cancel();InvalidateRect(window_,nullptr,FALSE); }
        else if(id==OpenOutput) {
            fs::path dir=lastResult_.contains("output")?fs::path(Wide(lastResult_["output"])).parent_path():fs::path(value(Output));
            if(fs::is_directory(dir)) ShellExecuteW(window_,L"open",dir.c_str(),nullptr,nullptr,SW_SHOWNORMAL);
            else appendLog(L"输出目录尚未创建，完成首次剪辑后即可打开。");
        } else if(id==ReviewFindings&&lastResult_.contains("output")) {
            showText(L"待复听位置 · "+fs::path(Wide(lastResult_["output"])).filename().wstring(),reviewFindingsText());
        } else if(id==ProgramMenu&&lastResult_.contains("output")) {
            if(lastResult_.value("program_menu",json::object()).value("status","")=="ready")showText(L"成片节目单 · "+fs::path(Wide(lastResult_["output"])).filename().wstring(),programMenuText());
            else programMenuTask();
        } else if((id==Play||id==Mapping)&&lastResult_.contains("output")) {
            fs::path target=Wide(lastResult_["output"]);if(id==Mapping) target=target.parent_path()/L"剪辑时间对照.csv";
            if(fs::exists(target)) ShellExecuteW(window_,L"open",target.c_str(),nullptr,nullptr,SW_SHOWNORMAL);
        }
        return 0;
    }
    case WM_ENGINE_LINE: { std::unique_ptr<std::string> line(reinterpret_cast<std::string*>(lp));receive(*line);return 0; }
    case WM_ENGINE_DONE: {
        runner_.finish();
        if(languageDialog_)SendMessageW(languageDialog_,TDM_CLICK_BUTTON,IDCANCEL,0);
        stopTiming();
        if(activeAction_=="inspect"&&wp==0&&completed_&&!cancelled_)notice_=false;
        if(activeAction_=="testproxy"&&completed_&&!cancelled_)notice_=false;
        if(cancelled_||wp!=0) {
            for(auto& [key,component]:components_) {
                (void)key;auto state=component.value("status","");
                if(state=="checking"||state=="downloading") {component["status"]="missing";component["detail"]=cancelled_?"任务已取消，可以重新检测或下载":"任务未完成，请查看日志后重试";}
            }
            if(cancelled_&&activeAction_=="testproxy") proxyStatus_=L"连接测试已取消，可以重新测试。";
        }
        enableControls(false);
        if((cancelled_||wp!=0)&&(mediaReady_||activeAction_=="menu")) {
            for(const auto& row:history_)if(row.value("output","")==activeOutput_) {
                lastResult_=row;
                if(lastResult_.value("program_menu",json::object()).value("status","")!="ready")lastResult_["program_menu"]={{"status",cancelled_?"cancelled":"failed"},{"chapters",json::array()}};
                if(mediaReady_)lastResult_["elapsed_seconds"]=elapsedSeconds();
                storeResult();break;
            }
            notice_=true;status_=cancelled_?L"成片已保留，节目单生成已取消。":L"成片已保留，节目单暂未生成，请查看日志。";
        }
        else if(cancelled_) { notice_=true;status_=L"任务已取消。";appendLog(status_); }
        else if(wp!=0||!completed_) { if(status_.find(L"失败")==std::wstring::npos) status_=L"处理未完成，请查看日志。"; }
        InvalidateRect(window_,nullptr,FALSE);
        if(testing()&&!options_.contains("test-menu")) finishTest(wp==0&&completed_?0:1);
        return 0;
    }
    case WM_TIMER:
        if(wp==SettingsTimer){flushPendingSettings(false);return 0;}
        if(wp==20) {
            invalidateFooter();return 0;
        }
        if(wp!=1)return 0;
        KillTimer(window_,1);
        if(options_.contains("test-menu")) testProgramMenu();
        else if(options_.contains("test-progress")) testProgress();
        else if(options_.contains("test-dropdowns")) testDropdowns();
        else if(options_.contains("test-controls")) testControls();
        else if(options_.contains("test-switches")) testSwitches();
        else if(options_.contains("test-autosave")) testAutoSave();
        else if(options_.contains("test-navigation")) testNavigationRendering();
        else if(options_.contains("test-rendering")) testRendering();
        else if(options_.contains("test-job")) start(false);
        else if(options_.contains("self-test")) start(true);
        else if(options_.contains("test-action")) environmentTask(options_["test-action"],options_.value("test-component","all"));
        else if(options_.contains("snapshot")) {if(options_.contains("focus-mode"))SetFocus(control(Relaxed));finishTest(0);}
        else environmentTask("inspect");
        return 0;
    case WM_CLOSE:
        flushPendingSettings();
        try { readSettings();saveSettings(); } catch (...) {}
        runner_.cancel();runner_.finish();DestroyWindow(window_);return 0;
    case WM_DESTROY: {
        MSG pending{};
        while(PeekMessageW(&pending,window_,WM_ENGINE_LINE,WM_ENGINE_LINE,PM_REMOVE)) delete reinterpret_cast<std::string*>(pending.lParam);
        PostQuitMessage(testExit_);return 0;
    }
    }
    return DefWindowProcW(window_,msg,wp,lp);
}
