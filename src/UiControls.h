#pragma once
#include <windows.h>
#include <uxtheme.h>

// Paint a complete dirty region before exposing it to the target DC.
class BufferedSurface {
public:
    BufferedSurface(HDC target, const RECT& bounds);
    ~BufferedSurface();
    BufferedSurface(const BufferedSurface&) = delete;
    BufferedSurface& operator=(const BufferedSurface&) = delete;
    HDC dc() const { return dc_; }
private:
    HPAINTBUFFER buffer_ = nullptr;
    HDC dc_ = nullptr;
};

void InitPaintControl(HWND window);

void InitChoiceControl(HWND window);
void InitToggleControl(HWND window, bool animated = false);
void ToggleChecked(HWND window);
double ToggleVisualPosition(HWND window);
void DrawCheckboxControl(const DRAWITEMSTRUCT* item, HFONT font, UINT dpi);
void DrawSwitchControl(const DRAWITEMSTRUCT* item, HFONT font, UINT dpi);
void ShowChoiceMenu(HWND owner, HWND control, HFONT font, UINT dpi);
bool MeasureChoiceMenuItem(MEASUREITEMSTRUCT* item);
bool DrawChoiceMenuItem(const DRAWITEMSTRUCT* item);
