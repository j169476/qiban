// Executes the production lease/prepare/claim/cancel state machine against
// synthetic process/window/clock functions. No client DLL or process is opened.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cstdlib>

static uint64_t now_tick = 1000, fake_born = 42;
static bool minimized = true, alive = true, identity = true;
static uintptr_t core_base = 0;
static bool personal_id(const char* p) { return p[0] && strlen(p) < 128 && !strchr(p, '@'); }
static bool account_matches(const char* p) { return identity && !strcmp(p, "wxid_bot"); }
static bool core_fingerprint(HMODULE) { return true; }
static HANDLE fake_open(DWORD, BOOL, DWORD) { return reinterpret_cast<HANDLE>(1); }
static DWORD fake_wait(HANDLE, DWORD) { return alive ? WAIT_TIMEOUT : WAIT_OBJECT_0; }
static BOOL fake_times(HANDLE, FILETIME* born, FILETIME*, FILETIME*, FILETIME*) {
    born->dwLowDateTime = static_cast<DWORD>(fake_born); born->dwHighDateTime = 0; return TRUE;
}
static BOOL fake_close(HANDLE) { return TRUE; }
static DWORD fake_owner(HWND, DWORD* pid) { *pid = 123; return 1; }
static BOOL fake_iconic(HWND) { return minimized; }
static DWORD fake_pid() { return 123; }
static HANDLE fake_current() { return reinterpret_cast<HANDLE>(1); }
static ULONGLONG fake_ticks() { return now_tick; }
static HMODULE fake_module(LPCWSTR) { return reinterpret_cast<HMODULE>(1); }
static BOOL fake_pin(DWORD, LPCWSTR, HMODULE* module) { *module = fake_module(nullptr); return TRUE; }

#define OpenProcess fake_open
#define WaitForSingleObject fake_wait
#define GetProcessTimes fake_times
#define CloseHandle fake_close
#define GetWindowThreadProcessId fake_owner
#define IsIconic fake_iconic
#define GetCurrentProcessId fake_pid
#define GetCurrentProcess fake_current
#define GetTickCount64 fake_ticks
#define GetModuleHandleW fake_module
#define GetModuleHandleExW fake_pin
#include "../src/companion_wechat/native/session.inc"

static int checks = 0;
static void expect(bool value) {
    ++checks;
    if (!value) { printf("native_session_failed:%d\n", checks); std::exit(1); }
}
static Request configuration() {
    Request r{};
    r.size = sizeof(r); r.pid = 123; r.born = 42; r.controller = 321; r.controller_born = 42; r.window = 1;
    strcpy_s(r.session, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    strcpy_s(r.account, "wxid_bot"); strcpy_s(r.owner, "wxid_main");
    return r;
}
int main() {
    Request r = configuration(), copy{};
    expect(XlnOpen(&r) == 0);
    expect(XlnOpen(&r) == ERROR_BUSY);  // A live controller cannot be replaced.
    r.sequence = 1; r.deadline = now_tick + 15000;
    strcpy_s(r.target, "wxid_main"); strcpy_s(r.text, "synthetic text");
    expect(XlnPrepare(&r) == 0);
    Request changed = r; strcpy_s(changed.text, "different text");
    expect(!claim(changed, copy));
    expect(claim(r, copy));
    expect(!claim(r, copy));  // Exact duplicate never submits twice.
    expect(guard(copy));
    minimized = false; expect(!guard(copy)); minimized = true;
    identity = false; expect(!guard(copy)); identity = true;
    now_tick += 1501; expect(!guard(copy)); now_tick = 1000;
    expect(XlnCancel(&r) == 0 && !guard(copy));
    r.sequence = 2; expect(XlnPrepare(&r) == 0);
    tasks = 1; r.sequence = 3; expect(XlnPrepare(&r) != 0); tasks = 0;
    r.sequence = 2; expect(claim(r, copy));
    expect(XlnClose(&r) == 0 && !guard(copy));
    r = configuration(); expect(XlnOpen(&r) == 0);
    r.sequence = 1; r.deadline = now_tick + 10000;
    strcpy_s(r.target, "123@chatroom"); strcpy_s(r.text, "synthetic text");
    expect(XlnPrepare(&r) != 0);  // Group is disabled in the native lease too.
    strcpy_s(r.target, "wxid_unbound"); expect(XlnPrepare(&r) != 0);
    expect(XlnClose(&r) == 0);
    r = configuration(); strcpy_s(r.family, "123@chatroom"); expect(XlnOpen(&r) == 0);
    r.sequence = 1; r.deadline = now_tick + 10000;
    strcpy_s(r.target, "123@chatroom"); strcpy_s(r.text, "synthetic text");
    expect(XlnPrepare(&r) == 0 && claim(r, copy));
    alive = false; expect(!guard(copy)); alive = true;
    expect(XlnClose(&r) == 0);
    r = configuration(); error_code = 99; expect(XlnOpen(&r) == ERROR_BUSY);
    printf("native_session_checks:%d\n", checks);
    return 0;
}
