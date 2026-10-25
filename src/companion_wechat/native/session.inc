// A process-lifetime pinned module, one controller lease, one in-flight text.
// No hooks, timers, sockets, logging, UI activation or persistent secrets.
struct Request {
    uint32_t size, pid, controller, reserved;
    uint64_t born, controller_born, window, deadline, sequence;
    char session[33], account[128], owner[128], family[128], target[128], text[1024];
};
struct Status {
    uint32_t size, stage, error, completed, tasks, completions, callbacks, active;
    uint64_t sequence;
};
static_assert(sizeof(Request) == 1632, "Request ABI");
static_assert(sizeof(Status) == 40, "Status ABI");
static Request authorized{}, prepared{};
static SRWLOCK lock = SRWLOCK_INIT;
static volatile LONG stage = 0, error_code = 0, completed = 0;
static volatile LONG tasks = 0, completions = 0, callbacks = 0, configured = 0;
static uint64_t sequence = 0;

static bool idle() { return !tasks && !completions && !callbacks; }
static bool process_matches(HANDLE handle, uint64_t expected) {
    FILETIME born{}, exit{}, kernel{}, user{};
    return handle && WaitForSingleObject(handle, 0) == WAIT_TIMEOUT &&
        GetProcessTimes(handle, &born, &exit, &kernel, &user) &&
        ((uint64_t(born.dwHighDateTime) << 32) | born.dwLowDateTime) == expected;
}
static bool controller_alive(const Request& r) {
    HANDLE handle = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, r.controller);
    bool valid = process_matches(handle, r.controller_born);
    if (handle) CloseHandle(handle);
    return valid;
}
static bool token(const char* value) {
    if (strnlen_s(value, 33) != 32) return false;
    for (int i = 0; i < 32; ++i)
        if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return false;
    return true;
}
static bool group_id(const char* value) {
    size_t n = strnlen_s(value, 128);
    if (n < 10 || n >= 128 || strcmp(value + n - 9, "@chatroom")) return false;
    for (size_t i = 0; i < n - 9; ++i) if (value[i] < '0' || value[i] > '9') return false;
    return true;
}
static bool same_session(const Request& r) {
    return configured && r.size == sizeof(Request) && r.pid == authorized.pid && r.born == authorized.born &&
        r.controller == authorized.controller && r.controller_born == authorized.controller_born &&
        !memcmp(r.session, authorized.session, sizeof(r.session)) &&
        !memcmp(r.account, authorized.account, sizeof(r.account)) &&
        !memcmp(r.owner, authorized.owner, sizeof(r.owner)) &&
        !memcmp(r.family, authorized.family, sizeof(r.family));
}
static bool identity_guard(const Request& r) {
    DWORD owner = 0;
    auto hwnd = reinterpret_cast<HWND>(r.window);
    GetWindowThreadProcessId(hwnd, &owner);
    return r.size == sizeof(Request) && r.pid == GetCurrentProcessId() && owner == r.pid && IsIconic(hwnd) &&
        process_matches(GetCurrentProcess(), r.born) && controller_alive(r) && account_matches(r.account);
}
static bool guard(const Request& r) {
    AcquireSRWLockShared(&lock);
    bool valid = same_session(r) && r.sequence == sequence && stage == 1 && !error_code &&
        GetTickCount64() <= r.deadline &&
        (!strcmp(r.target, authorized.owner) || (authorized.family[0] && !strcmp(r.target, authorized.family)));
    ReleaseSRWLockShared(&lock);
    return valid && identity_guard(r);
}

static bool claim(const Request& r, Request& copy) {
    AcquireSRWLockExclusive(&lock);
    bool valid = same_session(r) && idle() && !error_code && stage == 0 && r.sequence == sequence &&
        prepared.size && !memcmp(&r, &prepared, sizeof(Request)) && GetTickCount64() <= prepared.deadline &&
        identity_guard(r);
    if (valid) {
        copy = prepared; copy.deadline = GetTickCount64() + 1500;
        SecureZeroMemory(&prepared, sizeof(prepared)); stage = 1;
    }
    ReleaseSRWLockExclusive(&lock);
    return valid;
}

extern "C" __declspec(dllexport) DWORD WINAPI XlnOpen(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    const auto& r = *static_cast<Request*>(parameter);
    if (r.size != sizeof(Request) || !token(r.session) || !personal_id(r.account) ||
        !personal_id(r.owner) || !strcmp(r.owner, r.account) || (r.family[0] && !group_id(r.family)) ||
        r.controller == r.pid || r.reserved || r.sequence || r.target[0] || r.text[0]) return ERROR_INVALID_PARAMETER;
    AcquireSRWLockExclusive(&lock);
    DWORD code = ERROR_BUSY;
    if (idle() && !error_code && (!configured || !controller_alive(authorized))) {
        auto core = GetModuleHandleW(L"Weixin.dll");
        if (!core || !core_fingerprint(core)) code = ERROR_REVISION_MISMATCH;
        else {
            core_base = reinterpret_cast<uintptr_t>(core);
            HMODULE pinned{};
            if (!identity_guard(r)) code = ERROR_ACCESS_DENIED;
            else if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                    reinterpret_cast<LPCWSTR>(&XlnOpen), &pinned)) code = ERROR_DLL_INIT_FAILED;
            else {
                authorized = r; SecureZeroMemory(&prepared, sizeof(prepared)); sequence = 0;
                stage = completed = 0; configured = 1; code = 0;
            }
        }
    }
    ReleaseSRWLockExclusive(&lock);
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI XlnPrepare(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    const auto& r = *static_cast<Request*>(parameter);
    size_t size = strnlen_s(r.text, sizeof(r.text));
    if (!size || size >= sizeof(r.text) || !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, r.text,
            static_cast<int>(size), nullptr, 0) || strnlen_s(r.target, 128) >= 128) return ERROR_INVALID_PARAMETER;
    for (size_t i = 0; i < size; ++i) if (static_cast<unsigned char>(r.text[i]) < 32 || r.text[i] == 127)
        return ERROR_INVALID_PARAMETER;
    AcquireSRWLockExclusive(&lock);
    DWORD code = ERROR_ACCESS_DENIED;
    if (same_session(r) && idle() && !error_code && stage != 1 && r.sequence == sequence + 1 &&
        r.sequence && r.deadline >= GetTickCount64() && r.deadline <= GetTickCount64() + 15000 &&
        (!strcmp(r.target, authorized.owner) || (authorized.family[0] && !strcmp(r.target, authorized.family))) &&
        identity_guard(r)) {
        prepared = r; sequence = r.sequence; stage = 0; completed = 0; code = 0;
    }
    ReleaseSRWLockExclusive(&lock);
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI XlnCancel(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    const auto& r = *static_cast<Request*>(parameter);
    AcquireSRWLockExclusive(&lock);
    bool valid = same_session(r) && r.sequence == sequence;
    if (valid) { SecureZeroMemory(&prepared, sizeof(prepared)); if (stage < 2) stage = 4; }
    ReleaseSRWLockExclusive(&lock);
    return valid ? 0 : ERROR_ACCESS_DENIED;
}

extern "C" __declspec(dllexport) DWORD WINAPI XlnClose(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    const auto& r = *static_cast<Request*>(parameter);
    AcquireSRWLockExclusive(&lock);
    bool valid = same_session(r);
    if (valid) {
        configured = 0; SecureZeroMemory(&prepared, sizeof(prepared));
        SecureZeroMemory(&authorized, sizeof(authorized));
    }
    ReleaseSRWLockExclusive(&lock);
    return valid ? 0 : ERROR_ACCESS_DENIED;
}
