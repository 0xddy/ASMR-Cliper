#pragma once
#include "ProcessRunner.h"
#include <nlohmann/json.hpp>
#include <map>
#include <set>

enum ControlId { Input=101, Output, BrowseInput, BrowseOutput, Strict, Relaxed, ModeText,
    Language, Device, Silence, Before, After, Minimum, DenseGap, Audit,
    Start, Cancel, Doctor, Play, Mapping, OpenOutput, Prompt, Progress, Log,
    NavTask, NavHistory, NavEnvironment, NavSettings, NavLogs,
    Install, RepairPython, RepairDependencies, RepairWhisper, RepairAst, RepairFfmpeg,
    EditSettings, NetworkSettings, SettingsAudio, SettingsNetwork, ReservedSave, Reset,
    ProxyEnabled, ProxyUrl, TestProxy, SilenceDb, History, OpenLogs, ClearLog, NewTask,
    SettingsSounds, KeepSoftLaugh, KeepLoudLaugh, KeepVaping, KeepDrinking, KeepImpacts, Extract, RepairReview, ReviewFindings,
    EnvironmentBase, EnvironmentModels, SpeechChoice, ReviewChoice, RepairQwen, RepairAligner, RepairClap, RepairNeural,
    KeepHeartbeat, KeepTapping, OutputKind, ProgramMenu, SettingsRecognition, MenuEnabled, MenuModels,
    SettingsFade, FadeEnabled, FadeSeconds, StrictDetails, ModelSettings, EdgeFadeEnabled, EdgeFadeSeconds, AudioEncoding };

inline constexpr std::pair<int,const char*> SoundOptions[] = {
    {KeepSoftLaugh,"keep_soft_laugh"},{KeepHeartbeat,"keep_heartbeat"},{KeepTapping,"keep_tapping"},
    {KeepLoudLaugh,"keep_loud_laugh"},{KeepVaping,"keep_vaping"},
    {KeepDrinking,"keep_drinking"},{KeepImpacts,"keep_impacts"}};

inline bool IsSoundOption(int id) {
    for(auto option:SoundOptions)if(option.first==id)return true;
    return false;
}

class MainWindow {
public:
    MainWindow(std::filesystem::path root, nlohmann::json options);
    ~MainWindow();
    int run(HINSTANCE instance, int show);
private:
    static LRESULT CALLBACK WindowProc(HWND, UINT, WPARAM, LPARAM);
    LRESULT message(UINT, WPARAM, LPARAM);
    void createControls();
    void layout();
    void paint(HDC);
    void drawButton(const DRAWITEMSTRUCT*);
    void setFonts();
    void refreshMode();
    void enableControls(bool busy);
    void readSettings();
    void readEditingSettings();
    void readRecognitionSettings();
    void readNetworkSettings();
    void readSoundSettings();
    void readFadeSettings();
    void readModelSettings();
    std::vector<std::string> requiredComponents() const;
    void saveSettings();
    void autoSaveSetting(int id, bool reportInvalid = true);
    void flushPendingSettings(bool reportInvalid = true);
    void start(bool doctor);
    void environmentTask(const std::string& action, const std::string& component = "all");
    void programMenuTask();
    void storeResult();
    void selectPage(int page, int tab = -1);
    void updateVisibility();
    void showChoices(int id);
    bool environmentReady() const;
    void populateSettings(int tab = -1);
    void updateHistory();
    void selectHistory();
    void testNavigationRendering();
    void testControls();
    void testSwitches();
    void testAutoSave();
    void testProgress();
    void testProgramMenu();
    void testDropdowns();
    void testDropdownIdle(HWND popup);
    bool testing() const;
    void receive(const std::string&);
    void beginTiming();
    void stopTiming();
    double elapsedSeconds() const;
    void confirmLanguage(const nlohmann::json& data);
    std::string chooseLanguage(const nlohmann::json& data);
    void appendLog(const std::wstring&);
    void chooseFile(bool folder);
    void prompt();
    void showText(const std::wstring& title,const std::wstring& content);
    std::wstring reviewFindingsText() const;
    std::wstring programMenuText() const;
    void screenshot(const std::filesystem::path&, HWND popup = nullptr);
    void finishTest(DWORD code);
    int d(int value) const { return MulDiv(value, static_cast<int>(dpi_), 96); }
    HWND control(int id) const;
    HWND inputAt(POINT point) const;
    std::wstring value(int id) const;
    void text(int id, const std::wstring&);
    HWND window_ = nullptr;
    HWND languageDialog_ = nullptr;
    HINSTANCE instance_ = nullptr;
    UINT dpi_ = 96;
    std::filesystem::path root_;
    nlohmann::json cfg_, options_, lastResult_ = nlohmann::json::object();
    nlohmann::json environment_, history_ = nlohmann::json::array();
    std::map<std::string,nlohmann::json> components_;
    std::map<int, HWND> controls_;
    std::map<int,int> groups_;
    std::map<int,RECT> inputFrames_;
    HFONT font_ = nullptr, titleFont_ = nullptr, boldFont_ = nullptr, smallFont_ = nullptr, brandFont_ = nullptr;
    HBRUSH white_ = nullptr, background_ = nullptr;
    ProcessRunner runner_;
    bool busy_ = false, cancelled_ = false, completed_ = false, checking_ = false;
    std::wstring status_;
    bool notice_ = false, proxyTested_ = false;
    nlohmann::json proxyResults_ = nlohmann::json::array();
    size_t eventCount_ = 0;
    int testExit_ = 0;
    int page_ = 0, settingsTab_ = 0, environmentTab_ = 1;
    std::string activeAction_;
    std::wstring proxyStatus_;
    std::wstring downloadStatus_;
    nlohmann::json taskProgress_ = nlohmann::json::object();
    ULONGLONG taskStarted_ = 0, taskElapsed_ = 0;
    bool timing_ = false, hasTiming_ = false;
    bool mediaReady_ = false;
    bool strictExpanded_ = false;
    bool settingsReady_ = false, populatingSettings_ = false;
    std::set<int> pendingSettings_;
    int saveErrorControl_ = 0;
    std::wstring saveError_;
    std::string activeOutput_;
    int dropdownTestId_ = 0;
    bool dropdownTestCancel_ = false, dropdownTestIdle_ = false;
    nlohmann::json dropdownChecks_ = nlohmann::json::array();
};

std::string Utf8(const std::wstring& value);
std::wstring Wide(const std::string& value);
nlohmann::json ReadJson(const std::filesystem::path& path);
void WriteJson(const std::filesystem::path& path, const nlohmann::json& value);
