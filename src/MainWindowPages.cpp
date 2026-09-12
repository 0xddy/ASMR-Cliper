#include "MainWindow.h"
#include "UiControls.h"
#include "UiTheme.h"
#include <uxtheme.h>
#include <commctrl.h>
#include <algorithm>
#include <sstream>

namespace fs = std::filesystem;
namespace {
using namespace UiTheme;
void Rounded(HDC dc,RECT r,COLORREF fill,COLORREF border,int radius=14) {
    auto b=CreateSolidBrush(fill);auto p=CreatePen(PS_SOLID,1,border);
    auto oldB=SelectObject(dc,b);auto oldP=SelectObject(dc,p);
    RoundRect(dc,r.left,r.top,r.right,r.bottom,radius,radius);
    SelectObject(dc,oldB);SelectObject(dc,oldP);DeleteObject(b);DeleteObject(p);
}
std::wstring Number(double v) {std::wostringstream out;out<<v;return out.str();}
std::wstring ClockTime(double seconds) {
    int n=static_cast<int>(seconds);wchar_t text[32]{};swprintf_s(text,L"%02d:%02d:%02d",n/3600,(n/60)%60,n%60);return text;
}
struct EnvironmentRow {const char* key;const wchar_t* name;int repair;int tab;};
const EnvironmentRow EnvironmentRows[]={
    {"python",L"Python 运行时",RepairPython,0},{"dependencies",L"分析依赖",RepairDependencies,0},
    {"ast",L"基础声音分类",RepairAst,0},{"ffmpeg",L"FFmpeg",RepairFfmpeg,0},{"gpu",L"GPU 加速",0,0},
    {"review",L"Whisper large-v3",RepairReview,1},{"qwen",L"Qwen3-ASR-1.7B",RepairQwen,1},
    {"aligner",L"Qwen 时间定位",RepairAligner,1},{"whisper",L"Whisper large-v3-turbo",RepairWhisper,1},
    {"clap",L"ASMR 声音识别 · CLAP",RepairClap,1},{"neural",L"Qwen / ASMR 识别依赖",RepairNeural,1}};
struct EnvironmentLayout {
    int top, rowHeight, count;
    int rowTop(int index) const {return top+34+index*rowHeight;}
    int bottom() const {return rowTop(count)+8;}
};
EnvironmentLayout EnvironmentGeometry(int tab,int height) {
    int top=tab?270:256,count=tab?6:5;
    return {top,std::clamp((height-80-top-42)/count,60,68),count};
}
const EnvironmentRow* EnvironmentRepair(int id) {for(const auto& row:EnvironmentRows)if(row.repair==id)return &row;return nullptr;}

}

bool MainWindow::testing() const {
    return options_.contains("snapshot")||options_.contains("self-test")||options_.contains("test-job")||options_.contains("test-action")||options_.contains("test-navigation")||options_.contains("test-controls")||options_.contains("test-dropdowns")||options_.contains("test-progress")||options_.contains("test-menu");
}

