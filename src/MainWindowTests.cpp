#include "MainWindow.h"
#include <commctrl.h>
#include <algorithm>

void MainWindow::testProgramMenu() {
    using json=nlohmann::json;
    json checks=json::array();
    auto check=[&](const char* name,bool passed){checks.push_back({{"name",name},{"passed",passed}});};
    auto visible=[&](int id){return (GetWindowLongPtrW(control(id),GWL_STYLE)&WS_VISIBLE)!=0;};
    const auto folder=std::filesystem::path(Wide(options_["test-menu"])).parent_path();
    SetWindowPos(window_,nullptr,0,0,d(1100),d(800),SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE);
    selectPage(3,3);
    check("menu preference is a separate page",visible(MenuEnabled)&&!visible(Audit)&&!visible(KeepSoftLaugh)&&!visible(ProxyUrl));
    const auto language=cfg_["language"];text(Silence,L"unfinished value");
    SendMessageW(control(MenuEnabled),BM_SETCHECK,BST_UNCHECKED,0);SendMessageW(control(Save),BM_CLICK,0,0);
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
    for(int id:{SpeechChoice,ReviewChoice,Language,Device,OutputKind}) {
        if(id==OutputKind)selectPage(0);
        else if(id==SpeechChoice||id==ReviewChoice){environmentTab_=1;selectPage(2);}else selectPage(3,0);
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
    auto visible=[&](int id) {return (GetWindowLongPtrW(control(id),GWL_STYLE)&WS_VISIBLE)!=0;};
    auto checked=[&](int id) {return SendMessageW(control(id),BM_GETCHECK,0,0)==BST_CHECKED;};
    selectPage(0);SendMessageW(control(OutputKind),CB_SETCURSEL,2,0);readSettings();
    check("video output choice reaches job settings",cfg_["output_kind"]=="video");
    populateSettings();check("output choice round trip",SendMessageW(control(OutputKind),CB_GETCURSEL,0,0)==2);
    enableControls(true);check("output type cannot change during task",!IsWindowEnabled(control(OutputKind)));enableControls(false);
    SendMessageW(control(OutputKind),CB_SETCURSEL,0,0);readSettings();
    selectPage(2);click(EnvironmentModels);
    check("model selectors are on the model page",visible(SpeechChoice)&&visible(ReviewChoice)&&!visible(RepairPython));
    SendMessageW(control(SpeechChoice),CB_SETCURSEL,1,0);SendMessageW(control(ReviewChoice),CB_SETCURSEL,0,0);readModelSettings();
    check("Qwen selection is saved independently from full Whisper review",cfg_["speech_model"]=="qwen3-asr"&&cfg_["review_model_id"]=="whisper-large-v3");
    populateSettings();check("model selectors round trip",SendMessageW(control(SpeechChoice),CB_GETCURSEL,0,0)==1);
    enableControls(true);check("model selection disabled during work",!IsWindowEnabled(control(SpeechChoice))&&!IsWindowEnabled(control(ReviewChoice)));enableControls(false);
    click(EnvironmentBase);check("base environment is a separate tab",!visible(SpeechChoice)&&environmentTab_==0);
    SendMessageW(control(SpeechChoice),CB_SETCURSEL,0,0);readModelSettings();
    wchar_t title[128]{};GetWindowTextW(window_,title,128);
    check("window name and icon",std::wstring(title)==L"ASMR-Cliper"&&GetClassLongPtrW(window_,GCLP_HICON)!=0&&GetClassLongPtrW(window_,GCLP_HICONSM)!=0);
    selectPage(3,0);
    SendMessageW(control(Language),CB_SETCURSEL,2,0);
    SendMessageW(control(Device),CB_SETCURSEL,2,0);
    bool audit=checked(Audit);click(Audit);readSettings();
    check("dropdown selections reach task settings",cfg_["language"]=="ja"&&cfg_["device"]=="cpu");
    check("dropdown exposes current label",value(Language)==L"日语"&&value(Device)==L"CPU");
    check("post-export review toggle reaches task settings",cfg_["review_enabled"]==!audit);
    populateSettings(0);
    check("dropdown and toggle round trip",SendMessageW(control(Language),CB_GETCURSEL,0,0)==2&&checked(Audit)==!audit);
    for(int id:{Silence,SilenceDb,Before,After,Minimum,DenseGap}) {
        auto frame=inputFrames_.at(id);POINT point{frame.left+d(5),frame.top+d(5)};
        auto before=value(id);SetFocus(control(SettingsAudio));
        SendMessageW(window_,WM_LBUTTONDOWN,MK_LBUTTON,MAKELPARAM(point.x,point.y));
        SendMessageW(control(id),WM_LBUTTONUP,0,0);
        check(("input padding focuses without changing value "+std::to_string(id)).c_str(),GetFocus()==control(id)&&value(id)==before);
    }
    selectPage(3,1);
    text(ProxyUrl,L"http://127.0.0.1:10886");
    if(checked(ProxyEnabled))click(ProxyEnabled);
    readSettings();check("proxy off disables address",!IsWindowEnabled(control(ProxyUrl))&&cfg_["proxy_enabled"]==false);
    click(ProxyEnabled);readSettings();
    check("proxy on restores address",IsWindowEnabled(control(ProxyUrl))&&cfg_["proxy_enabled"]==true&&cfg_["proxy_url"]=="http://127.0.0.1:10886");
    enableControls(true);
    check("busy state exposes cancel and disables editing",visible(Progress)&&visible(Cancel)&&!IsWindowEnabled(control(ProxyEnabled))&&!IsWindowEnabled(control(ProxyUrl))&&!IsWindowEnabled(control(Start)));
    enableControls(false);
    check("idle state hides task progress",!visible(Progress)&&!visible(Cancel)&&IsWindowEnabled(control(Start)));
    click(SettingsSounds);
    check("sound options are on their own page",settingsTab_==2&&visible(KeepSoftLaugh)&&!visible(Audit)&&!visible(ProxyUrl));
    click(Reset);
    check("soft laughter heartbeat and prop tapping are retained by default",checked(KeepSoftLaugh)&&checked(KeepHeartbeat)&&checked(KeepTapping)&&!checked(KeepLoudLaugh)&&!checked(KeepVaping)&&!checked(KeepDrinking)&&!checked(KeepImpacts));
    check("airflow label no longer identifies a device",value(KeepVaping)==L"呼气/烟雾");
    auto language=cfg_["language"],proxy=cfg_["proxy_url"];
    text(Silence,L"invalid hidden field");
    for(auto [id,key]:SoundOptions){bool before=checked(id);click(id);click(Save);check((std::string("sound checkbox saves ")+key).c_str(),cfg_[key]==!before);}
    populateSettings(2);
    check("retention settings round trip",!checked(KeepSoftLaugh)&&!checked(KeepHeartbeat)&&!checked(KeepTapping)&&checked(KeepLoudLaugh)&&checked(KeepVaping)&&checked(KeepDrinking)&&checked(KeepImpacts));
    click(Reset);
    check("sound reset does not modify other settings",cfg_["language"]==language&&cfg_["proxy_url"]==proxy&&value(Silence)==L"invalid hidden field");
    populateSettings(0);enableControls(true);
    check("retention options disabled during task",!IsWindowEnabled(control(KeepSoftLaugh))&&!IsWindowEnabled(control(KeepHeartbeat))&&!IsWindowEnabled(control(KeepTapping))&&!IsWindowEnabled(control(KeepImpacts)));
    enableControls(false);
    selectPage(1);
    check("empty history only offers new task",visible(NewTask)&&!visible(History)&&!visible(Play)&&!visible(Mapping)&&!visible(OpenOutput));
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
