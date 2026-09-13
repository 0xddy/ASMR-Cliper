#include "MainWindow.h"
#include "UiControls.h"
#include "UiTheme.h"
#include <cmath>
#include <commctrl.h>
#include <algorithm>

void MainWindow::testRendering() {
    nlohmann::json checks=nlohmann::json::array();
    auto check=[&](const char* name,bool passed){checks.push_back({{"name",name},{"passed",passed}});};
    SetWindowPos(window_,nullptr,-32000,-32000,d(1140),d(820),SWP_NOZORDER|SWP_NOACTIVATE);
    ShowWindow(window_,SW_SHOWNOACTIVATE);selectPage(3,0);
    activeAction_="run";beginTiming();enableControls(true);
    auto clean=[&] {RedrawWindow(window_,nullptr,nullptr,RDW_VALIDATE|RDW_NOERASE|RDW_ALLCHILDREN);};
    RECT client{},dirty{};GetClientRect(window_,&client);
    clean();
    for(int n=0;n<100;++n)receive(nlohmann::json{{"type","task_progress"},{"task_progress",{
        {"stage",5},{"stages",7},{"title","成片复核"},{"round",2},{"round_limit",3},{"percent",n},{"detail","窗口进度"}}}}.dump());
    check("progress burst invalidates only task footer",GetUpdateRect(window_,&dirty,FALSE)&&dirty.top>=client.bottom-d(80)&&dirty.left>=d(200));
    check("progress leaves settings and navigation clean",!GetUpdateRect(control(Silence),nullptr,FALSE)&&!GetUpdateRect(control(NavSettings),nullptr,FALSE));
    clean();receive(R"({"type":"log","message":"模型继续处理"})");
    check("log-only event leaves page background clean",!GetUpdateRect(window_,nullptr,FALSE));
    struct Counts {int positions=0,paints=0,shows=0,erases=0;} counts;
    auto observer=[](HWND hwnd,UINT message,WPARAM wp,LPARAM lp,UINT_PTR,DWORD_PTR data)->LRESULT {
        auto& count=*reinterpret_cast<Counts*>(data);
        if(message==WM_WINDOWPOSCHANGED)++count.positions;
        if(message==WM_PAINT)++count.paints;
        if(message==WM_SHOWWINDOW)++count.shows;
        if(message==WM_ERASEBKGND)++count.erases;
        return DefSubclassProc(hwnd,message,wp,lp);
    };
    for(auto [id,hwnd]:controls_)SetWindowSubclass(hwnd,observer,99,reinterpret_cast<DWORD_PTR>(&counts));
    clean();layout();layout();
    check("unchanged layout does not move show or synchronously paint controls",counts.positions==0&&counts.paints==0&&counts.shows==0&&counts.erases==0);
    counts={};const auto originalDpi=dpi_;dpi_=144;layout();
    check("changed layout batches moves without intermediate control paints",counts.positions>0&&counts.paints==0&&counts.erases==0);
    dpi_=originalDpi;layout();
    counts={};SendMessageW(window_,WM_SIZE,SIZE_MINIMIZED,0);
    check("minimizing never collapses child layout",counts.positions==0&&counts.paints==0);
    for(auto [id,hwnd]:controls_)RemoveWindowSubclass(hwnd,observer,99);
    stopTiming();enableControls(false);
    HDC target=GetDC(window_),memory=CreateCompatibleDC(target);
    auto bitmap=CreateCompatibleBitmap(target,160,96);auto oldBitmap=SelectObject(memory,bitmap);
    RECT area{0,0,160,96},update{20,12,140,70};
    auto sentinel=CreateSolidBrush(UiTheme::Accent);FillRect(memory,&area,sentinel);
    {
        BufferedSurface surface(memory,update);
        check("painting uses a separate surface",surface.dc()!=memory);
        FillRect(surface.dc(),&update,white_);
        check("background clearing is not presented mid-frame",GetPixel(memory,40,30)==UiTheme::Accent);
    }
    check("completed buffer updates only its dirty rectangle",GetPixel(memory,40,30)==UiTheme::White&&GetPixel(memory,10,10)==UiTheme::Accent);
    FillRect(memory,&area,sentinel);int saved=SaveDC(memory);IntersectClipRect(memory,30,18,100,60);
    {BufferedSurface surface(memory,update);FillRect(surface.dc(),&update,white_);}
    RestoreDC(memory,saved);
    check("buffer presentation respects target child clipping",GetPixel(memory,40,30)==UiTheme::White&&GetPixel(memory,22,14)==UiTheme::Accent);
    SelectObject(memory,oldBitmap);DeleteObject(bitmap);DeleteObject(sentinel);DeleteDC(memory);ReleaseDC(window_,target);
    const auto folder=std::filesystem::path(Wide(options_["test-rendering"])).parent_path();
    screenshot(folder/L"rendering-settings.png");
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(folder/L"rendering-regression.json",{{"passed",passed},{"checks",checks}});
    completed_=passed;finishTest(passed?0:1);
}

