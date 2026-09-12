#include "UiControls.h"
#include "UiTheme.h"
#include <commctrl.h>
#include <string>
#include <vector>

namespace {
struct ChoiceItem {
    std::wstring label;
    HFONT font;
    UINT dpi;
    int width;
    bool checked;
    int scale(int value) const {return MulDiv(value,dpi,96);}
};
struct State { bool toggle=false; bool checked=false; int selection=-1; std::vector<std::wstring> choices; };
LRESULT CALLBACK ControlProc(HWND window,UINT message,WPARAM wp,LPARAM lp,UINT_PTR subclass,DWORD_PTR data) {
    auto state=reinterpret_cast<State*>(data);
    if(message==WM_NCDESTROY) {RemoveWindowSubclass(window,ControlProc,subclass);delete state;return DefSubclassProc(window,message,wp,lp);}
    if(state->toggle) {
        if(message==BM_GETCHECK)return state->checked?BST_CHECKED:BST_UNCHECKED;
        if(message==BM_SETCHECK) {state->checked=wp==BST_CHECKED;InvalidateRect(window,nullptr,FALSE);return 0;}
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
void Attach(HWND window,bool toggle) {
    auto state=new State;state->toggle=toggle;
    if(!SetWindowSubclass(window,ControlProc,1,reinterpret_cast<DWORD_PTR>(state)))delete state;
}
}
void InitChoiceControl(HWND window) {Attach(window,false);}
void InitToggleControl(HWND window) {Attach(window,true);}

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
