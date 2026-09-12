#pragma once
#include <windows.h>

void InitChoiceControl(HWND window);
void InitToggleControl(HWND window);
void ShowChoiceMenu(HWND owner, HWND control, HFONT font, UINT dpi);
bool MeasureChoiceMenuItem(MEASUREITEMSTRUCT* item);
bool DrawChoiceMenuItem(const DRAWITEMSTRUCT* item);
