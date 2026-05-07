#include "camera_hook.h"

#include <android/log.h>
#include <dlfcn.h>
#include <link.h>
#include <unistd.h>

#include <cstring>

#if VCAM_HAVE_DOBBY
#include "dobby.h"
#endif

#define LOG_TAG "vcam-hook"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace vcam::camera {

namespace {

bool g_hooks_active = false;

// Candidate libraries we expect to find a hookable symbol in. Probed
// in order; the first that resolves wins.
constexpr const char* kCandidateLibs[] = {
    "/system/lib64/libcameraservice.so",
    "/system/lib/libcameraservice.so",
    "/apex/com.android.media/lib64/libcameraservice.so",
    "/system_ext/lib64/libcameraservice.so",
};

// Mangled C++ symbols we expect to find in libcameraservice. Names
// vary slightly across Android versions (10–14) so we try several.
// On match we log the address — a real hook would then route through
// the inline-hook engine to a trampoline.
constexpr const char* kCandidateSymbols[] = {
    // CameraDeviceClient::onResultReceived(...) — the post-capture
    // callback we'd intercept to substitute YUV bytes. Name slightly
    // varies by Android version; we walk a small set.
    "_ZN7android20CameraDeviceClient16onResultReceivedERKNS_15CameraMetadataERKNS_8hardware6camera6device12CaptureResultERKNS_6VectorINS6_15PhysicalCaptureResultInfoEEE",
    "_ZN7android20CameraDeviceClient16onResultReceivedERKNS_15CameraMetadataERKNS_8hardware6camera6device12CaptureResultE",

    // Older CameraService::Client path used on Camera1 entry points.
    "_ZN7android13CameraService6Client14sendCommandFromEjii",
};

void* TryDlopen(const char* path) {
    void* h = dlopen(path, RTLD_NOW | RTLD_NOLOAD);
    if (!h) {
        // Not yet loaded into this process — fine, just means
        // libcameraservice isn't pulled in here. Try a fresh load
        // anyway in case we're early.
        h = dlopen(path, RTLD_NOW);
    }
    return h;
}

void* ResolveCameraServiceSymbol(const char** which_lib_out, const char** which_sym_out) {
    for (const char* lib : kCandidateLibs) {
        if (access(lib, F_OK) != 0) continue;
        void* h = TryDlopen(lib);
        if (!h) {
            LOGW("dlopen(%s) failed: %s", lib, dlerror());
            continue;
        }
        for (const char* sym : kCandidateSymbols) {
            void* p = dlsym(h, sym);
            if (p) {
                if (which_lib_out) *which_lib_out = lib;
                if (which_sym_out) *which_sym_out = sym;
                return p;
            }
        }
        // Don't dlclose — keeping it loaded is harmless and lets the
        // address stay valid for the future hook.
    }
    return nullptr;
}

}  // namespace

bool InstallHooks() {
    if (g_hooks_active) {
        LOGW("InstallHooks called twice — already active");
        return true;
    }

    const char* lib = nullptr;
    const char* sym = nullptr;
    void* addr = ResolveCameraServiceSymbol(&lib, &sym);
    if (!addr) {
        LOGW("no candidate camera service symbol resolved on this device — "
             "skeleton can't proceed without hand-tuned symbol names");
        return false;
    }

    LOGI("resolved %s @ %p in %s", sym, addr, lib);

#if VCAM_HAVE_DOBBY
    // Real installation path — wired up once you provide the
    // trampoline that mutates the CameraMetadata / CaptureResult to
    // splice in YUV bytes from `YuvReader::ReadLatest()`.
    //
    //   static decltype(&onResultReceivedTrampoline) g_orig = nullptr;
    //   if (DobbyHook(addr, (void*) onResultReceivedTrampoline,
    //                 (void**) &g_orig) != 0) {
    //       LOGE("DobbyHook failed");
    //       return false;
    //   }
    //
    // For now: just probe and log so we can confirm Dobby is linked.
    LOGI("Dobby is linked into this build — fill in DobbyHook() above.");
#else
    LOGW("Dobby not linked — set up third_party/dobby/ and rebuild "
         "to actually install the hook (see third_party/README.md).");
#endif

    g_hooks_active = false;  // flip to true once a real hook is wired
    return g_hooks_active;
}

void UninstallHooks() {
    if (!g_hooks_active) return;
    LOGI("UninstallHooks: skeleton — nothing to undo");
    g_hooks_active = false;
}

}  // namespace vcam::camera
