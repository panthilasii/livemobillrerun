// Zygisk module entry point for vcam-magisk.
//
// Lifecycle: Magisk's zygisk loader maps this .so into every spawned
// zygote child, then calls onLoad() once per process. preAppSpecialize()
// fires just before the process drops privileges into a normal app — at
// that point we have a chance to install hooks if the process matches
// our target packages (camera framework + apps that use Camera2 directly).

#include <android/log.h>
#include <jni.h>
#include <cstring>
#include <string>

#include "zygisk.h"
#include "camera_hook.h"
#include "yuv_reader.h"

#define LOG_TAG "vcam-zygisk"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

namespace {

// Process names where we install the camera hook.
//
// We're aggressive about which apps get hooks — only the user's chosen
// front-camera-using apps, plus the system camera service. Apps not on
// this list are skipped to keep the security blast radius small.
constexpr const char* kHookTargets[] = {
    "cameraserver",
    "system_server",          // CameraManagerGlobal lives here
    "com.android.camera2",
    "com.zhiliaoapp.musically",   // TikTok (international)
    "com.ss.android.ugc.trill",   // TikTok (some regions)
    "com.instagram.android",
};

bool ShouldHookProcess(const char* process) {
    if (!process) return false;
    for (const char* p : kHookTargets) {
        if (std::strcmp(process, p) == 0) return true;
    }
    return false;
}

class VcamModule : public zygisk::ModuleBase {
public:
    void onLoad(void* api, JNIEnv* env) override {
        api_ = api;
        env_ = env;
        LOGI("module loaded");
    }

    void preAppSpecialize(zygisk::AppSpecializeArgs* args) override {
        if (!args || !args->nice_name) return;
        const char* name = env_->GetStringUTFChars(args->nice_name, nullptr);
        const bool hook = ShouldHookProcess(name);
        LOGI("preAppSpecialize uid=%d nice=%s hook=%d", args->uid, name, hook);
        if (hook) {
            target_app_.assign(name);
        }
        env_->ReleaseStringUTFChars(args->nice_name, name);
    }

    void postAppSpecialize(const zygisk::AppSpecializeArgs* args) override {
        if (target_app_.empty()) return;
        LOGI("postAppSpecialize installing camera hook in %s", target_app_.c_str());

        // Initialize the YUV reader so the hook can pull frames quickly
        // once it starts intercepting Camera2 callbacks.
        if (!vcam::YuvReader::instance().Open()) {
            LOGW("YuvReader open failed — fallback to passthrough");
            return;
        }

        if (!vcam::camera::InstallHooks()) {
            LOGE("InstallHooks failed");
        }
    }

private:
    void* api_ = nullptr;
    JNIEnv* env_ = nullptr;
    std::string target_app_;
};

}  // namespace

REGISTER_ZYGISK_MODULE(VcamModule)
