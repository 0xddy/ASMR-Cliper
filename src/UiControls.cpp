#include "UiControls.h"
#include "UiTheme.h"
#include <commctrl.h>
#include <objidl.h>
#include <gdiplus.h>
#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

namespace {
LRESULT CALLBACK PaintControlProc(HWND window,UINT message,WPARAM wp,LPARAM lp,UINT_PTR id,DWORD_PTR) {
    // Owner drawing fills the whole control, including its rounded corners.
    if(message==WM_ERASEBKGND)return 1;
    if(message==WM_NCDESTROY)RemoveWindowSubclass(window,PaintControlProc,id);
    return DefSubclassProc(window,message,wp,lp);
}
struct ChoiceItem {
    std::wstring label;
    HFONT font;
    UINT dpi;
    int width;
    bool checked;
    int scale(int value) const {return MulDiv(value,dpi,96);}
};
constexpr UINT_PTR AnimationTimer=0xAC11;
constexpr double AnimationDuration=180.;
struct State {
    bool toggle=false,checked=false,animated=false,animating=false,hot=false;
    double position=0.,from=0.;ULONGLONG started=0;
    int selection=-1;std::vector<std::wstring> choices;
};
void FinishAnimation(HWND window,State* state) {
    KillTimer(window,AnimationTimer);state->animating=false;state->position=state->checked?1.:0.;
}
double Position(HWND window,State* state) {
    if(state->animating) {
        double t=std::min(1.,(GetTickCount64()-state->started)/AnimationDuration);
        double ease=1.-(1.-t)*(1.-t)*(1.-t);
        state->position=state->from+((state->checked?1.:0.)-state->from)*ease;
        if(t>=1.)FinishAnimation(window,state);
    }
    return state->position;
}
LRESULT CALLBACK ControlProc(HWND window,UINT message,WPARAM wp,LPARAM lp,UINT_PTR subclass,DWORD_PTR data) {
    auto state=reinterpret_cast<State*>(data);
    if(message==WM_NCDESTROY) {KillTimer(window,AnimationTimer);RemoveWindowSubclass(window,ControlProc,subclass);delete state;return DefSubclassProc(window,message,wp,lp);}
    if(state->toggle) {
        if(message==WM_MOUSEMOVE&&IsWindowEnabled(window)&&!state->hot) {
            state->hot=true;TRACKMOUSEEVENT tracking{sizeof(tracking),TME_LEAVE,window,0};TrackMouseEvent(&tracking);
            InvalidateRect(window,nullptr,FALSE);
        }
        if(message==WM_MOUSELEAVE) {state->hot=false;InvalidateRect(window,nullptr,FALSE);}
        if(message==BM_GETCHECK)return state->checked?BST_CHECKED:BST_UNCHECKED;
        if(message==BM_SETCHECK) {state->checked=wp==BST_CHECKED;FinishAnimation(window,state);InvalidateRect(window,nullptr,FALSE);return 0;}
        if(message==WM_TIMER&&wp==AnimationTimer) {Position(window,state);InvalidateRect(window,nullptr,FALSE);return 0;}
        if(((message==WM_SHOWWINDOW||message==WM_ENABLE)&&!wp)||
           (message==WM_WINDOWPOSCHANGED&&(reinterpret_cast<WINDOWPOS*>(lp)->flags&SWP_HIDEWINDOW))) {
            state->hot=false;FinishAnimation(window,state);InvalidateRect(window,nullptr,FALSE);
        }
    } else {
        switch(message) {
        case CB_ADDSTRING:state->choices.emplace_back(reinterpret_cast<LPCWSTR>(lp));return state->choices.size()-1;
        case CB_GETCOUNT:return state->choices.size();
        case CB_GETCURSEL:return state->selection;
        case CB_SETCURSEL:
            state->selection=wp<state->choices.size()?static_cast<int>(wp):-1;
            SetWindowTextW(window,state->selection<0?L"":state->choices[state->selection].c_str());
            InvalidateRect(window,nullptr,FALSE);return state->selection;
        case CB_GETLBTEXTLEN: return wp<state->choices.size()?static_cast<LRESULT>(state->choices[wp].size()):CB_ERR;
        case CB_GETLBTEXT:
            if(wp>=state->choices.size()||!lp)return CB_ERR;
            memcpy(reinterpret_cast<void*>(lp),state->choices[wp].c_str(),(state->choices[wp].size()+1)*sizeof(wchar_t));return state->choices[wp].size();
        case WM_GETDLGCODE:return DefSubclassProc(window,message,wp,lp)|DLGC_WANTARROWS;
        case WM_KEYDOWN:
            if(wp==VK_UP||wp==VK_DOWN||wp==VK_F4) {
                SendMessageW(GetParent(window),WM_COMMAND,MAKEWPARAM(GetDlgCtrlID(window),BN_CLICKED),reinterpret_cast<LPARAM>(window));return 0;
            }
            break;
        }
    }
    return DefSubclassProc(window,message,wp,lp);
}
State* ControlState(HWND window) {
    DWORD_PTR data=0;
    return GetWindowSubclass(window,ControlProc,1,&data)?reinterpret_cast<State*>(data):nullptr;
}
void Attach(HWND window,bool toggle,bool animated=false) {
    auto state=new State;state->toggle=toggle;state->animated=animated;
    if(!SetWindowSubclass(window,ControlProc,1,reinterpret_cast<DWORD_PTR>(state)))delete state;
}
}
BufferedSurface::BufferedSurface(HDC target,const RECT& bounds):dc_(target) {
    HDC memory=nullptr;
    buffer_=BeginBufferedPaint(target,&bounds,BPBF_COMPATIBLEBITMAP,nullptr,&memory);
    if(buffer_)dc_=memory;
}
BufferedSurface::~BufferedSurface() {if(buffer_)EndBufferedPaint(buffer_,TRUE);}
void InitPaintControl(HWND window) {SetWindowSubclass(window,PaintControlProc,2,0);}
void InitChoiceControl(HWND window) {Attach(window,false);}
void InitToggleControl(HWND window,bool animated) {Attach(window,true,animated);}