void MainWindow::createControls() {
    auto add=[&](int id,int group,LPCWSTR cls,const std::wstring& name,DWORD style,DWORD ex=0) {
        auto hwnd=CreateWindowExW(ex,cls,name.c_str(),WS_CHILD|style,0,0,0,0,window_,reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),instance_,nullptr);
        if(!hwnd) throw std::runtime_error("Cannot create GUI control.");
        controls_[id]=hwnd;groups_[id]=group;return hwnd;
    };
    auto button=[&](int id,int group,const wchar_t* label) {add(id,group,L"BUTTON",label,WS_TABSTOP|BS_OWNERDRAW);};
    button(NavTask,-1,L"剪辑任务");button(NavHistory,-1,L"处理记录");button(NavEnvironment,-1,L"运行环境");button(NavSettings,-1,L"偏好设置");button(NavLogs,-1,L"运行日志");
    add(Input,0,L"EDIT",Wide(cfg_["input"]),WS_TABSTOP|ES_AUTOHSCROLL);
    add(Output,0,L"EDIT",Wide(cfg_["output_dir"]),WS_TABSTOP|ES_AUTOHSCROLL);
    button(BrowseInput,0,L"选择文件");button(BrowseOutput,0,L"更改目录");
    button(OutputKind,0,L"");InitChoiceControl(control(OutputKind));
    for(auto label:{L"跟随输入",L"仅音频",L"视频（原编码）"})SendMessageW(control(OutputKind),CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label));
    button(Strict,0,L"严格模式  V2");button(Relaxed,0,L"宽松模式  V3");button(Extract,0,L"提取模式  V4");
    
    button(Start,0,L"开始剪辑");button(EditSettings,0,L"剪辑参数");button(Prompt,0,L"模式说明");
    add(History,1,L"LISTBOX",L"",WS_TABSTOP|WS_VSCROLL|LBS_OWNERDRAWFIXED|LBS_HASSTRINGS|LBS_NOTIFY|LBS_NOINTEGRALHEIGHT);
    button(NewTask,1,L"新建剪辑");button(Play,1,L"播放");button(Mapping,1,L"时间对照");button(OpenOutput,1,L"结果目录");
    button(ReviewFindings,1,L"待复听位置");
    button(ProgramMenu,1,L"生成节目单");
    button(Doctor,2,L"检测环境");button(Install,2,L"补齐环境");button(NetworkSettings,2,L"代理设置");
    for(const auto& row:EnvironmentRows)if(row.repair)button(row.repair,20+row.tab,L"下载 / 修复");
    button(EnvironmentModels,2,L"模型选择");button(EnvironmentBase,2,L"基础环境");
    button(SpeechChoice,21,L"");InitChoiceControl(control(SpeechChoice));
    button(ReviewChoice,21,L"");InitChoiceControl(control(ReviewChoice));
    for(auto label:{L"Whisper large-v3",L"Qwen3-ASR-1.7B",L"Whisper large-v3-turbo"})SendMessageW(control(SpeechChoice),CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label));
    for(auto label:{L"Whisper large-v3",L"Qwen3-ASR-1.7B"})SendMessageW(control(ReviewChoice),CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label));
    button(SettingsAudio,3,L"剪辑参数");button(SettingsSounds,3,L"保留声音");button(SettingsNetwork,3,L"网络代理");button(Save,3,L"保存设置");button(Reset,3,L"恢复默认");
    button(SettingsMenu,3,L"成片节目单");button(MenuEnabled,33,L"剪辑完成后自动识别节目单");InitToggleControl(control(MenuEnabled));
    button(MenuModels,33,L"声音模型设置");
    const wchar_t* sounds[]={L"轻笑",L"心跳",L"ASMR 道具敲击",L"大笑",L"呼气/烟雾",L"喝水休息",L"物品掉落 / 突兀撞击"};
    int soundIndex=0;
    for(auto [id,key]:SoundOptions){button(id,32,sounds[soundIndex++]);InitToggleControl(control(id));}
    button(Language,30,L"");InitChoiceControl(control(Language));
    button(Device,30,L"");InitChoiceControl(control(Device));
    for(auto label:{L"自动识别",L"韩语",L"日语",L"中文",L"英语"}) SendMessageW(control(Language),CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label));
    for(auto label:{L"自动选择",L"NVIDIA GPU",L"CPU"}) SendMessageW(control(Device),CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label));
    for(int id:{Silence,SilenceDb,Before,After,Minimum,DenseGap}) add(id,30,L"EDIT",L"",WS_TABSTOP|ES_AUTOHSCROLL);
    button(Audit,30,L"成片大模型复核（V4 必须开启）");InitToggleControl(control(Audit));
    button(ProxyEnabled,31,L"使用代理");InitToggleControl(control(ProxyEnabled));
    add(ProxyUrl,31,L"EDIT",L"",WS_TABSTOP|ES_AUTOHSCROLL);
    button(TestProxy,31,L"测试连接");
    add(Log,4,L"EDIT",L"",WS_TABSTOP|WS_VSCROLL|ES_MULTILINE|ES_READONLY|ES_AUTOVSCROLL);
    button(OpenLogs,4,L"日志目录");button(ClearLog,4,L"清空");SendMessageW(control(Log),EM_SETLIMITTEXT,180000,0);
    button(Cancel,-1,L"取消任务");
    add(Progress,-1,PROGRESS_CLASSW,L"",PBS_SMOOTH);SendMessageW(control(Progress),PBM_SETRANGE32,0,1000);SetWindowTheme(control(Progress),L"",L"");SendMessageW(control(Progress),PBM_SETBARCOLOR,0,Accent);SendMessageW(control(Progress),PBM_SETBKCOLOR,0,White);
    setFonts();populateSettings();updateHistory();enableControls(false);
    SendMessageW(window_,WM_CHANGEUISTATE,MAKEWPARAM(UIS_SET,UISF_HIDEFOCUS),0);
    appendLog(L"ASMR-Cliper 0.6.5");
    selectPage(page_);
}

void MainWindow::populateSettings(int tab) {
    const std::vector<std::string> outputs{"auto","audio","video"};
    auto output=std::find(outputs.begin(),outputs.end(),cfg_.value("output_kind","auto"));
    SendMessageW(control(OutputKind),CB_SETCURSEL,output==outputs.end()?0:output-outputs.begin(),0);
    const std::vector<std::string> models{"whisper-large-v3","qwen3-asr","whisper-turbo"};
    for(auto [id,key]:std::vector<std::pair<int,const char*>>{{SpeechChoice,"speech_model"},{ReviewChoice,"review_model_id"}}) {
        auto selected=std::find(models.begin(),models.end(),cfg_.value(key,"whisper-large-v3"));
        SendMessageW(control(id),CB_SETCURSEL,selected==models.end()?0:selected-models.begin(),0);
    }
    if(tab<0||tab==0) {
    const std::vector<std::string> languages{"auto","ko","ja","zh","en"},devices{"auto","cuda","cpu"};
    auto choose=[&](int id,const auto& choices,const std::string& selected) {auto it=std::find(choices.begin(),choices.end(),selected);SendMessageW(control(id),CB_SETCURSEL,it==choices.end()?0:it-choices.begin(),0);};
    choose(Language,languages,cfg_.value("language","auto"));choose(Device,devices,cfg_.value("device","auto"));
    for(auto [id,key]:std::vector<std::pair<int,const char*>>{{Silence,"silence_seconds"},{SilenceDb,"silence_db"},{Before,"strict_pre"},{After,"strict_post"},{Minimum,"strict_min_section"},{DenseGap,"strict_dense_gap"}}) text(id,Number(cfg_.value(key,0.)));
    SendMessageW(control(Audit),BM_SETCHECK,cfg_.value("review_enabled",true)?BST_CHECKED:BST_UNCHECKED,0);
    }
    if(tab<0||tab==1) {
    SendMessageW(control(ProxyEnabled),BM_SETCHECK,cfg_.value("proxy_enabled",true)?BST_CHECKED:BST_UNCHECKED,0);
    text(ProxyUrl,Wide(cfg_.value("proxy_url","http://127.0.0.1:10886")));
    }
    if(tab<0||tab==2) for(auto [id,key]:SoundOptions)
        SendMessageW(control(id),BM_SETCHECK,cfg_.value(key,id==KeepSoftLaugh||id==KeepHeartbeat||id==KeepTapping)?BST_CHECKED:BST_UNCHECKED,0);
    if(tab<0||tab==3)SendMessageW(control(MenuEnabled),BM_SETCHECK,cfg_.value("generate_program_menu",true)?BST_CHECKED:BST_UNCHECKED,0);
    refreshMode();
}

