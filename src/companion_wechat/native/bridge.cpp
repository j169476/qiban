// SPDX-License-Identifier: LicenseRef-helper-noncommercial-1.0
// Modified from Prometheus-first/helper a81c495770b397646ba0661118927c83589c41fc.
// See LICENSE.helper and README.md. Send-only reusable bridge, no hooks or file logs.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <cstdint>
#include <cstring>
#include <iterator>
#include <functional>
#include <memory>
#include <set>
#include <string>
#include "core_version.h"

struct SharedMessage { void* object; void* control; };
struct SmallString { char data[16]; uint64_t length; uint64_t capacity; };
struct TestResult {
    DWORD stage, exception;
    uint64_t vtable_rva;
    uint32_t native_status;
    uint64_t exception_pc, fault_address, access_kind;
    uint32_t native_status_high;
};
static uintptr_t core_base = 0;
// Only these audited ABI helpers are retained from upstream. No receive code.
#include "helper_abi.inc"


static bool personal_id(const char* value) {
    size_t n = strnlen_s(value, 128);
    if (!n || n >= 128 || strcmp(value, "filehelper") == 0 || strncmp(value, "gh_", 3) == 0) return false;
    for (size_t i = 0; i < n; ++i) {
        char c = value[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.')) return false;
    }
    return true;
}

static bool account_matches(const char* expected) {
    // Same bounded object chain and double-read as upstream account.py; exact build selected first.
    __try {
        auto app = *reinterpret_cast<uintptr_t*>(core_address(core_base, 0xB912690));
        if (!app || *reinterpret_cast<uintptr_t*>(app) != core_address(core_base, 0x8D804E8) ||
            *reinterpret_cast<uintptr_t*>(core_address(core_base, 0x8D804E8) + 0x60) != core_base + 0x469A0) return false;
        auto context = *reinterpret_cast<uintptr_t*>(app + 0x68);
        if (!context || *reinterpret_cast<uintptr_t*>(context) != core_address(core_base, 0x8DFE2F8) ||
            *reinterpret_cast<uintptr_t*>(core_address(core_base, 0x8DFE2F8) + 0x18) != core_base + (core_profile_id == 1 ? 0x36F930 : 0x3855C0) ||
            memcmp(reinterpret_cast<void*>(core_base + (core_profile_id == 1 ? 0x36F930 : 0x3855C0)), "\x48\x8d\x41\x48\xc3", 5)) return false;
        auto ptr = reinterpret_cast<SmallString*>(context + 0x48);
        SmallString before = *ptr;
        if (!before.length || before.length > 127 || before.capacity < before.length ||
            (before.capacity != 15 && (before.capacity < 16 || before.capacity > 1048576))) return false;
        const char* data = before.capacity == 15 ? ptr->data : *reinterpret_cast<char**>(before.data);
        char copy[128]{};
        memcpy(copy, data, before.length);
        return personal_id(copy) && strcmp(copy, expected) == 0 &&
            memcmp(ptr, &before, sizeof(before)) == 0 && memcmp(data, copy, before.length) == 0 &&
            *reinterpret_cast<uintptr_t*>(core_address(core_base, 0xB912690)) == app &&
            *reinterpret_cast<uintptr_t*>(app + 0x68) == context &&
            *reinterpret_cast<uintptr_t*>(context) == core_address(core_base, 0x8DFE2F8);
    } __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}



#include "session.inc"
struct Completion {
    volatile LONG references;
    uint32_t padding;
    void (__fastcall* invoke)(Completion*, const unsigned char*);
    void (__fastcall* destroy)(Completion*);
};
static void __fastcall completion_invoke(Completion*, const unsigned char* result) {
    InterlockedIncrement(&callbacks);
    __try {
        InterlockedExchange(&completed, result && result[0] == 1 ? 1 : 2);
        InterlockedExchange(&stage, 3);
    } __finally { InterlockedDecrement(&callbacks); }
}
static void __fastcall completion_destroy(Completion* self) {
    delete self;
    InterlockedDecrement(&completions);
}
static void invoke_builder(const Request* r) {
    if (!guard(*r)) { InterlockedExchange(&error_code, ERROR_ACCESS_DENIED); return; }
    struct TextRequest { SmallString receiver; SmallString body; } request{};
    request.receiver.capacity = request.body.capacity = 15;
    SmallString description{}; description.capacity = 15;
    TestResult report{};
    Completion* completion = nullptr;
    __try {
      __try {
        if (!set_native_string(&request.receiver, r->target) || !set_native_string(&request.body, r->text) ||
            !set_inline(&description, "wxbot.text")) { InterlockedExchange(&error_code, 8); return; }
        completion = new (std::nothrow) Completion{1, 0, completion_invoke, completion_destroy};
        if (!completion) { InterlockedExchange(&error_code, 8); return; }
        InterlockedIncrement(&completions);
        // Last guard is on WeChat's own scheduled task, immediately before the builder.
        if (!guard(*r)) { InterlockedExchange(&error_code, ERROR_ACCESS_DENIED); return; }
        InterlockedExchange(&stage, 2);
        using Builder = void (__fastcall*)(void*, TextRequest*, SmallString*, Completion**);
        reinterpret_cast<Builder>(core_address(core_base, 0x54b02f0))(nullptr, &request, &description, &completion);
      } __except(record_exception(GetExceptionInformation(), &report)) {
        InterlockedExchange(&error_code, static_cast<LONG>(report.exception));
      }
    } __finally {
      // Includes early validation/allocation returns. After an ABI exception keep
      // potentially owned allocations alive; never guess ownership and double-free.
      if (!report.exception) {
        if (completion) completion_destroy(completion);
        free_native_string(&request.receiver); free_native_string(&request.body);
      }
    }
}

struct Lease {
    Request request;
    explicit Lease(const Request& r) : request(r) { InterlockedIncrement(&tasks); }
    ~Lease() { SecureZeroMemory(&request, sizeof(request)); InterlockedDecrement(&tasks); }
};
static DWORD schedule(std::function<void()>* callable) {
    static_assert(sizeof(std::function<void()>) == 64, "MSVC function ABI");
    struct Location { const char* function; const char* file; uint32_t line, pad; void* caller; };
    Location location{"wxbot.selftest", "selftest_bridge.cpp", 1, 0, nullptr};
    SharedMessage scheduler{}, future{};
    TestResult report{};
    __try {
        __try {
            using App = void* (__fastcall*)();
            using Scheduler = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            auto app = reinterpret_cast<App>(core_address(core_base, 0x45d10))();
            if (!app) return ERROR_NOT_READY;
            reinterpret_cast<Scheduler>(core_address(core_base, 0x47310))(app, &scheduler);
            if (!scheduler.object || !scheduler.control) return ERROR_NOT_READY;
            using Schedule = SharedMessage* (__fastcall*)(void*, SharedMessage*, Location*, std::function<void()>*, uint32_t);
            reinterpret_cast<Schedule>(core_address(core_base, 0x2fb010))(scheduler.object, &future, &location, callable, 1);
        } __finally {
            guarded_release_shared(&future, &report); guarded_release_shared(&scheduler, &report);
        }
    } __except(record_exception(GetExceptionInformation(), &report)) { }
    return report.exception ? report.exception : 0;
}


extern "C" __declspec(dllexport) DWORD WINAPI XlnCommit(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    const auto& r = *static_cast<Request*>(parameter);
    Request copy{};
    if (!claim(r, copy)) return ERROR_ACCESS_DENIED;
    try {
        auto lease = std::make_shared<Lease>(copy);
        SecureZeroMemory(&copy, sizeof(copy));
        std::function<void()> task = [lease]() {
            InterlockedIncrement(&callbacks);
            __try { invoke_builder(&lease->request); }
            __finally { InterlockedDecrement(&callbacks); }
        };
        auto code = schedule(&task);
        if (code) InterlockedExchange(&error_code, static_cast<LONG>(code));
        return code;
    } catch (...) {
        SecureZeroMemory(&copy, sizeof(copy));
        InterlockedExchange(&error_code, ERROR_NOT_ENOUGH_MEMORY);
        return ERROR_NOT_ENOUGH_MEMORY;
    }
}
extern "C" __declspec(dllexport) DWORD WINAPI XlnStatus(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    AcquireSRWLockShared(&lock);
    Status r{sizeof(Status), static_cast<uint32_t>(stage), static_cast<uint32_t>(error_code),
        static_cast<uint32_t>(completed), static_cast<uint32_t>(tasks), static_cast<uint32_t>(completions),
        static_cast<uint32_t>(callbacks), static_cast<uint32_t>(configured), sequence};
    ReleaseSRWLockShared(&lock);
    *static_cast<Status*>(parameter) = r;
    return 0;
}
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, void*) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