void ToggleChecked(HWND window) {
    auto state=ControlState(window);if(!state||!state->toggle||!IsWindowEnabled(window))return;
    state->from=Position(window,state);state->checked=!state->checked;
    BOOL animations=TRUE;SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION,0,&animations,0);
    if(state->animated&&animations&&IsWindowVisible(window)) {
        state->started=GetTickCount64();state->animating=true;
        if(!SetTimer(window,AnimationTimer,16,nullptr))FinishAnimation(window,state);
    } else FinishAnimation(window,state);
    InvalidateRect(window,nullptr,FALSE);
}

double ToggleVisualPosition(HWND window) {
    auto state=ControlState(window);return state&&state->toggle?Position(window,state):0.;
}

void DrawCheckboxControl(const DRAWITEMSTRUCT* item,HFONT font,UINT dpi) {
    using namespace Gdiplus;
    // The owner provides one buffered surface for the entire row. Fractional
    // geometry keeps the outline and round-ended tick smooth at fractional DPI.
    HDC dc=item->hDC;RECT row=item->rcItem;
    FillRect(dc,&row,static_cast<HBRUSH>(GetStockObject(WHITE_BRUSH)));
    auto state=ControlState(item->hwndItem);
    bool disabled=(item->itemState&ODS_DISABLED)!=0;
    bool hot=!disabled&&state&&state->hot;
    bool pressed=!disabled&&(item->itemState&ODS_SELECTED)!=0;
    bool focused=!disabled&&(item->itemState&ODS_FOCUS)&&!(item->itemState&ODS_NOFOCUSRECT);
    REAL unit=static_cast<REAL>(dpi)/96.f;
    double position=ToggleVisualPosition(item->hwndItem);
    auto color=[](COLORREF value) {return Color(255,GetRValue(value),GetGValue(value),GetBValue(value));};
    auto blend=[](COLORREF a,COLORREF b,double amount) {
        auto channel=[&](int x,int y){return static_cast<BYTE>(x+(y-x)*amount+.5);};
        return RGB(channel(GetRValue(a),GetRValue(b)),channel(GetGValue(a),GetGValue(b)),channel(GetBValue(a),GetBValue(b)));
    };
    COLORREF on=disabled?RGB(159,187,183):pressed?RGB(0,91,84):hot?RGB(0,108,100):UiTheme::Accent;
    COLORREF off=disabled?RGB(246,248,248):(hot||pressed)?RGB(239,247,245):UiTheme::White;
    COLORREF outline=disabled?RGB(215,225,224):(hot||focused||pressed)?RGB(65,141,132):RGB(180,199,195);
    {
        Graphics graphics(dc);graphics.SetSmoothingMode(SmoothingModeAntiAlias);
        graphics.SetPixelOffsetMode(PixelOffsetModeHighQuality);graphics.SetCompositingQuality(CompositingQualityHighQuality);
        REAL size=(pressed?18.f:19.f)*unit,centerX=row.left+12.f*unit,centerY=(row.top+row.bottom)/2.f;
        RectF box(centerX-size/2.f,centerY-size/2.f,size,size);
        REAL radius=4.5f*unit,diameter=2.f*radius;
        GraphicsPath shape;
        shape.AddArc(box.X,box.Y,diameter,diameter,180.f,90.f);
        shape.AddArc(box.GetRight()-diameter,box.Y,diameter,diameter,270.f,90.f);
        shape.AddArc(box.GetRight()-diameter,box.GetBottom()-diameter,diameter,diameter,0.f,90.f);
        shape.AddArc(box.X,box.GetBottom()-diameter,diameter,diameter,90.f,90.f);shape.CloseFigure();
        double fillAmount=std::min(1.,position*1.6);
        SolidBrush fill(color(blend(off,on,fillAmount)));
        Pen border(color(blend(outline,on,fillAmount)),1.15f*unit);
        graphics.FillPath(&fill,&shape);graphics.DrawPath(&border,&shape);
        // Reveal the short then long stroke along its actual path; reversing
        // a click retraces the same path without moving the label or hit area.
        REAL progress=static_cast<REAL>(std::clamp((position-.18)/.82,0.,1.));
        if(progress>0.f) {
            REAL tickUnit=unit*(pressed?18.f/19.f:1.f);
            PointF a(centerX-4.7f*tickUnit,centerY+.1f*tickUnit);
            PointF b(centerX-1.3f*tickUnit,centerY+3.3f*tickUnit);
            PointF c(centerX+5.f*tickUnit,centerY-3.5f*tickUnit);
            REAL first=std::hypot(b.X-a.X,b.Y-a.Y),second=std::hypot(c.X-b.X,c.Y-b.Y),length=(first+second)*progress;
            GraphicsPath tick;PointF end;
            if(length<=first) {end={a.X+(b.X-a.X)*length/first,a.Y+(b.Y-a.Y)*length/first};tick.AddLine(a,end);}
            else {tick.AddLine(a,b);REAL part=(length-first)/second;end={b.X+(c.X-b.X)*part,b.Y+(c.Y-b.Y)*part};tick.AddLine(b,end);}
            Pen pen(Color(static_cast<BYTE>(255*std::min(1.,position*2.)),255,255,255),1.9f*unit);
            pen.SetStartCap(LineCapRound);pen.SetEndCap(LineCapRound);pen.SetLineJoin(LineJoinRound);
            graphics.DrawPath(&pen,&tick);
        }
    }
    auto oldFont=SelectObject(dc,font);SetBkMode(dc,TRANSPARENT);SetTextColor(dc,disabled?UiTheme::Muted:UiTheme::Ink);
    RECT label=row;label.left+=MulDiv(36,dpi,96);
    int length=GetWindowTextLengthW(item->hwndItem);std::wstring title(length+1,L'\0');GetWindowTextW(item->hwndItem,title.data(),length+1);
    DrawTextW(dc,title.c_str(),length,&label,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS|DT_NOPREFIX);
    SelectObject(dc,oldFont);
}