std::vector<std::string> MainWindow::requiredComponents() const {
    std::vector<std::string> result{"python","dependencies","ast","ffmpeg"};
    auto add=[&](std::string model) {
        if(model=="qwen3-asr"){result.push_back("qwen");result.push_back("aligner");result.push_back("neural");}
        else result.push_back(model=="whisper-turbo"?"whisper":"review");
    };
    add(cfg_.value("speech_model","whisper-large-v3"));
    if(cfg_.value("review_enabled",true)||cfg_.value("mode","")=="extract")add(cfg_.value("review_model_id","whisper-large-v3"));
    if(cfg_.value("mode","")=="extract"){result.push_back("clap");result.push_back("neural");}
    return result;
}

bool MainWindow::environmentReady() const {
    for(const auto& key:requiredComponents()){auto found=components_.find(key);if(found==components_.end()||found->second.value("status","")!="ready")return false;}
    return true;
}

void MainWindow::updateVisibility() {
    for(auto [id,hwnd]:controls_) {
        int group=groups_[id];bool visible=group==-1||group==page_||(page_==3&&group==30+settingsTab_)||(page_==2&&group==20+environmentTab_);
        if(id==Cancel||id==Progress)visible=busy_;
        if(id==History||id==Play||id==Mapping||id==OpenOutput||id==ProgramMenu)visible=visible&&!history_.empty();
        if(id==NewTask)visible=visible&&history_.empty();
        if(id==ReviewFindings)visible=visible&&!history_.empty()&&lastResult_.value("speech_review",nlohmann::json::object()).value("status","")=="needs_review";
        if(auto row=EnvironmentRepair(id)) {
            auto found=components_.find(row->key);
            visible=visible&&(found==components_.end()||found->second.value("status","")!="ready");
        }
        if(id==NetworkSettings)visible=visible&&environmentTab_==0;
        if(id==Install)visible=visible&&(!environmentReady()||(busy_&&activeAction_=="install"));
        ShowWindow(hwnd,visible?SW_SHOW:SW_HIDE);
    }
}

void MainWindow::selectPage(int page,int tab) {
    page_=std::clamp(page,0,4);if(tab>=0) settingsTab_=std::clamp(tab,0,3);
    updateVisibility();
    if(!testing())SetFocus(control(NavTask+page_));
    layout();
    for(int id:{NavTask,NavHistory,NavEnvironment,NavSettings,NavLogs,SettingsAudio,SettingsNetwork,SettingsSounds,SettingsMenu,EnvironmentBase,EnvironmentModels})InvalidateRect(control(id),nullptr,FALSE);
}

void MainWindow::showChoices(int id) {
    if(!busy_)ShowChoiceMenu(window_,control(id),font_,dpi_);
}

void MainWindow::layout() {
    RECT rect{};GetClientRect(window_,&rect);int w=MulDiv(rect.right,96,dpi_),h=MulDiv(rect.bottom,96,dpi_);
    const int x=224,r=w-32,cw=r-x,third=(cw-32)/3,col=(cw-64)/2;
    auto place=[&](int id,int a,int b,int width,int height) {MoveWindow(control(id),d(a),d(b),d(std::max(width,1)),d(std::max(height,1)),TRUE);};
    HDC dc=GetDC(window_);auto oldFont=SelectObject(dc,font_);TEXTMETRICW metrics{};GetTextMetricsW(dc,&metrics);SelectObject(dc,oldFont);ReleaseDC(window_,dc);
    auto field=[&](int id,int a,int b,int width) {
        RECT frame{d(a),d(b),d(a+width),d(b+40)};inputFrames_[id]=frame;
        int top=frame.top+(frame.bottom-frame.top-metrics.tmHeight)/2;
        MoveWindow(control(id),frame.left+d(12),top,frame.right-frame.left-d(24),metrics.tmHeight,TRUE);
        SendMessageW(control(id),EM_SETMARGINS,EC_LEFTMARGIN|EC_RIGHTMARGIN,MAKELPARAM(0,0));
    };
    for(int i=0;i<5;++i)place(NavTask+i,16,112+52*i,168,44);
    field(Input,x+24,142,cw-184);place(BrowseInput,r-144,142,120,40);
    field(Output,x+24,226,cw-184);place(BrowseOutput,r-144,226,120,40);
    place(OutputKind,x+24,310,236,40);
    place(Prompt,r-112,394,112,36);place(Strict,x,444,third,92);place(Relaxed,x+third+16,444,third,92);place(Extract,x+2*(third+16),444,r-x-2*(third+16),92);
    place(Start,x,568,176,40);place(EditSettings,x+188,568,124,40);
    place(NewTask,x+(cw-124)/2,365,124,40);
    place(History,x+16,112,cw-32,h-315);
    place(Play,x+24,h-176,100,40);place(Mapping,x+136,h-176,112,40);place(OpenOutput,x+260,h-176,124,40);
    place(ReviewFindings,x+396,h-176,136,40);
    place(ProgramMenu,x+544,h-176,136,40);
    bool install=GetWindowLongPtrW(control(Install),GWL_STYLE)&WS_VISIBLE;
    place(Doctor,r-(install?240:112),28,112,40);place(Install,r-116,28,116,40);place(NetworkSettings,r-124,186,100,36);
    place(EnvironmentModels,x+4,98,126,36);place(EnvironmentBase,x+134,98,126,36);
    place(SpeechChoice,x+24,198,col,40);place(ReviewChoice,x+40+col,198,col,40);
    auto environmentLayout=EnvironmentGeometry(environmentTab_,h);
    int rowIndex=0;for(const auto& row:EnvironmentRows)if(row.tab==environmentTab_) {
        if(row.repair)place(row.repair,r-136,environmentLayout.rowTop(rowIndex)+(environmentLayout.rowHeight-38)/2,112,38);
        ++rowIndex;
    }
    place(SettingsAudio,x+4,98,126,36);place(SettingsSounds,x+134,98,126,36);place(SettingsNetwork,x+264,98,126,36);
    place(SettingsMenu,x+394,98,126,36);place(MenuEnabled,x+24,184,cw-48,40);place(MenuModels,x+24,322,136,38);
    int soundRow=0;for(auto [id,key]:SoundOptions)place(id,x+24,242+soundRow++*44,cw-48,40);
    place(Language,x+24,240,col,40);place(Device,x+40+col,240,col,40);
    field(Silence,x+24,324,col);field(SilenceDb,x+40+col,324,col);place(Audit,x+24,384,cw-48,32);
    int cell=(cw-96)/4;
    for(int i=0;i<4;++i)field(Before+i,x+24+i*(cell+16),536,cell);
    place(ProxyEnabled,x+24,184,cw-48,40);field(ProxyUrl,x+24,268,cw-184);place(TestProxy,r-144,268,120,40);
    int actionY=settingsTab_==0?624:settingsTab_==2?586:settingsTab_==3?408:proxyTested_?564:356;
    place(Save,x,actionY,124,40);place(Reset,x+136,actionY,112,40);
    place(OpenLogs,r-224,28,112,40);place(ClearLog,r-100,28,100,40);place(Log,x+20,116,cw-40,h-228);
    place(Cancel,r-100,h-58,100,36);place(Progress,x,h-7,cw,3);
    InvalidateRect(window_,nullptr,TRUE);
}

