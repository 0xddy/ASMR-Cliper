#pragma once
#include <windows.h>

void InitChoiceControl(HWND window);
void InitToggleControl(HWND window, bool animated = false);
void ToggleChecked(HWND window);
double ToggleVisualPosition(HWND window);
void DrawSwitchControl(const DRAWITEMSTRUCT* item, HFONT font, UINT dpi);
void ShowChoiceMenu(HWND owner, HWND control, HFONT font, UINT dpi);
bool MeasureChoiceMenuItem(MEASUREITEMSTRUCT* item);
bool DrawChoiceMenuItem(const DRAWITEMSTRUCT* item);