void DrawSwitchControl(const DRAWITEMSTRUCT* item,HFONT font,UINT dpi) {
    using namespace Gdiplus;
    int width=item->rcItem.right-item->rcItem.left,height=item->rcItem.bottom-item->rcItem.top;
    if(width<=0||height<=0)return;
    HDC memory=CreateCompatibleDC(item->hDC);auto bitmap=CreateCompatibleBitmap(item->hDC,width,height);
    if(!memory||!bitmap){if(memory)DeleteDC(memory);if(bitmap)DeleteObject(bitmap);return;}
    auto oldBitmap=SelectObject(memory,bitmap);auto oldFont=SelectObject(memory,font);
    RECT bounds{0,0,width,height};FillRect(memory,&bounds,static_cast<HBRUSH>(GetStockObject(WHITE_BRUSH)));
    bool disabled=(item->itemState&ODS_DISABLED)!=0;
    auto scale=[&](int value){return MulDiv(value,dpi,96);};
    int length=GetWindowTextLengthW(item->hwndItem);std::wstring title(length+1,L'\0');GetWindowTextW(item->hwndItem,title.data(),length+1);
    RECT label=bounds;label.right-=scale(56);SetBkMode(memory,TRANSPARENT);SetTextColor(memory,disabled?UiTheme::Muted:UiTheme::Ink);
    DrawTextW(memory,title.c_str(),length,&label,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS|DT_NOPREFIX);
    double position=ToggleVisualPosition(item->hwndItem);
    COLORREF off=RGB(210,220,218),on=disabled?RGB(135,181,175):UiTheme::Accent;
    auto mix=[&](BYTE a,BYTE b){return static_cast<BYTE>(a+(b-a)*position+.5);};
    {
        Graphics graphics(memory);graphics.SetSmoothingMode(SmoothingModeAntiAlias);
        graphics.SetPixelOffsetMode(PixelOffsetModeHighQuality);graphics.SetCompositingQuality(CompositingQualityHighQuality);
        REAL unit=static_cast<REAL>(dpi)/96.f;
        // Inset by one device pixel so antialiased edges are never clipped.
        RectF track(width-44.f*unit-1.f,(height-24.f*unit)/2.f,44.f*unit,24.f*unit);
        GraphicsPath capsule;
        capsule.AddArc(track.X,track.Y,track.Height,track.Height,90.f,180.f);
        capsule.AddArc(track.GetRight()-track.Height,track.Y,track.Height,track.Height,270.f,180.f);capsule.CloseFigure();
        SolidBrush fill(Color(255,mix(GetRValue(off),GetRValue(on)),mix(GetGValue(off),GetGValue(on)),mix(GetBValue(off),GetBValue(on))));
        graphics.FillPath(&fill,&capsule);
        RectF thumb(track.X+(2.f+20.f*static_cast<REAL>(position))*unit,track.Y+2.f*unit,20.f*unit,20.f*unit);
        SolidBrush white(Color(255,255,255,255));graphics.FillEllipse(&white,thumb);
    }
    BitBlt(item->hDC,item->rcItem.left,item->rcItem.top,width,height,memory,0,0,SRCCOPY);
    SelectObject(memory,oldFont);SelectObject(memory,oldBitmap);DeleteObject(bitmap);DeleteDC(memory);
}

