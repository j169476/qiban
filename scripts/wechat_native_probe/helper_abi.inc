// SPDX-License-Identifier: LicenseRef-helper-noncommercial-1.0
// Audited subset of Prometheus-first/helper a81c495770b397646ba0661118927c83589c41fc; see LICENSE.helper.
static int record_exception(EXCEPTION_POINTERS* exception, TestResult* result) {
    result->exception = exception->ExceptionRecord->ExceptionCode;
    result->exception_pc = reinterpret_cast<uintptr_t>(exception->ExceptionRecord->ExceptionAddress);
    if (exception->ExceptionRecord->NumberParameters >= 2) {
        result->access_kind = exception->ExceptionRecord->ExceptionInformation[0];
        result->fault_address = exception->ExceptionRecord->ExceptionInformation[1];
    }
    return EXCEPTION_EXECUTE_HANDLER;
}

static bool core_image_identity(HMODULE core, uint32_t* timestamp, uint32_t* image_size) {
    __try {
        const auto bytes = reinterpret_cast<const unsigned char*>(core);
        const auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(bytes);
        if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 64 || dos->e_lfanew > 1048576) return false;
        const auto nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(bytes + dos->e_lfanew);
        if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
                nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return false;
        *timestamp = nt->FileHeader.TimeDateStamp;
        *image_size = nt->OptionalHeader.SizeOfImage;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static bool core_fingerprint(HMODULE core) {
    uint32_t timestamp = 0, image_size = 0;
    if (!core_image_identity(core, &timestamp, &image_size)) return false;
    wchar_t path[32768]{};
    if (!GetModuleFileNameW(core, path, static_cast<DWORD>(std::size(path)))) return false;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size{};
    bool valid = GetFileSizeEx(file, &size) && core_size_supported(static_cast<uint64_t>(size.QuadPart));
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    if (valid && BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0) {
        // Windows allocates the hash object when pbHashObject is NULL on supported Windows.
        if (BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0) {
            unsigned char buffer[65536], digest[32];
            DWORD count = 0;
            while (valid) {
                valid = ReadFile(file, buffer, sizeof(buffer), &count, nullptr) != FALSE;
                if (!valid || !count) break;
                valid = BCryptHashData(hash, buffer, count, 0) >= 0;
            }
            valid = valid && BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0 &&
                core_select_build(static_cast<uint64_t>(size.QuadPart), digest, timestamp, image_size);
            BCryptDestroyHash(hash);
        } else valid = false;
        BCryptCloseAlgorithmProvider(algorithm, 0);
    } else valid = false;
    CloseHandle(file);
    return valid;
}


static void release_shared(SharedMessage& message) {
    if (!message.control) return;
    auto control = static_cast<unsigned char*>(message.control);
    if (_InterlockedDecrement(reinterpret_cast<volatile LONG*>(control + 8)) == 0) {
        using VirtualFunction = void(__fastcall*)(void*);
        auto table = *reinterpret_cast<VirtualFunction**>(control);
        table[0](control);
        if (_InterlockedDecrement(reinterpret_cast<volatile LONG*>(control + 12)) == 0) {
            table = *reinterpret_cast<VirtualFunction**>(control);
            table[1](control);
        }
    }
    message = {};
}

static void guarded_release_shared(SharedMessage* message, TestResult* report) {
    __try {
        release_shared(*message);
    } __except (record_exception(GetExceptionInformation(), report)) {
    }
}

static bool set_inline(SmallString* string, const char* text) {
    const size_t length = std::strlen(text);
    if (length > 15 || string->capacity != 15 || string->length != 0) return false;
    std::memcpy(string->data, text, length + 1);
    string->length = length;
    return true;
}

static bool set_native_string(SmallString* string, const char* text) {
    const size_t length = std::strlen(text);
    if (length < 16) return set_inline(string, text);
    if (length > 1023 || string->capacity != 15 || string->length != 0) return false;
    using Allocate = void* (__fastcall*)(size_t);
    char* buffer = static_cast<char*>(reinterpret_cast<Allocate>(core_address(core_base, 0x74ab89c))(length + 1));
    if (!buffer) return false;
    std::memcpy(buffer, text, length + 1);
    std::memcpy(string->data, &buffer, sizeof(buffer));
    string->length = string->capacity = length;
    return true;
}

static void free_native_string(SmallString* string) {
    if (string->capacity > 15) {
        void* allocation = *reinterpret_cast<void**>(string->data);
        using Free = void (__fastcall*)(void*, size_t);
        reinterpret_cast<Free>(core_address(core_base, 0x74ab8e0))(allocation, string->capacity + 1);
    }
    *string = {};
    string->capacity = 15;
}