void MainWindow::testProgramMenu() {
    using json=nlohmann::json;
    json checks=json::array();
    auto check=[&](const char* name,bool passed){checks.push_back({{"name",name},{"passed",passed}});};
    auto visible=[&](int id){return (GetWindowLongPtrW(control(id),GWL_STYLE)&WS_VISIBLE)!=0;};
    const auto folder=std::filesystem::path(Wide(options_["test-menu"])).parent_path();
    SetWindowPos(window_,nullptr,0,0,d(1100),d(800),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    selectPage(3,1);
    check("menu and review share recognition settings",visible(MenuEnabled)&&visible(Audit)&&visible(SpeechChoice)&&!visible(KeepSoftLaugh)&&!visible(ProxyUrl));
    const auto language=cfg_["language"];text(Silence,L"unfinished value");
    SendMessageW(control(MenuEnabled),BM_SETCHECK,BST_CHECKED,0);SendMessageW(control(MenuEnabled),BM_CLICK,0,0);
    check("menu settings save without reading other unfinished fields",cfg_["generate_program_menu"]==false&&cfg_["language"]==language);
    SendMessageW(control(Reset),BM_CLICK,0,0);
    check("menu default resets independently",cfg_["generate_program_menu"]==true&&value(Silence)==L"unfinished value");
    screenshot(folder/L"program-menu-settings.png");
    activeAction_="run";completed_=cancelled_=false;beginTiming();enableControls(true);
    json report={{"type","media_ready"},{"output","example_ASMR_v3.m4a"},{"duration",180.},{"segments",4},
        {"speech_review",{{"status","passed"}}},{"program_menu",{{"status","pending"},{"chapters",json::array()}}}};
    receive(report.dump());
    check("validated media enters history before menu inference",history_.size()==1&&mediaReady_&&!completed_&&timing_&&!IsWindowEnabled(control(ProgramMenu)));
    json menu={{"status","ready"},{"chapters",json::array({
        {{"start",0.},{"end",70.},{"title","舔耳 / 湿润口腔音"}},
        {{"start",70.},{"end",130.},{"title","道具敲击"}},
        {{"start",130.},{"end",160.},{"title","心跳"}},
        {{"start",160.},{"end",180.},{"title","待确认"}}})}};
    report["type"]="complete";report["program_menu"]=menu;receive(report.dump());enableControls(false);selectPage(1);
    check("final menu updates the same history entry",history_.size()==1&&completed_&&value(ProgramMenu)==L"查看节目单");
    const auto content=programMenuText();
    check("menu shows actual playback order and output times",content.find(L"00:01:10")!=std::wstring::npos&&content.find(L"舔耳")<content.find(L"道具敲击")&&content.find(L"道具敲击")<content.find(L"心跳")&&content.find(L"待确认")!=std::wstring::npos);
    screenshot(folder/L"program-menu-history.png");SendMessageW(control(ProgramMenu),BM_CLICK,0,0);
    const auto elapsed=history_[0]["elapsed_seconds"];
    activeAction_="menu";completed_=false;beginTiming();activeOutput_="example_ASMR_v3.m4a";
    receive(json{{"type","menu_complete"},{"output",activeOutput_},{"program_menu",menu}}.dump());
    check("old output annotation keeps original task duration and record",history_.size()==1&&history_[0]["elapsed_seconds"]==elapsed&&history_[0]["duration"]==180.);
    activeAction_="run";completed_=cancelled_=false;beginTiming();enableControls(true);
    report["type"]="media_ready";report["output"]="cancelled_menu_ASMR_v4.m4a";report["program_menu"]={{"status","pending"}};receive(report.dump());
    cancelled_=true;SendMessageW(window_,WM_ENGINE_DONE,ERROR_CANCELLED,0);
    check("cancelling menu keeps the finished output and history",history_.size()==2&&history_[0]["program_menu"]["status"]=="cancelled"&&status_.find(L"成片已保留")!=std::wstring::npos&&!timing_);
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(std::filesystem::path(Wide(options_["test-menu"])),{{"passed",passed},{"checks",checks}});
    completed_=passed;finishTest(passed?0:1);
}

void MainWindow::testProgress() {
    nlohmann::json checks=nlohmann::json::array();
    auto check=[&](const char* name,bool passed){checks.push_back({{"name",name},{"passed",passed}});};
    const auto folder=std::filesystem::path(Wide(options_["test-progress"])).parent_path();
    SetWindowPos(window_,nullptr,0,0,d(1100),d(800),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    text(Input,L"D:\\ASMR\\示例录音.mp4");text(Output,L"D:\\ASMR\\剪辑结果");
    activeAction_="run";completed_=false;notice_=true;enableControls(true);beginTiming();
    taskStarted_-=18*60000+32000;
    nlohmann::json progress={{"stage",5},{"stages",6},{"title","成片复核"},{"round",2},{"round_limit",3},
        {"percent",46.2},{"detail","音频块 12/26 · 边界检查 2/2 · 窗口 5/11"}};
    receive(nlohmann::json{{"type","task_progress"},{"task_progress",progress}}.dump());
    const auto title=status_;
    check("round and limit are visible",title.find(L"第 2 轮 / 最多 3 轮")!=std::wstring::npos);
    receive(R"({"type":"log","message":"Qwen 语音定位：5 / 11 个窗口","progress":95})");
    receive(R"({"type":"progress","message":"成片大模型复核","progress":98})");
    check("legacy model logs cannot overwrite task progress",status_==title&&SendMessageW(control(Progress),PBM_GETPOS,0,0)==462);
    const auto events=eventCount_;SendMessageW(window_,WM_TIMER,20,0);
    check("clock timer never restarts environment inspection",activeAction_=="run"&&eventCount_==events);
    check("elapsed clock runs without log events",elapsedSeconds()>=1112.);
    for(int page:{0,3,4}) {selectPage(page);screenshot(folder/(L"progress-page-"+std::to_wstring(page)+L".png"));}
    progress["round"]=3;progress["percent"]=0.;progress["detail"]="准备本轮候选音轨";
    receive(nlohmann::json{{"type","task_progress"},{"task_progress",progress}}.dump());
    check("next round resets only its own progress",status_.find(L"第 3 轮")!=std::wstring::npos&&SendMessageW(control(Progress),PBM_GETPOS,0,0)==0);
    receive(R"({"type":"complete","message":"完成","output":"test.m4a","duration":4800,"segments":24,"speech_review":{"status":"passed"}})");
    const auto elapsed=elapsedSeconds();taskStarted_-=5000;SendMessageW(window_,WM_TIMER,20,0);
    check("completion freezes and persists elapsed independently of media duration",!timing_&&elapsedSeconds()==elapsed&&history_.front()["elapsed_seconds"]==elapsed&&history_.front()["duration"]==4800);
    enableControls(false);selectPage(1);screenshot(folder/L"progress-complete.png");
    completed_=false;beginTiming();taskStarted_-=65*60000+7000;
    receive(R"({"type":"complete","output":"example_ASMR_v4.m4a","duration":7200,"segments":32,"speech_review":{"status":"needs_review","findings":[{"start":4980.12,"end":4982.36,"text":"这是一处疑似话语"}]}})");
    enableControls(false);selectPage(1);
    check("uncertain speech completes and remains in history",completed_&&history_.front()["speech_review"]["status"]=="needs_review"&&status_.find(L"失败")==std::wstring::npos);
    const auto findings=reviewFindingsText();
    check("history exposes exact output positions and transcripts",findings.find(L"01:23:00.120")!=std::wstring::npos&&findings.find(L"这是一处疑似话语")!=std::wstring::npos&&(GetWindowLongPtrW(control(ReviewFindings),GWL_STYLE)&WS_VISIBLE));
    screenshot(folder/L"progress-flagged.png");
    beginTiming();check("new task resets elapsed and stage",elapsedSeconds()<1.&&taskProgress_.empty());
    receive(R"({"type":"error","message":"test error"})");check("errors stop elapsed clock",!timing_);
    beginTiming();cancelled_=true;stopTiming();const auto cancelledElapsed=elapsedSeconds();taskStarted_-=5000;
    check("cancelled task elapsed stays fixed",elapsedSeconds()==cancelledElapsed);cancelled_=false;
    const nlohmann::json language={{"detected","ko"},{"choices",{"ko","ja","zh","en"}}};
    options_["test-language-choice"]="ja";
    check("language dialog accepts a corrected language",chooseLanguage(language)=="ja");
    options_["test-language-choice"]="ko";
    check("language dialog accepts detected language",chooseLanguage(language)=="ko");
    options_["test-language-choice"]="cancel";
    check("language dialog cancellation does not choose a language",chooseLanguage(language).empty());
    auto unknown=language;unknown["detected"]="auto";options_["test-language-choice"]="zh";
    check("unknown language can be explicitly selected",chooseLanguage(unknown)=="zh");
    options_.erase("test-language-choice");
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(std::filesystem::path(Wide(options_["test-progress"])),{{"passed",passed},{"checks",checks}});
    completed_=passed;finishTest(passed?0:1);
}

void MainWindow::testDropdownIdle(HWND popup) {
    if(dropdownTestIdle_)return;
    dropdownTestIdle_=true;
    MENUBARINFO info{sizeof(info)};RECT field{},menu{},first{},last{};
    GetWindowRect(control(dropdownTestId_),&field);GetWindowRect(popup,&menu);
    bool measured=GetMenuBarInfo(popup,OBJID_CLIENT,0,&info)!=FALSE;
    int count=static_cast<int>(SendMessageW(control(dropdownTestId_),CB_GETCOUNT,0,0));
    measured=measured&&GetMenuItemRect(window_,info.hMenu,0,&first)&&GetMenuItemRect(window_,info.hMenu,count-1,&last);
    dropdownChecks_.push_back({{"name","popup geometry "+std::to_string(dropdownTestId_)},
        {"passed",measured&&abs((menu.right-menu.left)-(field.right-field.left))<=d(4)&&first.bottom-first.top>=d(40)&&last.bottom<=menu.bottom},
        {"field_width",field.right-field.left},{"popup_width",menu.right-menu.left},{"row_height",first.bottom-first.top}});
    if(!dropdownTestCancel_) {
        auto path=std::filesystem::path(Wide(options_["test-dropdowns"])).parent_path()/
            (L"dropdown-"+std::to_wstring(dropdownTestId_)+L".png");
        screenshot(path,popup);
        // Mouse hover can preselect a row while the screenshot is captured.
        // Anchor keyboard navigation before testing wraparound to the last row.
        PostMessageW(window_,WM_KEYDOWN,VK_HOME,1);
        PostMessageW(window_,WM_KEYDOWN,VK_UP,1);
        PostMessageW(window_,WM_KEYDOWN,VK_RETURN,1);
    } else {
        PostMessageW(window_,WM_KEYDOWN,VK_DOWN,1);
        PostMessageW(window_,WM_KEYDOWN,VK_ESCAPE,1);
    }
}

void MainWindow::testDropdowns() {
    // Exercise the real menu loop and keyboard messages; settings are never saved in tests.
    auto originalSettings=cfg_;
    for(int id:{SpeechChoice,ReviewChoice,Language,Device,OutputKind,AudioEncoding}) {
        if(id==OutputKind)selectPage(0);
        else {SendMessageW(control(Audit),BM_SETCHECK,BST_CHECKED,0);enableControls(false);selectPage(3,1);}
        dropdownTestId_=id;
        int count=static_cast<int>(SendMessageW(control(id),CB_GETCOUNT,0,0));
        for(bool cancel:{false,true}) {
            dropdownTestCancel_=cancel;dropdownTestIdle_=false;
            if(!cancel)SendMessageW(control(id),CB_SETCURSEL,0,0);
            SendMessageW(control(id),WM_KEYDOWN,VK_F4,1);
            dropdownChecks_.push_back({{"name",std::string(cancel?"Escape preserves selection ":"Arrow and Enter select last item ")+std::to_string(id)},
                {"passed",dropdownTestIdle_&&SendMessageW(control(id),CB_GETCURSEL,0,0)==count-1},
                {"actual_selection",SendMessageW(control(id),CB_GETCURSEL,0,0)}});
        }
    }
    dropdownTestId_=0;
    // Verify the component list at the minimum supported window size, including repair actions.
    auto folder=std::filesystem::path(Wide(options_["test-dropdowns"])).parent_path();
    cfg_=originalSettings;populateSettings();environmentTab_=1;selectPage(2);screenshot(folder/L"environment-models.png");
    SetWindowPos(window_,nullptr,0,0,d(1100),d(800),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    screenshot(folder/L"environment-minimum.png");
    components_.clear();enableControls(false);screenshot(folder/L"environment-missing.png");
    environmentTab_=0;selectPage(2);screenshot(folder/L"environment-base-missing.png");
    bool passed=std::all_of(dropdownChecks_.begin(),dropdownChecks_.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(std::filesystem::path(Wide(options_["test-dropdowns"])),{{"passed",passed},{"checks",dropdownChecks_}});
    completed_=passed;finishTest(passed?0:1);
}

void MainWindow::testControls() {
    cfg_["mode"]="relaxed";refreshMode();
    nlohmann::json checks=nlohmann::json::array();
    auto check=[&](const char* name,bool passed) {checks.push_back({{"name",name},{"passed",passed}});};
    auto click=[&](int id) {SendMessageW(control(id),BM_CLICK,0,0);};
    auto choose=[&](int id,int index) {SendMessageW(control(id),CB_SETCURSEL,index,0);SendMessageW(window_,WM_COMMAND,MAKEWPARAM(id,CBN_SELCHANGE),reinterpret_cast<LPARAM>(control(id)));};
    auto visible=[&](int id) {return (GetWindowLongPtrW(control(id),GWL_STYLE)&WS_VISIBLE)!=0;};
    auto checked=[&](int id) {return SendMessageW(control(id),BM_GETCHECK,0,0)==BST_CHECKED;};
    const auto folder=std::filesystem::path(Wide(options_["test-controls"])).parent_path();
    selectPage(0);SendMessageW(control(OutputKind),CB_SETCURSEL,2,0);readSettings();
    check("video output choice reaches job settings",cfg_["output_kind"]=="video");
    populateSettings();check("output choice round trip",SendMessageW(control(OutputKind),CB_GETCURSEL,0,0)==2);
    choose(OutputKind,3);populateSettings();
    check("precise video selection saves and round trips",cfg_["output_kind"]=="video"&&cfg_["video_cut_mode"]=="precise"&&SendMessageW(control(OutputKind),CB_GETCURSEL,0,0)==3);
    screenshot(folder/L"precise-video-output.png");
    SendMessageW(control(OutputKind),CB_SETCURSEL,0,0);readSettings();
    check("default output returns to original video packets",cfg_["video_cut_mode"]=="copy");
    wchar_t title[128]{};GetWindowTextW(window_,title,128);
    check("window name and icon",std::wstring(title)==L"ASMR-Cliper"&&GetClassLongPtrW(window_,GCLP_HICON)!=0&&GetClassLongPtrW(window_,GCLP_HICONSM)!=0);
    selectPage(2);click(EnvironmentModels);
    check("environment focuses on model files",visible(ModelSettings)&&!visible(SpeechChoice)&&!visible(ReviewChoice)&&!visible(RepairPython));
    click(ModelSettings);
    check("model settings shortcut opens recognition category",page_==3&&settingsTab_==1&&visible(SpeechChoice)&&visible(Language)&&visible(Audit)&&visible(MenuEnabled)&&!visible(Silence));
    click(Reset);click(MenuModels);
    check("model manager shortcut returns to environment files",page_==2&&environmentTab_==1);
    click(EnvironmentBase);check("base environment remains separate",environmentTab_==0&&!visible(ModelSettings));
    selectPage(3,0);click(Reset);strictExpanded_=false;layout();
    check("editing groups sounds pauses and fades",visible(KeepSoftLaugh)&&visible(KeepTapping)&&visible(Silence)&&visible(FadeEnabled)&&!visible(Language)&&!visible(Audit)&&!visible(ProxyUrl));
    check("strict parameters can be folded",!visible(Before)&&visible(StrictDetails));
    check("sound defaults preserved",checked(KeepWhisper)&&checked(KeepSoftLaugh)&&checked(KeepHeartbeat)&&checked(KeepTapping)&&!checked(KeepLoudLaugh)&&!checked(KeepVaping)&&!checked(KeepDrinking)&&!checked(KeepImpacts));
    check("whisper permission is visible and named correctly",visible(KeepWhisper)&&value(KeepWhisper)==L"轻语 / 耳语");
    check("airflow label no longer identifies a device",value(KeepVaping)==L"呼气/烟雾");
    check("pause and fade defaults preserved",value(Silence)==L"1.5"&&!checked(FadeEnabled)&&value(FadeSeconds)==L"0.3"&&!IsWindowEnabled(control(FadeSeconds)));
    check("output edges default on at half a second",checked(EdgeFadeEnabled)&&value(EdgeFadeSeconds)==L"0.5"&&IsWindowEnabled(control(EdgeFadeSeconds)));
    click(EdgeFadeEnabled);check("edge duration follows its switch",!IsWindowEnabled(control(EdgeFadeSeconds)));click(EdgeFadeEnabled);
    text(EdgeFadeSeconds,L"0.8");flushPendingSettings();populateSettings(0);
    check("edge duration saves and round trips independently of joins",cfg_["edge_fade_enabled"]==true&&cfg_["edge_fade_seconds"]==.8&&!cfg_["join_fade_enabled"].get<bool>()&&value(EdgeFadeSeconds)==L"0.8");
    const auto beforeEdge=cfg_;text(EdgeFadeSeconds,L"4");flushPendingSettings();check("invalid edge duration cannot change configuration",cfg_==beforeEdge);
    text(EdgeFadeSeconds,L"0.5");flushPendingSettings();
    SetWindowPos(window_,nullptr,0,0,d(1100),d(800),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    screenshot(folder/L"preferences-editing.png");
    click(StrictDetails);
    for(int id:{Silence,SilenceDb,Before,After,Minimum,DenseGap}) {
        auto frame=inputFrames_.at(id);POINT point{frame.left+d(5),frame.top+d(5)};
        auto before=value(id);SetFocus(control(SettingsAudio));
        SendMessageW(window_,WM_LBUTTONDOWN,MK_LBUTTON,MAKELPARAM(point.x,point.y));
        SendMessageW(control(id),WM_LBUTTONUP,0,0);
        check(("input padding focuses without changing value "+std::to_string(id)).c_str(),GetFocus()==control(id)&&value(id)==before);
    }
    RECT action{},client{};GetWindowRect(control(Reset),&action);MapWindowPoints(nullptr,window_,reinterpret_cast<POINT*>(&action),2);GetClientRect(window_,&client);
    check("header actions and expanded strict fields fit minimum window",visible(Before)&&action.bottom<d(94)&&inputFrames_.at(Before).bottom+d(20)<client.bottom-d(80));
    screenshot(folder/L"preferences-editing-expanded.png");
    SetWindowPos(window_,nullptr,0,0,d(1560),d(960),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    screenshot(folder/L"preferences-editing-wide.png");
    check("wide window keeps bounded settings columns",inputFrames_.at(EdgeFadeSeconds).right<=d(224+1040-24));
    SetWindowPos(window_,nullptr,0,0,d(1100),d(800),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    text(Before,L"6.5");click(StrictDetails);click(SettingsRecognition);click(SettingsAudio);click(StrictDetails);
    check("folding and category changes keep strict drafts",visible(Before)&&value(Before)==L"6.5");
    text(Silence,L"1.2");click(FadeEnabled);text(FadeSeconds,L"0.45");
    for(auto [id,key]:SoundOptions)click(id);
    flushPendingSettings();populateSettings(0);
    check("editing saves pause fade and strict parameters together",cfg_["max_pause_seconds"]==1.2&&cfg_["strict_pre"]==6.5&&cfg_["join_fade_enabled"]==true&&cfg_["join_fade_seconds"]==.45&&value(Silence)==L"1.2"&&value(FadeSeconds)==L"0.45"&&!cfg_.contains("silence_seconds"));
    check("retention settings round trip",!checked(KeepWhisper)&&cfg_["keep_whisper"]==false&&!checked(KeepSoftLaugh)&&!checked(KeepHeartbeat)&&!checked(KeepTapping)&&checked(KeepLoudLaugh)&&checked(KeepVaping)&&checked(KeepDrinking)&&checked(KeepImpacts));
    const auto previousFade=cfg_["join_fade_seconds"];text(Silence,L"1.1");click(KeepTapping);text(FadeSeconds,L"nan");flushPendingSettings();
    check("invalid fade keeps last valid value without blocking other settings",cfg_["join_fade_seconds"]==previousFade&&cfg_["max_pause_seconds"]==1.1&&cfg_["keep_tapping"]==true);
    const auto beforeInvalid=cfg_;
    text(FadeSeconds,L"0.45");text(Silence,L"0.1");bool rejectedPause=false;try{readSettings();}catch(...){rejectedPause=true;}
    check("invalid maximum pause rejects task settings atomically",rejectedPause&&cfg_==beforeInvalid);
    text(Silence,L"unfinished pause");text(FadeSeconds,L"0.6");text(EdgeFadeSeconds,L"0.9");
    click(SettingsRecognition);choose(SpeechChoice,1);choose(ReviewChoice,0);
    choose(Language,2);choose(Device,2);click(MenuEnabled);click(Audit);
    check("recognition saves without parsing unfinished editing fields",cfg_["speech_model"]=="qwen3-asr"&&cfg_["review_model_id"]=="whisper-large-v3"&&cfg_["language"]=="ja"&&cfg_["device"]=="cpu"&&cfg_["generate_program_menu"]==false&&cfg_["review_enabled"]==false&&cfg_["max_pause_seconds"]==1.1);
    check("review model follows review switch",!IsWindowEnabled(control(ReviewChoice)));click(Audit);
    check("enabling review restores model selection",IsWindowEnabled(control(ReviewChoice)));
    populateSettings(1);check("recognition selections round trip",value(Language)==L"日语"&&value(Device)==L"CPU"&&SendMessageW(control(SpeechChoice),CB_GETCURSEL,0,0)==1&&checked(Audit));
    SendMessageW(control(OutputKind),CB_SETCURSEL,2,0);click(Reset);
    check("recognition reset preserves editing and output drafts",value(Silence)==L"unfinished pause"&&value(FadeSeconds)==L"0.6"&&value(EdgeFadeSeconds)==L"0.9"&&checked(FadeEnabled)&&SendMessageW(control(OutputKind),CB_GETCURSEL,0,0)==2);
    choose(AudioEncoding,1);populateSettings(1);
    check("output encoding is saved in recognition category",cfg_["audio_output_codec"]=="flac"&&SendMessageW(control(AudioEncoding),CB_GETCURSEL,0,0)==1);
    screenshot(folder/L"preferences-recognition.png");
    choose(AudioEncoding,2);choose(SpeechChoice,1);choose(Language,2);click(MenuEnabled);
    click(SettingsAudio);click(Reset);
    check("editing reset preserves recognition drafts",SendMessageW(control(AudioEncoding),CB_GETCURSEL,0,0)==2&&SendMessageW(control(SpeechChoice),CB_GETCURSEL,0,0)==1&&value(Language)==L"日语"&&!checked(MenuEnabled)&&value(Silence)==L"1.5"&&!checked(FadeEnabled)&&checked(EdgeFadeEnabled)&&value(EdgeFadeSeconds)==L"0.5");
    click(SettingsNetwork);text(ProxyUrl,L"http://127.0.0.1:10886");
    if(checked(ProxyEnabled))click(ProxyEnabled);
    flushPendingSettings();check("proxy off disables address",!IsWindowEnabled(control(ProxyUrl))&&cfg_["proxy_enabled"]==false);
    click(ProxyEnabled);
    check("proxy saves without overwriting recognition settings",IsWindowEnabled(control(ProxyUrl))&&cfg_["proxy_enabled"]==true&&cfg_["proxy_url"]=="http://127.0.0.1:10886"&&cfg_["speech_model"]=="qwen3-asr"&&SendMessageW(control(SpeechChoice),CB_GETCURSEL,0,0)==1);
    screenshot(folder/L"preferences-network.png");
    click(Reset);check("network reset preserves other drafts",value(Language)==L"日语"&&!checked(MenuEnabled));
    enableControls(true);
    check("busy state exposes cancel and locks all settings",visible(Progress)&&visible(Cancel)&&!IsWindowEnabled(control(OutputKind))&&!IsWindowEnabled(control(AudioEncoding))&&!IsWindowEnabled(control(SpeechChoice))&&!IsWindowEnabled(control(ReviewChoice))&&!IsWindowEnabled(control(ProxyEnabled))&&!IsWindowEnabled(control(ProxyUrl))&&!IsWindowEnabled(control(Start))&&!IsWindowEnabled(control(KeepTapping))&&!IsWindowEnabled(control(FadeEnabled))&&!IsWindowEnabled(control(FadeSeconds))&&!IsWindowEnabled(control(EdgeFadeEnabled))&&!IsWindowEnabled(control(EdgeFadeSeconds))&&!IsWindowEnabled(control(MenuEnabled))&&!IsWindowEnabled(control(Reset)));
    click(SettingsRecognition);check("category navigation stays available while busy",settingsTab_==1&&visible(Audit)&&!IsWindowEnabled(control(Audit)));
    enableControls(false);check("idle state hides task progress",!visible(Progress)&&!visible(Cancel)&&IsWindowEnabled(control(Start)));
    readSettings();check("starting task reads drafts from all categories",cfg_["audio_output_codec"]=="pcm"&&cfg_["speech_model"]=="qwen3-asr"&&cfg_["language"]=="ja"&&cfg_["generate_program_menu"]==false&&cfg_["output_kind"]=="video"&&cfg_["max_pause_seconds"]==1.5);
    selectPage(1);check("empty history only offers new task",visible(NewTask)&&!visible(History)&&!visible(Play)&&!visible(Mapping)&&!visible(OpenOutput));
    click(NewTask);check("new task opens editor",page_==0&&visible(Input));
    click(Extract);readSettings();check("V4 selection reaches task settings",cfg_["mode"]=="extract"&&visible(Extract));
    check("V4 requires final speech review",checked(Audit)&&!IsWindowEnabled(control(Audit))&&cfg_["review_enabled"]==true);
    history_.push_back({{"output","test.m4a"},{"speech_review",{{"status","needs_review"},{"findings",nlohmann::json::array({{{"start",1},{"end",2},{"text","speech"}}})}}}});
    updateHistory();selectPage(1);check("unresolved review offers listening positions",visible(ReviewFindings));
    history_[0]["speech_review"]["status"]="passed";updateHistory();check("passed review hides unresolved positions",!visible(ReviewFindings));
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(std::filesystem::path(Wide(options_["test-controls"])),{{"passed",passed},{"checks",checks}});
    completed_=passed;finishTest(passed?0:1);
}

void MainWindow::testAutoSave() {
    using json=nlohmann::json;
    namespace fs=std::filesystem;
    json checks=json::array();
    auto check=[&](const char* name,bool passed){checks.push_back({{"name",name},{"passed",passed}});};
    auto click=[&](int id){SendMessageW(control(id),BM_CLICK,0,0);};
    auto choose=[&](int id,int index){SendMessageW(control(id),CB_SETCURSEL,index,0);SendMessageW(window_,WM_COMMAND,MAKEWPARAM(id,CBN_SELCHANGE),reinterpret_cast<LPARAM>(control(id)));};
    auto blur=[&](int id){SendMessageW(window_,WM_COMMAND,MAKEWPARAM(id,EN_KILLFOCUS),reinterpret_cast<LPARAM>(control(id)));};
    auto pump=[&](DWORD milliseconds) {
        auto until=GetTickCount64()+milliseconds;
        while(GetTickCount64()<until) {
            MSG message{};while(PeekMessageW(&message,nullptr,0,0,PM_REMOVE)){TranslateMessage(&message);DispatchMessageW(&message);}
            auto now=GetTickCount64();if(now<until)MsgWaitForMultipleObjects(0,nullptr,FALSE,static_cast<DWORD>(until-now),QS_ALLINPUT);
        }
    };
    const auto report=fs::path(Wide(options_["test-autosave"]));auto file=report;file.replace_extension(L".settings.json");
    auto saved=[&](){return ReadJson(file);};
    cfg_=ReadJson(root_/L"config/defaults.json");cfg_["input"]="";cfg_["output_dir"]="output";
    populateSettings();saveSettings();selectPage(3,0);
    check("preferences have no save button",!control(ReservedSave)&&control(Reset));
    check("whispers are enabled by default",saved()["keep_whisper"]==true);
    click(KeepWhisper);check("whisper permission saves immediately",saved()["keep_whisper"]==false);
    click(FadeEnabled);check("switch saves immediately to disk",saved()["join_fade_enabled"]==true);
    text(Silence,L"");pump(450);
    check("unfinished number keeps saved value",saved()["max_pause_seconds"]==1.5);
    click(KeepTapping);check("unfinished number does not block another setting",saved()["keep_tapping"]==false&&saved()["max_pause_seconds"]==1.5);
    text(Silence,L"0.85");check("typing is debounced",saved()["max_pause_seconds"]==1.5);
    pump(450);check("idle input saves without focus change",saved()["max_pause_seconds"]==.85);
    text(Silence,L"11");blur(Silence);
    check("out of range input stays unsaved and reports the field",saved()["max_pause_seconds"]==.85&&status_.find(L"最长空窗期")!=std::wstring::npos);
    text(Silence,L"1.2");blur(Silence);check("valid correction saves on blur and clears error",saved()["max_pause_seconds"]==1.2&&saveErrorControl_==0);
    text(Before,L"6.5");selectPage(3,1);
    check("switching category flushes pending values",saved()["strict_pre"]==6.5);
    choose(Language,2);choose(Device,2);choose(SpeechChoice,1);choose(ReviewChoice,0);choose(AudioEncoding,3);
    check("dropdown changes save models language device and encoding",saved()["language"]=="ja"&&saved()["device"]=="cpu"&&saved()["speech_model"]=="qwen3-asr"&&saved()["whisper_model"]=="models/qwen-asr"&&saved()["review_model_id"]=="whisper-large-v3"&&saved()["audio_output_codec"]=="aac");
    click(MenuEnabled);click(Audit);check("review and menu switches save immediately",saved()["generate_program_menu"]==false&&saved()["review_enabled"]==false);
    selectPage(3,2);text(ProxyUrl,L"http://127.0.0.1:18080");blur(ProxyUrl);click(ProxyEnabled);
    check("network changes save without changing recognition",saved()["proxy_url"]=="http://127.0.0.1:18080"&&saved()["proxy_enabled"]==false&&saved()["language"]=="ja");
    click(Reset);check("reset saves only the current category",saved()["proxy_url"]==cfg_["proxy_url"]&&saved()["proxy_enabled"]==true&&saved()["language"]=="ja"&&saved()["strict_pre"]==6.5);
    selectPage(3,0);text(Silence,L"2.5");click(Reset);pump(450);
    check("reset discards a pending value in its category",saved()["max_pause_seconds"]==1.5&&value(Silence)==L"1.5"&&saved()["language"]=="ja");
    check("editing reset restores whisper permission",saved()["keep_whisper"]==true);
    const auto beforePopulate=saved();populateSettings();pump(450);
    check("configuration population does not write incidental changes",saved()==beforePopulate&&pendingSettings_.empty());
    // Block only this test's temporary output file to exercise a real write failure.
    auto temporary=file;temporary+=L".tmp";fs::create_directory(temporary);
    click(KeepSoftLaugh);
    check("failed disk write keeps saved configuration and reports failure",saved()["keep_soft_laugh"]==true&&cfg_["keep_soft_laugh"]==true&&saveErrorControl_==KeepSoftLaugh);
    fs::remove(temporary);flushPendingSettings();
    check("failed write can be retried without repeating the change",saved()["keep_soft_laugh"]==false&&saveErrorControl_==0);
    click(KeepWhisper);
    selectPage(0);click(Extract);choose(OutputKind,2);
    check("mode and mandatory V4 review persist together",saved()["mode"]=="extract"&&saved()["review_enabled"]==true&&saved()["output_kind"]=="video");
    auto expected=saved();enableControls(true);click(Reset);click(KeepTapping);choose(Language,4);
    check("busy task cannot change saved preferences",saved()==expected);
    enableControls(false);populateSettings();
    MainWindow reloaded(root_,{{"test-config",Utf8(file.wstring())},{"test-autosave",options_["test-autosave"]}});
    check("fresh application loads automatically saved preferences",reloaded.cfg_["mode"]=="extract"&&reloaded.cfg_["language"]=="ja"&&reloaded.cfg_["audio_output_codec"]=="aac"&&reloaded.cfg_["keep_soft_laugh"]==false&&reloaded.cfg_["keep_whisper"]==false);
    notice_=false;status_.clear();selectPage(3,0);screenshot(report.parent_path()/L"preferences-autosave.png");
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(report,{{"passed",passed},{"checks",checks}});completed_=passed;finishTest(passed?0:1);
}

void MainWindow::testSwitches() {
    nlohmann::json checks=nlohmann::json::array();
    auto check=[&](const char* name,bool passed){checks.push_back({{"name",name},{"passed",passed}});};
    auto click=[&](int id){SendMessageW(control(id),BM_CLICK,0,0);};
    auto pump=[&](DWORD milliseconds) {
        auto until=GetTickCount64()+milliseconds;
        do {
            MSG message{};
            while(PeekMessageW(&message,nullptr,0,0,PM_REMOVE)){TranslateMessage(&message);DispatchMessageW(&message);}
            auto now=GetTickCount64();if(now>=until)break;
            MsgWaitForMultipleObjects(0,nullptr,FALSE,static_cast<DWORD>(until-now),QS_ALLINPUT);
        }while(true);
    };
    const auto folder=std::filesystem::path(Wide(options_["test-switches"])).parent_path();
    cfg_["mode"]="relaxed";selectPage(3,0);enableControls(false);
    // Exercise actual child timers without taking focus or showing a user window.
    SetWindowPos(window_,nullptr,-32000,-32000,d(1100),d(800),SWP_NOZORDER|SWP_NOACTIVATE);ShowWindow(window_,SW_SHOWNOACTIVATE);
    SendMessageW(control(FadeEnabled),BM_SETCHECK,BST_UNCHECKED,0);
    check("programmatic initialization has no animation",ToggleVisualPosition(control(FadeEnabled))==0.);
    screenshot(folder/L"switch-off.png");
    BOOL animations=TRUE;SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION,0,&animations,0);
    click(FadeEnabled);
    check("switch state and dependent field update immediately",SendMessageW(control(FadeEnabled),BM_GETCHECK,0,0)==BST_CHECKED&&IsWindowEnabled(control(FadeSeconds)));
    if(animations) {
        pump(40);double middle=ToggleVisualPosition(control(FadeEnabled));
        check("thumb passes through intermediate positions",middle>0.&&middle<1.);
        screenshot(folder/L"switch-moving.png");
        // Test continuity at the reversal itself; synchronous settings I/O
        // legitimately advances wall-clock animation before BM_CLICK returns.
        double before=ToggleVisualPosition(control(FadeEnabled));ToggleChecked(control(FadeEnabled));
        check("rapid reversal starts at the current thumb position",std::abs(ToggleVisualPosition(control(FadeEnabled))-before)<.06&&SendMessageW(control(FadeEnabled),BM_GETCHECK,0,0)==BST_UNCHECKED);
        pump(35);check("reversed thumb travels toward off",ToggleVisualPosition(control(FadeEnabled))<before);
        pump(220);check("animation settles at exact off endpoint",ToggleVisualPosition(control(FadeEnabled))==0.);
        click(FadeEnabled);pump(220);check("animation settles at exact on endpoint",ToggleVisualPosition(control(FadeEnabled))==1.);
    } else check("system disabled animations are respected",ToggleVisualPosition(control(FadeEnabled))==1.);
    screenshot(folder/L"switch-on.png");
    for(UINT dpi:{96u,144u,192u}) {
        int width=MulDiv(360,dpi,96),height=MulDiv(40,dpi,96);
        HDC dc=GetDC(window_),memory=CreateCompatibleDC(dc);auto bitmap=CreateCompatibleBitmap(dc,width,height);auto old=SelectObject(memory,bitmap);
        DRAWITEMSTRUCT draw{};draw.CtlType=ODT_BUTTON;draw.CtlID=FadeEnabled;draw.hwndItem=control(FadeEnabled);draw.hDC=memory;draw.rcItem={0,0,width,height};draw.itemState=ODS_FOCUS|ODS_SELECTED;
        DrawSwitchControl(&draw,font_,dpi);
        check("focused switch keeps white row background",GetPixel(memory,2,2)==UiTheme::White&&GetPixel(memory,width/2,2)==UiTheme::White);
        int blended=0;
        for(int x=width-MulDiv(48,dpi,96);x<width;++x)for(int y=0;y<height;++y){auto color=GetPixel(memory,x,y);if(color!=UiTheme::White&&color!=UiTheme::Accent)++blended;}
        check("switch curves have antialiased edge pixels at each DPI",blended>10);
        SelectObject(memory,old);DeleteObject(bitmap);DeleteDC(memory);ReleaseDC(window_,dc);
    }
    click(FadeEnabled);selectPage(3,1);
    check("hidden switches finish animation without stale visuals",ToggleVisualPosition(control(FadeEnabled))==0.);
    selectPage(3,0);click(FadeEnabled);enableControls(true);
    check("disabled switches settle on the logical state",ToggleVisualPosition(control(FadeEnabled))==1.&&!IsWindowEnabled(control(FadeEnabled)));
    enableControls(false);click(FadeEnabled);SendMessageW(control(FadeEnabled),BM_SETCHECK,BST_CHECKED,0);
    check("programmatic reset cancels in-flight animation",ToggleVisualPosition(control(FadeEnabled))==1.);
    bool soft=SendMessageW(control(KeepSoftLaugh),BM_GETCHECK,0,0)==BST_CHECKED;click(KeepSoftLaugh);
    check("sound checkboxes retain immediate state changes",(SendMessageW(control(KeepSoftLaugh),BM_GETCHECK,0,0)==BST_CHECKED)==!soft&&ToggleVisualPosition(control(KeepSoftLaugh))==(!soft?1.:0.));
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(std::filesystem::path(Wide(options_["test-switches"])),{{"passed",passed},{"checks",checks}});
    completed_=passed;finishTest(passed?0:1);
}