void ShowChoiceMenu(HWND owner,HWND control,HFONT font,UINT dpi) {
    if(!IsWindowEnabled(control))return;
    int count=static_cast<int>(SendMessageW(control,CB_GETCOUNT,0,0));
    if(count<=0)return;
    HMENU menu=CreatePopupMenu();if(!menu)return;
    MENUINFO info{sizeof(info)};info.fMask=MIM_STYLE|MIM_BACKGROUND;
    info.dwStyle=MNS_NOCHECK;info.hbrBack=static_cast<HBRUSH>(GetStockObject(WHITE_BRUSH));SetMenuInfo(menu,&info);
    RECT rect{};GetWindowRect(control,&rect);
    int selected=static_cast<int>(SendMessageW(control,CB_GETCURSEL,0,0));
    std::vector<ChoiceItem> items;items.reserve(count);
    for(int i=0;i<count;++i) {
        int length=static_cast<int>(SendMessageW(control,CB_GETLBTEXTLEN,i,0));
        std::wstring label(length+1,L'\0');SendMessageW(control,CB_GETLBTEXT,i,reinterpret_cast<LPARAM>(label.data()));label.resize(length);
        // Native menus reserve a submenu-arrow column even for owner-drawn rows.
        int chrome=GetSystemMetricsForDpi(SM_CXMENUSIZE,dpi)+GetSystemMetricsForDpi(SM_CXBORDER,dpi);
        items.push_back({std::move(label),font,dpi,rect.right-rect.left-chrome,i==selected});
        MENUITEMINFOW item{sizeof(item)};item.fMask=MIIM_FTYPE|MIIM_STRING|MIIM_DATA|MIIM_ID|MIIM_STATE;
        item.fType=MFT_OWNERDRAW;item.wID=i+1;item.fState=i==selected?MFS_CHECKED:MFS_UNCHECKED;
        item.dwTypeData=items.back().label.data();item.dwItemData=reinterpret_cast<ULONG_PTR>(&items.back());
        InsertMenuItemW(menu,i,TRUE,&item);
    }
    TPMPARAMS placement{sizeof(placement)};placement.rcExclude=rect;
    int choice=TrackPopupMenuEx(menu,TPM_RETURNCMD|TPM_NONOTIFY|TPM_LEFTALIGN|TPM_VERTICAL,
        rect.left,rect.bottom+MulDiv(6,dpi,96),owner,&placement);
    DestroyMenu(menu);
    if(choice>0&&choice<=count)SendMessageW(control,CB_SETCURSEL,choice-1,0);
}

