#pragma once
#include <windows.h>
#include <objidl.h>
#include <gdiplus.h>

// GitHub Octicons mark-github-16, Copyright (c) 2026 GitHub Inc., MIT.
// https://github.com/primer/octicons/blob/main/icons/mark-github-16.svg
// License: third_party/octicons/LICENSE.MIT
// Converted once from the upstream SVG; no runtime SVG parser is required.
// Upstream SVG SHA-256: 7421820090b50ac79d7c2bf7c951a433d7098a8a9c474809207bdd45f62d9d46
inline void DrawGithubMark(HDC dc, float x, float y, float size, COLORREF color) {
    if (!dc || size <= 0.f) return;
    Gdiplus::GraphicsPath path(Gdiplus::FillModeWinding);
    path.StartFigure();
    path.AddBezier(6.766f,11.328f,4.703f,11.078f,3.25f,9.594f,3.25f,7.672f);
    path.AddBezier(3.25f,7.672f,3.25f,6.891f,3.531f,6.047f,4.f,5.484f);
    path.AddBezier(4.f,5.484f,3.797f,4.969f,3.828f,3.875f,4.063f,3.422f);
    path.AddBezier(4.063f,3.422f,4.688f,3.344f,5.531f,3.672f,6.031f,4.125f);
    path.AddBezier(6.031f,4.125f,6.625f,3.938f,7.25f,3.844f,8.016f,3.844f);
    path.AddBezier(8.016f,3.844f,8.781f,3.844f,9.406f,3.938f,9.969f,4.109f);
    path.AddBezier(9.969f,4.109f,10.453f,3.672f,11.313f,3.344f,11.938f,3.422f);
    path.AddBezier(11.938f,3.422f,12.156f,3.844f,12.188f,4.937f,11.984f,5.469f);
    path.AddBezier(11.984f,5.469f,12.484f,6.062f,12.75f,6.859f,12.75f,7.672f);
    path.AddBezier(12.75f,7.672f,12.75f,9.594f,11.297f,11.047f,9.203f,11.312f);
    path.AddBezier(9.203f,11.312f,9.734f,11.656f,10.093f,12.406f,10.093f,13.266f);
    path.AddLine(10.093f,13.266f,10.093f,14.891f);
    path.AddBezier(10.093f,14.891f,10.093f,15.359f,10.484f,15.625f,10.953f,15.438f);
    path.AddBezier(10.953f,15.438f,13.781f,14.359f,16.f,11.53f,16.f,8.03f);
    path.AddBezier(16.f,8.03f,16.f,3.61f,12.406f,0.f,7.984f,0.f);
    path.AddBezier(7.984f,0.f,3.563f,0.f,0.f,3.61f,0.f,8.031f);
    path.AddArc(-0.00003f,0.172912f,15.76f,15.76f,-179.840676f,-70.059857f);
    path.AddBezier(5.172f,15.453f,5.594f,15.609f,6.f,15.328f,6.f,14.906f);
    path.AddLine(6.f,14.906f,6.f,13.656f);
    path.AddBezier(6.f,13.656f,5.781f,13.75f,5.5f,13.812f,5.25f,13.812f);
    path.AddBezier(5.25f,13.812f,4.219f,13.812f,3.61f,13.25f,3.172f,12.203f);
    path.AddBezier(3.172f,12.203f,3.f,11.781f,2.812f,11.531f,2.453f,11.484f);
    path.AddBezier(2.453f,11.484f,2.266f,11.469f,2.203f,11.391f,2.203f,11.297f);
    path.AddBezier(2.203f,11.297f,2.203f,11.109f,2.516f,10.969f,2.828f,10.969f);
    path.AddBezier(2.828f,10.969f,3.281f,10.969f,3.672f,11.25f,4.078f,11.829f);
    path.AddBezier(4.078f,11.829f,4.391f,12.281f,4.718f,12.484f,5.109f,12.484f);
    path.AddBezier(5.109f,12.484f,5.5f,12.484f,5.75f,12.344f,6.109f,11.984f);
    path.AddBezier(6.109f,11.984f,6.375f,11.719f,6.579f,11.484f,6.766f,11.328f);
    path.CloseFigure();
    Gdiplus::Matrix transform(size/16.f,0.f,0.f,size/16.f,x,y);
    path.Transform(&transform);
    Gdiplus::Graphics graphics(dc);
    graphics.SetSmoothingMode(Gdiplus::SmoothingModeAntiAlias);
    graphics.SetPixelOffsetMode(Gdiplus::PixelOffsetModeHighQuality);
    Gdiplus::SolidBrush brush(Gdiplus::Color(255,GetRValue(color),GetGValue(color),GetBValue(color)));
    graphics.FillPath(&brush,&path);
}