HWND MainWindow::inputAt(POINT point) const {
    for(const auto& [id,frame]:inputFrames_) {
        auto child=control(id);
        if((GetWindowLongPtrW(child,GWL_STYLE)&WS_VISIBLE)&&IsWindowEnabled(child)&&PtInRect(&frame,point))return child;
    }
    return nullptr;
}

void MainWindow::paint(HDC dc) {
    RECT b{};GetClientRect(window_,&b);FillRect(dc,&b,background_);
    int w=MulDiv(b.right,96,dpi_),h=MulDiv(b.bottom,96,dpi_),x=224,r=w-32,cw=r-x;
    auto label=[&](const std::wstring& s,int a,int y,int width,int height,HFONT font,COLORREF color=Ink,UINT flags=DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS) {
        RECT q{d(a),d(y),d(a+width),d(y+height)};SelectObject(dc,font);SetTextColor(dc,color);SetBkMode(dc,TRANSPARENT);DrawTextW(dc,s.c_str(),-1,&q,flags);
    };
    auto card=[&](int top,int bottom) {Rounded(dc,{d(x),d(top),d(r),d(bottom)},White,Line,d(16));};
    auto line=[&](int a,int y,int right) {auto pen=CreatePen(PS_SOLID,1,Line);auto old=SelectObject(dc,pen);MoveToEx(dc,d(a),d(y),nullptr);LineTo(dc,d(right),d(y));SelectObject(dc,old);DeleteObject(pen);};
    RECT side{0,0,d(200),b.bottom};FillRect(dc,&side,white_);
    auto icon=LoadIconW(instance_,MAKEINTRESOURCEW(101));if(icon)DrawIconEx(dc,d(22),d(32),icon,d(24),d(24),0,nullptr,DI_NORMAL);
    label(L"ASMR-Cliper",54,28,142,32,brandFont_);label(L"v0.6.5",24,h-43,140,20,smallFont_,Muted);
    const wchar_t* titles[]={L"剪辑任务",L"处理记录",L"运行环境",L"偏好设置",L"运行日志"};label(titles[page_],x,24,cw-260,42,titleFont_);
    if(page_==0) {
        card(96,374);label(L"音频 / 视频文件",x+24,110,cw-48,24,font_);label(L"输出目录",x+24,194,cw-48,24,font_);
        label(L"输出类型",x+24,278,236,24,font_);
        label(L"保留源编码 · 视频按关键帧向内调整切点",x+280,310,cw-304,40,smallFont_,Muted);
        label(L"剪辑模式",x,394,cw-140,36,boldFont_);
    } else if(page_==1) {
        if(history_.empty()) {
            int cx=x+cw/2;auto pen=CreatePen(PS_SOLID,d(2),RGB(184,199,199));auto oldP=SelectObject(dc,pen);auto oldB=SelectObject(dc,GetStockObject(NULL_BRUSH));
            RoundRect(dc,d(cx-22),d(256),d(cx+22),d(310),d(8),d(8));MoveToEx(dc,d(cx-11),d(274),nullptr);LineTo(dc,d(cx+11),d(274));MoveToEx(dc,d(cx-11),d(286),nullptr);LineTo(dc,d(cx+6),d(286));
            SelectObject(dc,oldP);SelectObject(dc,oldB);DeleteObject(pen);
            label(L"暂无剪辑记录",x,324,cw,28,boldFont_,Muted,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
        } else {card(96,h-112);line(x+24,h-192,r-24);label(std::to_wstring(history_.size())+L" 条记录",r-110,30,110,36,smallFont_,Muted,DT_RIGHT|DT_VCENTER|DT_SINGLELINE);}
    } else if(page_==2) {
        Rounded(dc,{d(x),d(94),d(x+264),d(138)},White,Line,d(14));
        if(environmentTab_==1) {
            card(154,254);
            int col=(cw-64)/2;
            label(L"语音识别",x+24,164,col,24,boldFont_);label(L"成片复核",x+40+col,164,col,24,boldFont_);
        } else {
            card(164,240);label(environmentReady()?L"环境已就绪":L"所选模型需要补齐环境",x+24,175,cw-180,28,boldFont_,Accent);
            label(cfg_.value("proxy_enabled",true)?L"代理已开启":L"直接连接",x+24,208,cw-180,22,smallFont_,Muted);
        }
        auto geometry=EnvironmentGeometry(environmentTab_,h);int top=geometry.top;
        card(top,geometry.bottom());
        label(L"组件",x+24,top+8,cw-180,22,smallFont_,Muted);label(L"状态",r-104,top+8,80,22,smallFont_,Muted,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
        auto required=requiredComponents();int index=0;
        for(const auto& row:EnvironmentRows)if(row.tab==environmentTab_) {
            int rowTop=geometry.rowTop(index++),y=rowTop+(geometry.rowHeight-46)/2;auto found=components_.find(row.key);auto state=found==components_.end()?"unknown":found->second.value("status","unknown");
            std::wstring detail=found==components_.end()?L"等待检测":Wide(found->second.value("detail",""));
            if(row.key==std::string("ffmpeg")){auto a=detail.find(L"version ");if(a!=std::wstring::npos){a+=8;auto end=detail.find_first_of(L" -",a);detail=L"FFmpeg "+detail.substr(a,end-a);}}
            bool needed=std::find(required.begin(),required.end(),row.key)!=required.end();
            if(state=="ready")label(L"已就绪",r-104,rowTop+(geometry.rowHeight-26)/2,80,26,smallFont_,Accent,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
            else if(!row.repair)label(L"可选",r-104,rowTop+(geometry.rowHeight-26)/2,80,26,smallFont_,Muted,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
            std::wstring name=row.name;if(needed&&environmentTab_)name+=L"  ·  当前使用";
            label(name,x+24,y,cw-188,25,font_);label(detail,x+24,y+25,cw-188,22,smallFont_,Muted);
            if(index<geometry.count)line(x+24,geometry.rowTop(index),r-24);
        }
    } else if(page_==3) {
        Rounded(dc,{d(x),d(94),d(x+524),d(138)},White,Line,d(14));
        if(settingsTab_==0) {
            int col=(cw-64)/2;card(164,436);label(L"分析参数",x+24,176,cw-48,28,boldFont_);
            label(L"语音语言",x+24,211,col,22,smallFont_,Muted);label(L"计算设备",x+40+col,211,col,22,smallFont_,Muted);
            label(L"长静音时长（秒）",x+24,294,col,22,smallFont_,Muted);label(L"静音电平（dB）",x+40+col,294,col,22,smallFont_,Muted);
            card(452,600);label(L"严格模式 V2",x+24,468,cw-48,28,boldFont_);
            const wchar_t* names[]={L"说话前余量（秒）",L"说话后余量（秒）",L"最短片段（秒）",L"聊天合并间隔（秒）"};int cell=(cw-96)/4;
            for(int i=0;i<4;++i)label(names[i],x+24+i*(cell+16),508,cell,22,smallFont_,Muted);
        } else if(settingsTab_==1) {
            card(164,332);label(L"代理地址",x+24,238,cw-48,22,smallFont_,Muted);
            if(proxyTested_) {
                card(348,540);label(proxyStatus_,x+24,362,cw-48,27,boldFont_,Ink);
                int row=0;for(const auto& result:proxyResults_) {
                    int y=400+row++*38;bool ok=result.value("ok",false);
                    label(Wide(result.value("name","")),x+24,y,cw-230,26,font_);
                    label(ok?std::to_wstring(result.value("milliseconds",0))+L" ms":L"连接失败",r-184,y,160,26,smallFont_,ok?Accent:RGB(178,72,61),DT_RIGHT|DT_VCENTER|DT_SINGLELINE);
                }
            }
        } else if(settingsTab_==2) {
            card(164,562);label(L"选择要保留的声音",x+24,176,cw-48,28,boldFont_);
            label(L"勾选表示允许保留 · 说话声始终删除 · V4 仍需 ASMR 证据",x+24,210,cw-48,22,smallFont_,Muted);
        } else {
            card(164,384);
            label(L"分析实际成片，按声音出现顺序生成带时间的节目单。",x+24,242,cw-48,26,font_);
            label(L"本地 CLAP 声音语义模型 · 不确定项目标为待确认",x+24,278,cw-48,22,smallFont_,Muted);
        }
    } else card(96,h-92);
    for(const auto& [id,frame]:inputFrames_) if(GetWindowLongPtrW(control(id),GWL_STYLE)&WS_VISIBLE)Rounded(dc,frame,IsWindowEnabled(control(id))?White:Bg,Line,d(12));
    if(busy_) {
        RECT footer{d(200),d(h-80),b.right,b.bottom};FillRect(dc,&footer,white_);line(200,h-80,w);
        label(status_,x,h-70,cw-124,28,font_);
        std::wstring detail=downloadStatus_;
        if((activeAction_=="run"||activeAction_=="menu")&&!taskProgress_.empty()) {
            detail=Wide(taskProgress_.value("detail",""));
            if(taskProgress_.value("percent",nlohmann::json()).is_number()) {
                detail+=L"   ·   "+std::wstring(taskProgress_.value("round",0)>0?L"本轮 ":L"本阶段 ")+std::to_wstring(static_cast<int>(taskProgress_["percent"].get<double>()))+L"%";
            }
        }
        label(detail,x,h-38,cw-294,22,smallFont_,Muted);
        if(hasTiming_)label(L"已用时 "+ClockTime(elapsedSeconds()),r-282,h-38,166,22,smallFont_,Muted);
    } else if(notice_&&!status_.empty()) {
        label(status_,x,h-56,cw-(hasTiming_?180:0),32,font_,Muted);
        if(hasTiming_)label(L"耗时 "+ClockTime(elapsedSeconds()),r-170,h-56,170,32,smallFont_,Muted);
    }
}

void MainWindow::drawButton(const DRAWITEMSTRUCT* item) {
    int id=static_cast<int>(item->CtlID);RECT r=item->rcItem;bool focus=(item->itemState&ODS_FOCUS)!=0,disabled=(item->itemState&ODS_DISABLED)!=0;
    SetBkMode(item->hDC,TRANSPARENT);
    if(id==History) {
        bool selected=(item->itemState&ODS_SELECTED)!=0;auto brush=CreateSolidBrush(White);FillRect(item->hDC,&r,brush);DeleteObject(brush);
        if(selected)Rounded(item->hDC,r,Soft,Soft,d(12));int index=static_cast<int>(item->itemID);
        if(index>=0&&static_cast<size_t>(index)<history_.size()) {
            const auto& row=history_[index];r.left+=d(16);r.right-=d(16);RECT title=r;title.top+=d(8);title.bottom=title.top+d(28);
            auto name=fs::path(Wide(row.value("output",""))).filename().wstring();SelectObject(item->hDC,font_);SetTextColor(item->hDC,Ink);DrawTextW(item->hDC,name.c_str(),-1,&title,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS);
            std::wstring sub=Wide(row.value("finished_at",""))+L"   ·   时长 "+ClockTime(row.value("duration",0.))+L"   ·   "+std::to_wstring(row.value("segments",0))+L" 段";
            if(row.contains("elapsed_seconds"))sub+=L"   ·   耗时 "+ClockTime(row.value("elapsed_seconds",0.));
            auto review=row.value("speech_review",nlohmann::json::object());auto reviewStatus=review.value("status","");
            sub+=reviewStatus=="passed"?L"   ·   模型复核通过":reviewStatus=="needs_review"?L"   ·   "+std::to_wstring(review.value("findings",nlohmann::json::array()).size())+L" 处待复听":L"   ·   未做成片复核";
            r.top+=d(41);SelectObject(item->hDC,smallFont_);SetTextColor(item->hDC,Muted);DrawTextW(item->hDC,sub.c_str(),-1,&r,DT_LEFT|DT_SINGLELINE|DT_END_ELLIPSIS);
        }return;
    }
    if(IsSoundOption(id)) {
        auto brush=CreateSolidBrush(focus&&!disabled?Bg:White);FillRect(item->hDC,&r,brush);DeleteObject(brush);
        bool checked=SendMessageW(control(id),BM_GETCHECK,0,0)==BST_CHECKED;
        int y=(r.top+r.bottom)/2;RECT box{r.left+d(2),y-d(10),r.left+d(22),y+d(10)};
        auto ink=disabled?Muted:Accent;Rounded(item->hDC,box,checked?ink:White,checked?ink:Line,d(6));
        if(checked){auto pen=CreatePen(PS_SOLID,d(2),White);auto old=SelectObject(item->hDC,pen);MoveToEx(item->hDC,box.left+d(5),y,nullptr);LineTo(item->hDC,box.left+d(9),y+d(4));LineTo(item->hDC,box.right-d(4),y-d(4));SelectObject(item->hDC,old);DeleteObject(pen);}
        r.left+=d(36);SelectObject(item->hDC,font_);SetTextColor(item->hDC,disabled?Muted:Ink);auto title=value(id);DrawTextW(item->hDC,title.c_str(),-1,&r,DT_LEFT|DT_VCENTER|DT_SINGLELINE);return;
    }
    bool toggle=id==Audit||id==ProxyEnabled||id==MenuEnabled;
    if(toggle) {
        auto brush=CreateSolidBrush(focus&&!disabled?Bg:White);FillRect(item->hDC,&r,brush);DeleteObject(brush);
        RECT label=r;label.right-=d(64);SelectObject(item->hDC,font_);SetTextColor(item->hDC,disabled?Muted:Ink);auto title=value(id);DrawTextW(item->hDC,title.c_str(),-1,&label,DT_LEFT|DT_VCENTER|DT_SINGLELINE);
        bool checked=SendMessageW(control(id),BM_GETCHECK,0,0)==BST_CHECKED;
        RECT track{r.right-d(44),r.top+(r.bottom-r.top-d(24))/2,r.right,r.top+(r.bottom-r.top+d(24))/2};
        auto color=checked&&!disabled?Accent:RGB(210,220,218);Rounded(item->hDC,track,color,color,d(24));
        RECT dot{checked?track.right-d(22):track.left+d(2),track.top+d(2),checked?track.right-d(2):track.left+d(22),track.top+d(22)};Rounded(item->hDC,dot,White,White,d(20));return;
    }
    bool nav=id>=NavTask&&id<=NavLogs,tabs=id==SettingsAudio||id==SettingsNetwork||id==SettingsSounds||id==SettingsMenu||id==EnvironmentBase||id==EnvironmentModels,mode=id==Strict||id==Relaxed||id==Extract,choice=id==Language||id==Device||id==SpeechChoice||id==ReviewChoice||id==OutputKind;
    bool selected=(id==Strict&&cfg_.value("mode","relaxed")=="strict")||(id==Relaxed&&cfg_.value("mode","relaxed")=="relaxed")||(id==Extract&&cfg_.value("mode","")=="extract")||(nav&&id-NavTask==page_)||(id==SettingsAudio&&settingsTab_==0)||(id==SettingsNetwork&&settingsTab_==1)||(id==SettingsSounds&&settingsTab_==2)||(id==SettingsMenu&&settingsTab_==3)||(id==EnvironmentModels&&environmentTab_==1)||(id==EnvironmentBase&&environmentTab_==0);
    bool onCard=nav||tabs||choice||id==BrowseInput||id==BrowseOutput||id==Play||id==Mapping||id==OpenOutput||id==ReviewFindings||id==ProgramMenu||id==MenuModels||id==NetworkSettings||id==TestProxy||EnvironmentRepair(id);
    auto base=CreateSolidBrush(onCard?White:Bg);FillRect(item->hDC,&r,base);DeleteObject(base);
    COLORREF fill=White,edge=nav||tabs?White:Line,ink=Ink;
    if(selected){fill=Soft;edge=nav||tabs?Soft:Accent;ink=Accent;}
    if(focus&&!selected&&!disabled){fill=RGB(237,243,242);edge=nav||tabs?fill:Line;}
    if(id==Start||id==Install||id==Save||id==NewTask){fill=disabled?RGB(164,187,181):Accent;edge=fill;ink=White;}
    else if(disabled){fill=Bg;edge=Line;ink=RGB(154,168,169);}
    else if(item->itemState&ODS_SELECTED)fill=RGB(215,235,230);
    Rounded(item->hDC,r,fill,edge,d(12));SetTextColor(item->hDC,ink);
    if(mode) {
        r.left+=d(20);r.right-=d(20);r.top+=d(15);r.bottom=r.top+d(27);SelectObject(item->hDC,boldFont_);auto title=value(id);DrawTextW(item->hDC,title.c_str(),-1,&r,DT_LEFT|DT_VCENTER|DT_SINGLELINE);
        r.top+=d(36);r.bottom=r.top+d(22);SelectObject(item->hDC,smallFont_);SetTextColor(item->hDC,Muted);
        DrawTextW(item->hDC,id==Strict?L"优先保留长段连续声音":id==Extract?L"仅提取 ASMR 证据明确的片段":L"根据声音上下文调整边界",-1,&r,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS);
        if(selected){RECT dot=item->rcItem;dot.left=dot.right-d(29);dot.right-=d(19);dot.top+=d(23);dot.bottom=dot.top+d(10);Rounded(item->hDC,dot,Accent,Accent,d(10));}
    } else if(choice) {
        int index=static_cast<int>(SendMessageW(control(id),CB_GETCURSEL,0,0));wchar_t label[256]{};if(index>=0)SendMessageW(control(id),CB_GETLBTEXT,index,reinterpret_cast<LPARAM>(label));
        r.left+=d(12);r.right-=d(38);SelectObject(item->hDC,font_);DrawTextW(item->hDC,label,-1,&r,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS);
        auto pen=CreatePen(PS_SOLID,d(1),disabled?Muted:Ink);auto old=SelectObject(item->hDC,pen);int a=item->rcItem.right-d(21),y=(item->rcItem.bottom+item->rcItem.top)/2;
        MoveToEx(item->hDC,a-d(4),y-d(2),nullptr);LineTo(item->hDC,a,y+d(2));LineTo(item->hDC,a+d(4),y-d(2));SelectObject(item->hDC,old);DeleteObject(pen);
    } else {
        SelectObject(item->hDC,font_);auto title=value(id);if(nav)r.left+=d(20);DrawTextW(item->hDC,title.c_str(),-1,&r,(nav?DT_LEFT:DT_CENTER)|DT_VCENTER|DT_SINGLELINE);
    }
}

void MainWindow::refreshMode() {
    bool extract=cfg_.value("mode","")=="extract";
    if(extract)SendMessageW(control(Audit),BM_SETCHECK,BST_CHECKED,0);
    EnableWindow(control(Audit),!busy_&&!extract);
    for(int id:{Strict,Relaxed,Extract})InvalidateRect(control(id),nullptr,FALSE);InvalidateRect(window_,nullptr,FALSE);
}

void MainWindow::enableControls(bool busy) {
    busy_=busy;
    EnableWindow(control(Extract),!busy);
    for(int id:{SpeechChoice,ReviewChoice,OutputKind})EnableWindow(control(id),!busy);
    for(const auto& row:EnvironmentRows)if(row.repair) {
        auto found=components_.find(row.key);EnableWindow(control(row.repair),!busy&&(found==components_.end()||found->second.value("status","")!="ready"));
    }
    for(auto [id,key]:SoundOptions)EnableWindow(control(id),!busy);
    for(int id:{Input,Output,BrowseInput,BrowseOutput,Strict,Relaxed,Language,Device,Silence,SilenceDb,Before,After,Minimum,DenseGap,Audit,Start,Doctor,Install,RepairPython,RepairDependencies,RepairWhisper,RepairAst,RepairFfmpeg,Save,Reset,ProxyEnabled,ProxyUrl,TestProxy}) EnableWindow(control(id),!busy);
    EnableWindow(control(Cancel),busy);
    for(int id:{Play,Mapping,OpenOutput,ReviewFindings}) EnableWindow(control(id),lastResult_.contains("output"));
    bool menuReady=lastResult_.value("program_menu",nlohmann::json::object()).value("status","")=="ready";
    text(ProgramMenu,menuReady?L"查看节目单":L"生成节目单");
    EnableWindow(control(ProgramMenu),lastResult_.contains("output")&&(!busy||menuReady));EnableWindow(control(MenuEnabled),!busy);
    EnableWindow(control(ProxyUrl),!busy&&SendMessageW(control(ProxyEnabled),BM_GETCHECK,0,0)==BST_CHECKED);
    updateVisibility();layout();refreshMode();
}

void MainWindow::updateHistory() {
    SendMessageW(control(History),LB_RESETCONTENT,0,0);
    for(const auto& row:history_) {auto name=Wide(row.value("output",""));SendMessageW(control(History),LB_ADDSTRING,0,reinterpret_cast<LPARAM>(name.c_str()));}
    SendMessageW(control(History),LB_SETITEMHEIGHT,0,d(76));
    if(!history_.empty()) {SendMessageW(control(History),LB_SETCURSEL,0,0);selectHistory();}
    updateVisibility();
}

void MainWindow::selectHistory() {
    int index=static_cast<int>(SendMessageW(control(History),LB_GETCURSEL,0,0));
    if(index>=0&&static_cast<size_t>(index)<history_.size()) lastResult_=history_[index];
    enableControls(busy_);InvalidateRect(window_,nullptr,FALSE);
}

void MainWindow::testNavigationRendering() {
    // Win32 drops dirty regions for hidden windows. Keep this test window
    // outside the desktop without activation to exercise normal invalidation.
    SetWindowPos(window_,nullptr,-32000,-32000,0,0,SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE);
    ShowWindow(window_,SW_SHOWNOACTIVATE);
    // Preserve the last painted colors. Only dirty controls are rendered again,
    // so a missed child invalidation cannot be hidden by a full screenshot.
    std::map<int,COLORREF> colors;
    auto paintDirty=[&](const std::vector<int>& ids,bool force=false) {
        HDC dc=GetDC(window_);
        for(int id:ids) {
            HWND child=control(id);
            if(!force&&!GetUpdateRect(child,nullptr,FALSE)) continue;
            RECT r{};GetClientRect(child,&r);
            HDC memory=CreateCompatibleDC(dc);auto bitmap=CreateCompatibleBitmap(dc,r.right,r.bottom);auto old=SelectObject(memory,bitmap);
            SendMessageW(child,WM_PRINT,reinterpret_cast<WPARAM>(memory),PRF_CLIENT|PRF_NONCLIENT|PRF_ERASEBKGND);
            colors[id]=GetPixel(memory,d(8),r.bottom/2);
            SelectObject(memory,old);DeleteObject(bitmap);DeleteDC(memory);ValidateRect(child,nullptr);
        }
        ReleaseDC(window_,dc);
    };
    const std::vector<int> nav{NavTask,NavHistory,NavEnvironment,NavSettings,NavLogs},tabs{SettingsAudio,SettingsNetwork,SettingsSounds},modes{Strict,Relaxed,Extract};
    nlohmann::json checks=nlohmann::json::array();
    auto check=[&](const std::vector<int>& ids,int selected,const char* group) {
        bool passed=true;int highlighted=0;
        nlohmann::json painted=nlohmann::json::object();
        for(int id:ids) {
            if(colors[id]==Soft)++highlighted;
            painted[std::to_string(id)]=colors[id];
            passed=passed&&(id==selected?colors[id]==Soft:colors[id]==White);
        }
        checks.push_back({{"group",group},{"selected",selected},{"highlighted",highlighted},{"painted_colors",painted},{"passed",passed}});
    };
    auto click=[&](int id) {SendMessageW(window_,WM_COMMAND,MAKEWPARAM(id,BN_CLICKED),reinterpret_cast<LPARAM>(control(id)));};
    selectPage(0);paintDirty(nav,true);
    for(int round=0;round<3;++round) for(int page:{1,2,3,4,0,3,1,4,2,0}) {
        click(NavTask+page);paintDirty(nav);check(nav,NavTask+page,"sidebar");
    }
    selectPage(3,0);paintDirty(tabs,true);
    for(int tab:{1,2,0,2,1,0,2,0,1}) {
        click(tabs[tab]);paintDirty(tabs);check(tabs,tabs[tab],"settings-tabs");
    }
    const std::vector<int> environmentTabs{EnvironmentModels,EnvironmentBase};
    selectPage(2);paintDirty(environmentTabs,true);
    for(int id:{EnvironmentBase,EnvironmentModels,EnvironmentBase,EnvironmentModels}) {
        click(id);paintDirty(environmentTabs);check(environmentTabs,id,"environment-tabs");
    }
    selectPage(0);paintDirty(modes,true);
    for(int id:{Strict,Extract,Relaxed,Extract,Strict,Relaxed}) {click(id);paintDirty(modes);check(modes,id,"mode-cards");}
    bool passed=std::all_of(checks.begin(),checks.end(),[](const auto& row){return row.at("passed").template get<bool>();});
    WriteJson(std::filesystem::path(Wide(options_["test-navigation"])),{{"passed",passed},{"checks",checks}});
    selectPage(4);status_=passed?L"连续切换验证通过：只有当前页面保持选中高亮。":L"导航增量重绘测试失败。";
    completed_=passed;finishTest(passed?0:1);
}
