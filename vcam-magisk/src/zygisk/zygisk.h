// Minimal subset of the Magisk Zygisk API headers.
//
// The full headers live in the Magisk source tree; we only need enough
// to declare a Zygisk module that hooks zygote-spawned processes. The
// API is stable at the v4 ABI as of Magisk 26.x. If you upgrade Magisk
// past v27 the constants here may need to be refreshed — check
// https://github.com/topjohnwu/Magisk/blob/master/native/src/zygisk/zygisk.hpp

#pragma once

#include <jni.h>

namespace zygisk {

constexpr int API_VERSION = 4;

struct AppSpecializeArgs {
    jint& uid;
    jint& gid;
    jintArray& gids;
    jint& runtime_flags;
    jobjectArray& rlimits;
    jint& mount_external;
    jstring& se_info;
    jstring& nice_name;
    jstring& instruction_set;
    jstring& app_data_dir;
    jboolean* is_top_app;
    jobjectArray* pkg_data_info_list;
    jobjectArray* whitelisted_data_info_list;
    jboolean* mount_data_dirs;
    jboolean* mount_storage_dirs;
};

struct ServerSpecializeArgs {
    jint& uid;
    jint& gid;
    jintArray& gids;
    jint& runtime_flags;
    jlong& permitted_capabilities;
    jlong& effective_capabilities;
};

class ModuleBase {
public:
    virtual void onLoad(void* api, JNIEnv* env) {}
    virtual void preAppSpecialize(AppSpecializeArgs* args) {}
    virtual void postAppSpecialize(const AppSpecializeArgs* args) {}
    virtual void preServerSpecialize(ServerSpecializeArgs* args) {}
    virtual void postServerSpecialize(const ServerSpecializeArgs* args) {}
};

enum Option : int {
    DLCLOSE_MODULE_LIBRARY = 0,
    FORCE_DENYLIST_UNMOUNT = 1,
};

}  // namespace zygisk

// REGISTER_ZYGISK_MODULE — Magisk loader looks up a symbol named
// `zygisk_module` (struct of API version + factory). The full macro is
// in the upstream header; here's the minimum.
struct zygisk_module_t {
    long apiVersion;
    void (*entry)(void* api, JNIEnv* env);
};

#define REGISTER_ZYGISK_MODULE(clazz)                                    \
    extern "C" [[gnu::visibility("default")]]                            \
    void zygisk_module_entry(void* api, JNIEnv* env) {                   \
        static clazz instance;                                           \
        instance.onLoad(api, env);                                       \
    }                                                                    \
    extern "C" [[gnu::visibility("default")]]                            \
    zygisk_module_t zygisk_module = {                                    \
        zygisk::API_VERSION,                                             \
        zygisk_module_entry,                                             \
    };