bool MeasureChoiceMenuItem(MEASUREITEMSTRUCT* item) {
    if(item->CtlType!=ODT_MENU||!item->itemData)return false;
    auto entry=reinterpret_cast<const ChoiceItem*>(item->itemData);
    item->itemWidth=entry->width;item->itemHeight=entry->scale(44);return true;
}

bool DrawChoiceMenuItem(const DRAWITEMSTRUCT* item) {
    if(item->CtlType!=ODT_MENU||!item->itemData)return false;
    using namespace UiTheme;
    auto entry=reinterpret_cast<const ChoiceItem*>(item->itemData);
    auto scale=[&](int v){return entry->scale(v);};
    int saved=SaveDC(item->hDC);RECT row=item->rcItem;
    FillRect(item->hDC,&row,static_cast<HBRUSH>(GetStockObject(WHITE_BRUSH)));
    bool hover=(item->itemState&ODS_SELECTED)!=0;
    if(entry->checked||hover) {
        RECT background=row;InflateRect(&background,-scale(4),-scale(3));
        auto brush=CreateSolidBrush(entry->checked?(hover?RGB(214,237,230):Soft):RGB(241,246,245));
        SelectObject(item->hDC,brush);SelectObject(item->hDC,GetStockObject(NULL_PEN));
        RoundRect(item->hDC,background.left,background.top,background.right,background.bottom,scale(10),scale(10));
        SelectObject(item->hDC,GetStockObject(WHITE_BRUSH));DeleteObject(brush);
    }
    RECT text=row;text.left+=scale(12);text.right-=scale(40);
    SelectObject(item->hDC,entry->font);SetBkMode(item->hDC,TRANSPARENT);SetTextColor(item->hDC,entry->checked?Accent:Ink);
    DrawTextW(item->hDC,entry->label.c_str(),-1,&text,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS|DT_NOPREFIX);
    if(entry->checked) {
        auto pen=CreatePen(PS_SOLID,scale(2),Accent);SelectObject(item->hDC,pen);
        int x=row.right-scale(24),y=(row.top+row.bottom)/2;
        MoveToEx(item->hDC,x-scale(4),y,nullptr);LineTo(item->hDC,x-scale(1),y+scale(3));LineTo(item->hDC,x+scale(5),y-scale(4));
        SelectObject(item->hDC,GetStockObject(NULL_PEN));DeleteObject(pen);
    }
    RestoreDC(item->hDC,saved);return true;
}
